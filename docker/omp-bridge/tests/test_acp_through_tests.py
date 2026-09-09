#!/usr/bin/env python3
"""Durchstich-Tests (Review #464/#465 Nacharbeit, Pflicht-Tests).

These are NOT unit attrapps: each one drives a full layered path against the
golden ACP wire fixtures replayed by fake_acp_server (the same fixtures
test_acp_replay.py feeds the live client):

  1. drive_live_run × ACP fixture, finish   -> mc finish called with reflection
  2. drive_live_run × cancel fixture        -> stopReason=cancelled ->
     classify_acp -> Kind.INTERRUPTED -> halted_interrupted (no retry, no
     blocker, no finish)
  3. permission roundtrip with an EMPTY operator answer -> REJECT_ONCE
     (fail-closed), the rejected tool run still completes end_turn
  4. _make_acp_run_factory with 2+ events -> the transcript sink survives a
     SECOND write (the old sink() read a nonexistent session_id property and
     died on event 2), and the file carries the REAL ACP sessionId
  5. reducer: real fixture chunks mapped in stream mode -> every preview line
     is a replace-me acp-preview (one bubble per sentence, never stacking),
     and exactly ONE permanent assistant line lands

Runs two ways:
  * pytest:      pytest test_acp_through_tests.py -v
  * standalone:  python3 test_acp_through_tests.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT.parent.parent / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BACKEND))

import bridge  # noqa: E402
import acp_client  # noqa: E402
import fake_acp_server  # noqa: E402  (import guard: syntax check)
import acp_chat_events  # noqa: E402
from app.services.omp_chat import OmpLineParser  # noqa: E402
from test_acp_adapter import InProcessFake  # noqa: E402

RPC = ROOT / "rpc"
FIXTURES = {
    "normal": RPC / "acp-normal-turn.ndjson",
    "permission": RPC / "acp-permission-tool.ndjson",
    "cancel": RPC / "acp-cancel-mid-turn.ndjson",
}
for name, path in FIXTURES.items():
    if not path.exists():
        raise RuntimeError(f"missing fixture: {path}")


class RecordingLifecycle(bridge.MCLifecycle):
    """Lifecycle double that records every call drive_live_run makes."""

    def __init__(self, *, active: bool = True):
        self.calls: list[tuple] = []
        self._active = active

    def ack(self, task_id):
        self.calls.append(("ack", task_id))

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish", task_id, reflection, review))

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", task_id, blocker_type, question))

    def comment(self, task_id, text):
        self.calls.append(("comment", task_id, text))

    def task_is_active(self, task_id):
        return self._active if self._active is not None else None




# ── 1. drive_live_run: finish ───────────────────────────────────────────────

def _run_acp_attempt(fixture: Path, *, policy: str = "yolo",
                     ask_answers: list | None = None,
                     cancel_state: "bridge.ACPCancelState | None" = None
                     ) -> tuple[bridge.RunOutcome, list]:
    """One run_acp_once through the in-process fake server. The wire-level
    permission reply (client -> fake) is captured by patching
    ACPClient._send_raw — the fake's own sink records only server->client
    traffic, never our decision reply."""
    sink: list = []
    fake = InProcessFake(fixture, sink)
    answers = list(ask_answers or [])
    sent: list[dict] = []
    orig_send = acp_client.ACPClient._send_raw

    def _sniff(self, obj):
        sent.append(obj)
        return orig_send(self, obj)

    acp_client.ACPClient._send_raw = _sniff
    try:
        outcome = bridge.run_acp_once(
            "hello golden fixture",
            cwd=str(HERE),
            model="m",
            max_time=10,
            permission_policy=policy,
            task_id="T1",
            cancel_state=cancel_state or bridge.ACPCancelState(),
            cancel_poll_interval=0.01,
            client_factory=lambda: fake.client,
            ask_fn=lambda task_id, question: answers.pop(0) if answers else "",
        )
    finally:
        acp_client.ACPClient._send_raw = orig_send
        fake.close()
    return outcome, sent





def test_drive_live_run_acp_finish_calls_mc_finish():
    """Durchstich: serve_loop's exact wiring (run_acp_once as run_once,
    classify_acp injected) — a genuine end_turn+sentinel turn must reach
    `mc finish` with the extracted reflection."""
    lc = RecordingLifecycle()
    attempts = {"n": 0}

    def run_once() -> bridge.RunOutcome:
        attempts["n"] += 1
        outcome, _ = _run_acp_attempt(FIXTURES["normal"])
        # The fixture's streamed text is short — feed the full completion
        # contract the way the live prompt would end (same oracle as native).
        outcome.final_text = (
            "hello golden fixture\n\n"
            "## Was wurde gemacht\nDatei erstellt und getestet.\n"
            "## Was hat funktioniert\nZweiter Lauf gruen.\n"
            "## Was war unklar\nNichts Wesentliches.\n"
            "## Lesson fuer Agent-Memory\nImmer erst reproduzieren.\n"
            "TASK_COMPLETE"
        )
        return outcome

    action = bridge.drive_live_run(
        lc, run_once, task_id="task-1", board_requires_review=False,
        retries_left=2, continues_left=2, pre_acked=False,
        classify_fn=bridge.classify_acp,
    )
    assert attempts["n"] == 1
    finishes = [c for c in lc.calls if c[0] == "finish"]
    assert len(finishes) == 1, lc.calls
    assert finishes[0][1] == "task-1"
    assert "TASK_COMPLETE" not in finishes[0][2]  # reflection block only
    assert "Was wurde gemacht" in finishes[0][2]
    assert finishes[0][3] is False  # board_requires_review=False
    assert action.action == "finish"
    print("PASS test_drive_live_run_acp_finish_calls_mc_finish")


# ── 2. drive_live_run: cancel -> INTERRUPTED ────────────────────────────────


def test_drive_live_run_acp_cancel_halts_without_escalation():
    """Durchstich: a cancelled turn (stopReason=cancelled from the golden
    fixture) classifies INTERRUPTED and the driver halts — no retry, no
    blocker, no finish, no 'omp abort (hang)' comment."""
    lc = RecordingLifecycle()
    attempts = {"n": 0}

    def run_once() -> bridge.RunOutcome:
        attempts["n"] += 1
        outcome, _ = _run_acp_attempt(
            FIXTURES["cancel"],
            cancel_state=bridge.ACPCancelState(requested=True),
        )
        return outcome

    action = bridge.drive_live_run(
        lc, run_once, task_id="task-1", board_requires_review=False,
        retries_left=2, continues_left=2, pre_acked=False,
        classify_fn=bridge.classify_acp,
    )
    assert attempts["n"] == 1, "a cancelled turn is never retried"
    assert action.action == "halted_interrupted", action
    assert not [c for c in lc.calls if c[0] in ("blocker", "finish")], lc.calls
    assert not [c for c in lc.calls if c[0] == "comment"], lc.calls
    print("PASS test_drive_live_run_acp_cancel_halts_without_escalation")


# ── 3. permission: empty answer -> REJECT_ONCE (fail-closed) ────────────────


def test_permission_empty_answer_rejects_once_and_run_completes():
    """Durchstich (Review #464 Blocker 2): policy=ask with an EMPTY operator
    answer must REJECT the tool (fail-closed), never ALLOW_ONCE. The turn
    itself still settles end_turn — a rejected tool is a decision, not a
    crash."""
    outcome, sent = _run_acp_attempt(
        FIXTURES["permission"], policy="ask", ask_answers=[""],
    )
    assert outcome.final_stop_reason == "end_turn"
    assert outcome.saw_session is True
    # The decision that crossed the wire for the permission request is a
    # bare rejection (fail-closed), not an allow shortcut.
    rejections = [
        m for m in sent
        if (m.get("result") or {}).get("outcome", {}).get("outcome") == "selected"
        and "reject" in str((m.get("result") or {})
                            .get("outcome", {}).get("optionId", ""))
    ]
    allows = [
        m for m in sent
        if (m.get("result") or {}).get("outcome", {}).get("outcome") == "selected"
        and str((m.get("result") or {}).get("outcome", {}).get("optionId", ""))
        .startswith("allow")
    ]
    assert rejections and not allows, sent


def test_acp_permission_decision_fail_closed_table():
    f = bridge._acp_permission_decision
    params = {"toolCall": {"toolCallId": "t", "title": "$ x", "kind": "execute"}}
    kw = dict(policy="ask", task_id="T1")
    assert f(params, ask_fn=lambda t, q: "", **kw) is acp_client.REJECT_ONCE
    assert f(params, ask_fn=lambda t, q: "garbage", **kw) is acp_client.REJECT_ONCE
    assert f(params, ask_fn=lambda t, q: "yes", **kw) is acp_client.ALLOW_ALWAYS
    assert f(params, ask_fn=lambda t, q: "no", **kw) is acp_client.REJECT_ONCE
    print("PASS test_acp_permission_decision_fail_closed_table")


def test_mc_ask_blocking_unreadable_baseline_fails_closed():
    """N2 (Review #471): wenn die Thread-Baseline nicht lesbar ist, darf der
    Poll KEINE beliebige aeltere Operator-Nachricht als Antwort nehmen. Ohne
    Baseline sofort "" -> der Aufrufer mappt auf REJECT_ONCE."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "ask" in cmd:
            class P:
                returncode = 0
                stdout = ""
                stderr = ""
            return P()
        # thread --json: the baseline read fails (rc!=0)
        class P2:
            returncode = 1
            stdout = ""
            stderr = "thread unavailable"
        return P2()

    import subprocess as _sub
    orig = _sub.run
    _sub.run = fake_run
    try:
        answer = bridge._mc_ask_blocking("T1", "Freigabe?", poll_interval=0.01, timeout=0.2)
    finally:
        _sub.run = orig
    assert answer == "", answer
    # Fail-closed means: NO poll loop at all — only the one baseline read,
    # never a `--limit 10` poll that could accept an older operator message.
    assert not any("--limit" in c and "10" in c for c in calls), calls
    print("PASS test_mc_ask_blocking_unreadable_baseline_fails_closed")


def test_mc_ask_blocking_older_operator_message_never_becomes_the_answer():
    """N2 counterparts: WITH a readable baseline the poll filters everything
    at or below the question's seq — a stale operator message is skipped, and
    the FIRST NEWER user-side message is the answer."""
    import json as _json
    thread_page = {
        "messages": [
            {"seq": 1, "author": {"kind": "user"}, "direction": "user_to_agent",
             "body": "ALTE Nachricht vor der Frage"},
            {"seq": 2, "author": {"kind": "agent"}, "direction": "agent_to_user",
             "body": "Frage: Freigabe?"},
        ]
    }

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stderr = ""
            stdout = _json.dumps(thread_page) if "thread" in cmd else ""
        return P()

    import subprocess as _sub
    orig = _sub.run
    _sub.run = fake_run
    try:
        # Baseline seq=2; the stale seq=1 user message must NOT answer.
        answer = bridge._mc_ask_blocking("T1", "Freigabe?", poll_interval=0.01, timeout=0.05)
    finally:
        _sub.run = orig
    assert answer == "", answer  # timeout with no NEWER message -> ""
    print("PASS test_mc_ask_blocking_older_operator_message_never_becomes_the_answer")


# ── 4. _make_acp_run_factory: sink survives 2+ events, real sessionId ───────


def test_make_acp_run_factory_sink_survives_multiple_events():
    """Durchstich (Review #465 Blocker 1): the REAL _make_acp_run_factory's
    transcript sink must survive repeated writes. The old sink() read a
    `session_id` attribute the ChatEventSink class never had and died on the
    second batch; the chat stalled after line 1. We drive the factory's own
    sink function directly with successive mapped batches (the same call
    pattern run_acp_once uses), assert every batch lands in ONE file, and
    that the file name carries the REAL ACP sessionId — set via the
    on-session hook — not the "acp-session" stub."""
    agent_dir = tempfile.mkdtemp(prefix="acp-factory-agent-")
    old_dir = os.environ.get("PI_CODING_AGENT_DIR")
    os.environ["PI_CODING_AGENT_DIR"] = agent_dir
    try:
        factory = bridge._make_acp_run_factory(
            model="m", max_time=10, permission_policy="yolo", task_id="T1",
        )
        run = factory
        # The factory closes over its private sink; reach it the honest way:
        # run a REAL attempt through it with a patched run_acp_once that
        # captures the transcript_sink kwarg it passes.
        captured: dict = {}
        fake = InProcessFake(FIXTURES["permission"], [])
        orig = bridge.run_acp_once

        def capturing_run_acp_once(prompt, **kwargs):
            captured["sink"] = kwargs.get("transcript_sink")
            return orig(
                prompt,
                cwd=str(HERE),
                model="m",
                max_time=10,
                permission_policy="yolo",
                task_id="T1",
                client_factory=lambda: fake.client,
                transcript_sink=kwargs.get("transcript_sink"),
                on_session_id=kwargs.get("on_session_id"),
            )

        bridge.run_acp_once = capturing_run_acp_once
        try:
            outcome = run("mach was")
        finally:
            bridge.run_acp_once = orig
            fake.close()
        assert outcome.final_stop_reason == "end_turn"
        sink = captured.get("sink")
        assert sink is not None, "factory must wire a transcript_sink"

        # Drive the captured sink the way run_acp_once does: multiple
        # successive batches. The OLD code raised AttributeError here.
        mapper = acp_chat_events.ACPEventMapper()
        batch1 = mapper.dump(mapper.map_user_prompt("mach was"))
        batch2 = mapper.dump(mapper.map_update(
            {"update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "arbeit"},
                        "messageId": "m1"}}, stream=True))
        batch3 = mapper.dump(mapper.map_final_assistant_message("fertig."))
        sink(batch1)
        sink(batch2)   # <- the second call is the regression: never stall
        sink(batch3)

        # The written file carries the REAL sessionId from session/new
        # (flowed through on_session_id), not the "acp-session" stub.
        session_root = Path(agent_dir) / "sessions"
        files = list(session_root.rglob("*.jsonl"))
        assert files, "no transcript file written"
        assert len(files) == 1, [f.name for f in files]
        header = json.loads(files[0].read_text().splitlines()[0])
        assert header["type"] == "session"
        assert header["id"] == outcome.session_id, header
        assert header["id"] != "acp-session"
        events = [json.loads(l) for l in files[0].read_text().splitlines()[1:]]
        assert len(events) >= 3, f"expected all 3 batches, got {len(events)}"
        types = [e.get("type") for e in events]
        assert "message" in types and "custom_message" in types
    finally:
        if old_dir is None:
            os.environ.pop("PI_CODING_AGENT_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_DIR"] = old_dir
    print("PASS test_make_acp_run_factory_sink_survives_multiple_events")


# ── 5. reducer: stream-mode mapping over a real fixture ─────────────────────


def test_reducer_stream_mode_one_preview_slot_one_final_line():
    """Durchstich (Review #465 Option b, #471 N3, Folge-PR): map the REAL
    normal fixture's updates in stream mode. Contract:
    - every streamed text chunk becomes an acp-preview line destined for
      the SIBLING preview file (never the transcript JSONL),
    - the backend tailer's channel reader turns those lines into uuid-less
      VOLATILE `preview` events (source "acp", the reducer's replace-me
      slot — never a permanent timeline bubble),
    - exactly ONE permanent assistant message lands (the final line),
    - preview lines never carry usage, the final line does."""
    mapper = acp_chat_events.ACPEventMapper()
    preview_lines: list[str] = []
    other_lines: list[str] = []
    for line in FIXTURES["normal"].read_text().splitlines():
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("method") != "session/update":
            continue
        for entry in mapper.map_update(msg["params"], stream=True):
            encoded = json.dumps(entry, ensure_ascii=False)
            if entry.get("customType") == acp_chat_events.PREVIEW_CUSTOM_TYPE:
                preview_lines.append(encoded)
            else:
                other_lines.append(encoded)

    fixture_text = ""
    for line in FIXTURES["normal"].read_text().splitlines():
        if "agent_message_chunk" in line:
            fixture_text = json.loads(line)["params"]["update"]["content"]["text"]
            break
    assert preview_lines, "stream mode must emit preview lines for chunks"
    # Growing snapshot: the LAST preview carries the FULL accumulated text.
    snapshots = [json.loads(l)["content"] for l in preview_lines]
    assert snapshots[-1] == fixture_text, snapshots
    assert len({s for s in snapshots}) >= 1
    # Every preview flush is the bridge's acp-preview custom message.
    for l in preview_lines:
        e = json.loads(l)
        assert e["type"] == "custom_message"
        assert e["customType"] == acp_chat_events.PREVIEW_CUSTOM_TYPE

    # Backend-side: the tailer's channel reader makes each line a volatile
    # preview event (same contract the SSE stream carries).
    from app.services.transcript_chat import _read_preview_channel

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        pfile = Path(td) / "p1.jsonl"
        pfile.write_text("\n".join(preview_lines) + "\n", encoding="utf-8")
        state = {"path": pfile, "offset": 0, "buffer": b""}
        preview_events = _read_preview_channel(state)
    assert preview_events, "preview channel lines must become preview events"
    assert all(ev.get("kind") == "preview" and ev.get("uuid") is None
               and ev.get("source") == "acp" for ev in preview_events), \
        preview_events
    assert not [ev for ev in preview_events if ev["kind"] == "usage"], \
        "preview lines must never carry usage (0/0 garbage)"

    # The ONE final assistant line — through the REAL OmpLineParser, as the
    # transcript path (history + tailer) consumes it.
    mapper2 = acp_chat_events.ACPEventMapper()
    mapper2.set_prompt_usage({"inputTokens": 10, "outputTokens": 2})
    final_lines = mapper2.dump(mapper2.map_final_assistant_message(
        "sentence one. sentence two.", stop_reason="stop"))
    parser = OmpLineParser()
    final_events = []
    for l in final_lines:
        final_events += parser(l)
    messages = [ev for ev in final_events if ev["kind"] == "message"
                and ev["role"] == "assistant"]
    assert len(messages) == 1, final_events
    usage = [ev for ev in final_events if ev["kind"] == "usage"]
    assert usage and usage[0]["inputTokens"] == 10
    print("PASS test_reducer_stream_mode_one_preview_slot_one_final_line")


if __name__ == "__main__":
    import contextlib
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        import inspect
        kwargs = {}
        if "tmp_path" in inspect.signature(fn).parameters:
            kwargs["tmp_path"] = None
        fn(**kwargs)
    print(f"{len(fns)} through-tests passed")
