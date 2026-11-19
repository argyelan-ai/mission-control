#!/usr/bin/env python3
"""Output cap (stopReason=max_tokens) continues IN THE SAME ACP session.

Live 27.10.2026 20:30 and 28.10.2026 14:55 (local reasoning model, bench
task): three consecutive turns hit the 32k output cap while still thinking
(no text, no tool call); omp gave up ("response.incomplete recovery cap
reached"), the prompt ended stopReason=max_tokens. classify_acp lumped that
with the wall-clock cut (ABORT_MAXTIME) and drive_live_run RETRIED: a fresh
session, all planning lost, the next session repeated the same failure.

Now:
  * max_tokens  -> Kind.ABORT_OUTPUT_CAP: continue-nudge first (same session,
    targeted "stop planning, write in smaller pieces" prompt), the fresh
    retry only once the continue budget is spent.
  * maxtime     -> unchanged (ABORT_MAXTIME, retry).
  * "same session" is real: run_acp_once resumes the previous attempt's
    session with ACP `session/load` (omp advertises loadSession; the chat
    daemon resumes the same way). Every attempt spawns a new `omp acp`
    child, so the session id is the only thing that survives — before this
    change every continue-nudge landed in a FRESH session that had never seen
    the task. A failed load falls back to a fresh session with the full task
    prompt + nudge (never a bare nudge without the task).

Run: pytest test_acp_output_cap_continue.py -q  (in docker/omp-bridge/tests/)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import bridge  # noqa: E402
import test_acp_adapter as taa  # noqa: E402

SID = "01a0814f-a62d-76a6-8e8e-3e17841eb215"


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def _ended(stop_reason: str, text: str = "") -> bridge.RunOutcome:
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_end = True
    o.model_worked = True
    o.final_stop_reason = stop_reason
    o.final_text = text
    o.session_id = SID
    return o


def test_max_tokens_classifies_output_cap_not_maxtime():
    c = bridge.classify_acp(_ended("max_tokens"))
    assert c.kind is bridge.Kind.ABORT_OUTPUT_CAP
    assert c.reason == "output_cap"
    # continueable first, retryable as the fallback
    assert bridge.Kind.ABORT_OUTPUT_CAP in bridge.CONTINUEABLE_KINDS
    assert c.retryable is True


def test_wall_clock_maxtime_keeps_old_classification():
    c = bridge.classify_acp(_ended("maxtime"))
    assert c.kind is bridge.Kind.ABORT_MAXTIME
    assert c.reason == "maxtime_cutoff"


def test_output_cap_nudge_is_targeted_english():
    nudge = bridge._nudge_prompt_for(bridge.Kind.ABORT_OUTPUT_CAP, 0)
    assert "output limit" in nudge
    assert "smaller" in nudge
    # second nudge for the same kind stays the targeted one (it is not a
    # format problem -> no copy-paste reflection template)
    assert bridge._nudge_prompt_for(bridge.Kind.ABORT_OUTPUT_CAP, 1) == nudge


# ---------------------------------------------------------------------------
# lifecycle (drive_live_run)
# ---------------------------------------------------------------------------

class _Life(bridge.MCLifecycle):
    def __init__(self):
        self.comments: list[str] = []
        self.calls: list[tuple] = []

    def ack(self, task_id):
        self.calls.append(("ack",))

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish",))

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", question))

    def comment(self, task_id, text):
        self.comments.append(text)

    def task_is_active(self, task_id):
        return None


FINISH_TEXT = (
    "done\n\n"
    + "\n".join(f"{h}\nWrote the page in three smaller edits, as the nudge asked." for h in bridge.REFLECTION_HEADERS)
    + "\n" + bridge.SENTINEL
)


def test_output_cap_continues_in_same_session_before_any_retry():
    life = _Life()
    fresh_runs: list[int] = []
    nudges: list[str] = []

    def run_once():
        fresh_runs.append(1)
        return _ended("max_tokens")

    def continue_once(nudge):
        nudges.append(nudge)
        return _ended("end_turn", FINISH_TEXT)

    action = bridge.drive_live_run(
        life, run_once, task_id="T", board_requires_review=False,
        retries_left=2, pre_acked=True, continues_left=2,
        continue_once=continue_once, classify_fn=bridge.classify_acp,
    )
    assert len(fresh_runs) == 1, "output cap must not relaunch a fresh session"
    assert len(nudges) == 1 and "output limit" in nudges[0]
    assert action.action == "finish"
    assert any("continued in same session (output cap)" in c for c in life.comments), life.comments
    assert not any("retrying" in c for c in life.comments), life.comments


def test_output_cap_falls_back_to_fresh_retry_when_continues_spent():
    life = _Life()
    outcomes = iter([_ended("max_tokens"), _ended("max_tokens"), _ended("end_turn", FINISH_TEXT)])
    fresh_runs: list[int] = []

    def run_once():
        fresh_runs.append(1)
        return next(outcomes)

    def continue_once(nudge):
        return next(outcomes)

    action = bridge.drive_live_run(
        life, run_once, task_id="T", board_requires_review=False,
        retries_left=2, pre_acked=True, continues_left=1,
        continue_once=continue_once, classify_fn=bridge.classify_acp,
    )
    assert action.action == "finish"
    assert len(fresh_runs) == 2, "continue spent -> exactly one fresh retry"
    assert any("continued in same session (output cap)" in c for c in life.comments)
    assert any("retrying in a fresh session" in c for c in life.comments), life.comments


def test_output_cap_without_continue_executor_retries_not_blocks():
    """A path with no live session to continue (continue_once=None) keeps the
    old retry instead of collapsing a continueable kind into a blocker."""
    life = _Life()
    outcomes = iter([_ended("max_tokens"), _ended("end_turn", FINISH_TEXT)])
    action = bridge.drive_live_run(
        life, lambda: next(outcomes), task_id="T", board_requires_review=False,
        retries_left=2, pre_acked=True, continues_left=2,
        continue_once=None, classify_fn=bridge.classify_acp,
    )
    assert action.action == "finish", (action.action, life.calls)


# ---------------------------------------------------------------------------
# run_acp_once: resume via session/load
# ---------------------------------------------------------------------------

def _fixture_lines() -> list[dict]:
    return [json.loads(l) for l in taa.FIXTURES["normal"].read_text().splitlines() if l.strip()]


def _write_fixture(msgs: list[dict]) -> Path:
    fd, path = tempfile.mkstemp(suffix=".ndjson", prefix="acp-resume-")
    with os.fdopen(fd, "w") as fh:
        for m in msgs:
            fh.write(json.dumps(m) + "\n")
    return Path(path)


def _update(su: str, **extra) -> dict:
    upd = {"sessionUpdate": su, **extra}
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": SID, "update": upd}}


def _run(fixture: Path, **kw):
    wire: list = []
    fake = taa.InProcessFake(fixture, wire)
    transcript: list[str] = []
    try:
        outcome = bridge.run_acp_once(
            "continue please", cwd=str(HERE), model=None, max_time=10,
            permission_policy="yolo", task_id="T1", cancel_poll_interval=0.01,
            client_factory=lambda: fake.client,
            transcript_sink=lambda lines: transcript.extend(lines),
            **kw,
        )
    finally:
        fake.close()
    return outcome, wire, transcript


def _client_requests(wire: list) -> list[dict]:
    return [m["__client"] for m in wire if "__client" in m and "method" in m["__client"]]


def test_resume_loads_the_previous_session_and_mutes_its_replay():
    init = _fixture_lines()[0]
    msgs = [
        init,
        # omp replays the loaded session's history BEFORE answering the load
        _update("agent_thought_chunk", content={"type": "text", "text": "OLD THOUGHT"}),
        _update("agent_message_chunk", content={"type": "text", "text": "OLD REPLY"}),
        {"jsonrpc": "2.0", "id": 2, "result": {"configOptions": []}},
        _update("agent_message_chunk", content={"type": "text", "text": "new work"}),
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn",
                                               "usage": {"totalTokens": 10}}},
    ]
    outcome, wire, transcript = _run(_write_fixture(msgs), resume_session_id=SID,
                                     fresh_prompt="FULL TASK PROMPT\n\ncontinue please")
    methods = [r["method"] for r in _client_requests(wire)]
    assert "session/load" in methods and "session/new" not in methods, methods
    load = next(r for r in _client_requests(wire) if r["method"] == "session/load")
    assert load["params"]["sessionId"] == SID
    prompt = next(r for r in _client_requests(wire) if r["method"] == "session/prompt")
    assert prompt["params"]["sessionId"] == SID
    assert "FULL TASK PROMPT" not in prompt["params"]["prompt"][0]["text"]
    assert outcome.session_id == SID
    assert outcome.resumed is True
    assert outcome.final_stop_reason == "end_turn"
    # the replay is history, not this turn's work
    assert "OLD" not in outcome.final_text, outcome.final_text
    assert not any("OLD" in line for line in transcript), transcript


def test_resume_failure_falls_back_to_fresh_session_with_full_prompt():
    lines = _fixture_lines()
    init, new_reply = lines[0], dict(lines[1])
    new_reply = {**new_reply, "id": 3}
    msgs = [
        init,
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32002, "message": "Session not found"}},
        new_reply,
        _update("agent_message_chunk", content={"type": "text", "text": "fresh"}),
        {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}},
    ]
    outcome, wire, _ = _run(_write_fixture(msgs), resume_session_id="gone-session",
                            fresh_prompt="FULL TASK PROMPT\n\ncontinue please")
    reqs = _client_requests(wire)
    methods = [r["method"] for r in reqs]
    assert methods.index("session/load") < methods.index("session/new"), methods
    prompt = next(r for r in reqs if r["method"] == "session/prompt")
    assert "FULL TASK PROMPT" in prompt["params"]["prompt"][0]["text"]
    assert outcome.resumed is False
    assert "Session not found" in (outcome.resume_error or "")
    assert outcome.final_stop_reason == "end_turn"


def test_resume_non_acp_error_also_falls_back():
    """Any load failure (not only ACPError) takes the fresh-session fallback
    instead of ending the attempt as a launch error (review finding)."""
    fake = taa.InProcessFake(taa.FIXTURES["normal"], [])

    def boom(*_a, **_k):
        raise RuntimeError("load exploded")

    fake.client.load_session = boom
    try:
        outcome = bridge.run_acp_once(
            "nudge", cwd=str(HERE), model=None, max_time=10,
            permission_policy="yolo", task_id="T1", cancel_poll_interval=0.01,
            client_factory=lambda: fake.client,
            resume_session_id="whatever", fresh_prompt="FULL TASK PROMPT",
        )
    finally:
        fake.close()
    assert outcome.resumed is False
    assert "load exploded" in (outcome.resume_error or "")
    # the fallback opened a fresh session (not a launch-preflight failure)
    assert outcome.saw_session is True
    assert outcome.session_id == SID


def test_no_resume_requested_keeps_session_new():
    outcome, _sink = taa.run_adapter(taa.FIXTURES["normal"])
    assert outcome.resumed is None
    assert outcome.final_stop_reason == "end_turn"


def test_drive_live_run_reports_a_failed_resume():
    life = _Life()

    def continue_once(nudge):
        o = _ended("end_turn", FINISH_TEXT)
        o.resumed = False
        o.resume_error = "Session not found"
        return o

    bridge.drive_live_run(
        life, lambda: _ended("max_tokens"), task_id="T", board_requires_review=False,
        retries_left=0, pre_acked=True, continues_left=1,
        continue_once=continue_once, classify_fn=bridge.classify_acp,
    )
    assert any("could not be resumed" in c for c in life.comments), life.comments


# ---------------------------------------------------------------------------
# serve_loop wiring: the continue hands the previous session id on
# ---------------------------------------------------------------------------

def test_serve_loop_continue_resumes_previous_attempt_session():
    import test_acp_context_session_reset as tcr

    saved = {k: os.environ.get(k) for k in tcr._SERVE_ENV_KEYS}
    calls: list[tuple[str, dict]] = []
    life = _Life()
    try:
        with tempfile.TemporaryDirectory(prefix="output-cap-serve-") as td:
            tmp = Path(td)
            os.environ.update({
                "PI_CODING_AGENT_DIR": str(tmp / "agent"),
                "OMP_DRIVER": "acp",
                "OMP_ACP_CWD": str(HERE),
                "OMP_ACP_PERMISSIONS": "yolo",
                "MSG_DELIVERY_MODE": "nudge",
                "OMP_HOME": str(tmp / "home"),
                "OMP_TURN_SIGNAL_FILE": str(tmp / "turn-signal.ndjson"),
                "OMP_TASK_LOCK_FILE": str(tmp / "task.lock"),
                "OMP_MSG_QUEUE_DIR": str(tmp / "msg-q"),
                "OMP_MSG_ACK_DIR": str(tmp / "msg-ack"),
                "OMP_MSG_NUDGE_STATE_FILE": str(tmp / "nudge-state"),
                "OMP_MSG_NUDGE_MSG_FILE": str(tmp / "nudge-msg"),
                "OMP_MAX_RETRIES": "2",
                "OMP_MAX_CONTINUES": "2",
            })
            os.environ.pop("OMP_TASK_DEADLINE", None)
            os.environ.pop("OMP_PROFILE", None)
            (tmp / "agent").mkdir()
            outcomes = iter([_ended("max_tokens"), _ended("end_turn", FINISH_TEXT)])

            orig = bridge.run_acp_once

            def spy(prompt, **kw):
                calls.append((prompt, kw))
                o = next(outcomes)
                if kw.get("on_first_activity"):
                    kw["on_first_activity"]()
                return o

            bridge.run_acp_once = spy
            polls = iter([{"state": "new_task",
                           "task": {**tcr._SERVE_TASK, "workspace_path": str(HERE)}}])
            try:
                bridge.serve_loop(
                    poll_interval=0, max_iterations=1,
                    _poll_fn=lambda: next(polls, {"state": "idle"}),
                    _lifecycle_factory=lambda task: life,
                    _run_factory=None, _sleep=lambda _s: None,
                    _context_env_path=str(tmp / "mc-context.env"),
                )
            finally:
                bridge.run_acp_once = orig
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert len(calls) == 2, [c[0][:40] for c in calls]
    first_kw, second_prompt, second_kw = calls[0][1], calls[1][0], calls[1][1]
    assert not first_kw.get("resume_session_id")
    assert second_kw.get("resume_session_id") == SID, second_kw.get("resume_session_id")
    assert "output limit" in second_prompt
    # the fallback for a failed load carries the task, not a bare nudge
    assert "Do the thing." in (second_kw.get("fresh_prompt") or "")
    assert ("finish",) in life.calls, life.calls
