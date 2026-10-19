#!/usr/bin/env python3
"""Delivered is not working — the task claim waits for the model (19.10.2026).

Operator finding (live, task 4de17c6a): the bridge PATCHed ``in_progress``
4 s after the poll claim, before the model had done anything — the DGX Spark
on :8000 was switched off, nothing could ever answer, yet the card said
"in Arbeit". The serve loop called ``lifecycle.ack()`` up front, before
``drive_live_run``.

Contract under test (ACP driver, the live one):

1. ``run_acp_once`` reports the model's FIRST sign of life (first text/thought
   chunk or first tool call) through ``on_first_activity`` — exactly once, and
   never for a session that opened but produced nothing.
2. ``drive_live_run`` with an ``ack_gate`` claims the task only after the gate
   fired. A run whose model never answered collapses to a blocker WITHOUT a
   claim — and without any comment before the blocker, because an agent
   comment on an unclaimed card is itself an implicit ACK
   (``apply_ack_handshake``: inbox -> in_progress).
3. The full serve loop on the ACP branch wires the gate: no up-front ack;
   model dead (unreachable runtime) -> never ``ack``, terminal blocker.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import bridge  # noqa: E402
from test_acp_adapter import InProcessFake  # noqa: E402

RPC = ROOT / "rpc"
NORMAL = RPC / "acp-normal-turn.ndjson"
SILENT = RPC / "acp-silent-cancel.ndjson"
UNREACHABLE = RPC / "acp-model-unreachable.ndjson"
# Live shape (task 4de17c6a, Spark :8000 off): omp retried for ~7 min, then
# sent its error as ONE message chunk and ended the turn "refusal".
ECONNREFUSED = RPC / "acp-model-econnrefused.ndjson"

_REFLECTION = (
    "\n\n## Was wurde gemacht\nDer Lauf hat die Aufgabe vollstaendig erledigt.\n"
    "## Was hat funktioniert\nDas Modell antwortete sofort und sauber.\n"
    "## Was war unklar\nNichts Wesentliches, die Aufgabe war eindeutig.\n"
    "## Lesson fuer Agent-Memory\nErst arbeiten, dann melden.\n"
    "TASK_COMPLETE"
)


class Recording(bridge.MCLifecycle):
    def __init__(self):
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def _rec(self, *c):
        with self._lock:
            self.calls.append(c)

    def ack(self, task_id):
        self._rec("ack", task_id)

    def finish(self, task_id, reflection, *, review):
        self._rec("finish", task_id)

    def set_blocker(self, task_id, *, blocker_type, question):
        self._rec("blocker", task_id, blocker_type, question)

    def comment(self, task_id, text):
        self._rec("comment", task_id, text)

    def task_is_active(self, task_id):
        return None

    def kinds(self):
        return [c[0] for c in self.calls]


def _run(fixture: Path, on_first_activity) -> bridge.RunOutcome:
    fake = InProcessFake(fixture, [])
    try:
        return bridge.run_acp_once(
            "hello golden fixture", cwd=str(HERE), model="m", max_time=10,
            permission_policy="yolo", task_id="T1",
            cancel_state=bridge.ACPCancelState(), cancel_poll_interval=0.01,
            client_factory=lambda: fake.client,
            on_first_activity=on_first_activity,
        )
    finally:
        fake.close()


# ── 1. run_acp_once reports the first sign of life ─────────────────────────

def test_first_activity_fires_once_when_the_model_answered():
    hits: list[int] = []
    outcome = _run(NORMAL, lambda: hits.append(1))
    assert outcome.saw_session and outcome.model_worked
    assert hits == [1], hits


def test_omp_error_chunk_is_not_model_work():
    """The exact live shape: one chunk with omp's connection error + stopReason
    'refusal'. Must not claim — the model never ran."""
    hits: list[int] = []
    outcome = _run(ECONNREFUSED, lambda: hits.append(1))
    assert outcome.saw_session
    assert "ECONNREFUSED" in outcome.final_text
    assert outcome.final_stop_reason == "refusal"
    assert outcome.model_worked is False
    assert hits == []


def test_first_activity_never_fires_for_a_session_without_model_output():
    """Session opened (omp is up), prompt ended with nothing from the model."""
    hits: list[int] = []
    outcome = _run(SILENT, lambda: hits.append(1))
    assert outcome.saw_session
    assert hits == []


def test_first_activity_never_fires_when_the_model_endpoint_is_down():
    hits: list[int] = []
    outcome = _run(UNREACHABLE, lambda: hits.append(1))
    assert outcome.saw_session
    assert outcome.error_message, "the prompt error must land on the outcome"
    assert hits == []


def test_a_failing_callback_never_breaks_the_run():
    def boom():
        raise RuntimeError("mc down")

    outcome = _run(NORMAL, boom)
    assert outcome.final_stop_reason == "end_turn"


# ── 2. drive_live_run claims only after the gate fired ─────────────────────

def _dead_model_outcome() -> bridge.RunOutcome:
    o = bridge.RunOutcome()
    o.saw_session = True
    o.error_message = "Internal error: Connection error."
    return o


def test_dead_model_is_blocked_without_ever_claiming():
    lc = Recording()
    gate = bridge.FirstActivityAck(lc, "T1")
    action = bridge.drive_live_run(
        lc, _dead_model_outcome, task_id="T1", board_requires_review=False,
        retries_left=2, ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert action.action == "blocker", action
    assert "ack" not in lc.kinds(), lc.calls
    # No agent comment may precede the blocker: a comment on an unclaimed
    # card is an implicit ACK in the backend (apply_ack_handshake).
    assert lc.kinds()[0] == "blocker", lc.calls
    # The retry notes and the classification are not lost — they follow
    # the blocker (the card is no longer inbox then).
    assert lc.kinds().count("comment") >= 3, lc.calls
    question = lc.calls[0][3]
    assert "Connection error" in question, question


def test_model_answered_claims_before_finishing():
    lc = Recording()
    gate = bridge.FirstActivityAck(lc, "T1")

    def run_once() -> bridge.RunOutcome:
        gate.trigger()  # what run_acp_once's on_first_activity does
        o = bridge.RunOutcome()
        o.saw_session = o.saw_agent_start = o.saw_agent_end = True
        o.final_stop_reason = "end_turn"
        o.final_text = "done" + _REFLECTION
        return o

    action = bridge.drive_live_run(
        lc, run_once, task_id="T1", board_requires_review=False,
        retries_left=1, ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert action.action == "finish", (action, lc.calls)
    assert lc.kinds() == ["ack", "finish"], lc.calls


def test_gate_acks_at_most_once_across_threads():
    lc = Recording()
    gate = bridge.FirstActivityAck(lc, "T1")
    threads = [threading.Thread(target=gate.trigger) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert gate.wait(timeout=5)
    assert lc.kinds() == ["ack"]


def test_output_without_a_gate_signal_still_claims_before_terminal():
    """Belt: the model worked (model_worked) but the callback was lost — the
    claim must still happen before finish (mc finish requires in_progress)."""
    lc = Recording()
    gate = bridge.FirstActivityAck(lc, "T1")

    def run_once() -> bridge.RunOutcome:
        o = bridge.RunOutcome()
        o.saw_session = o.saw_agent_start = o.saw_agent_end = True
        o.model_worked = True
        o.final_stop_reason = "end_turn"
        o.final_text = "done" + _REFLECTION
        return o

    bridge.drive_live_run(
        lc, run_once, task_id="T1", board_requires_review=False,
        retries_left=0, ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert lc.kinds() == ["ack", "finish"], lc.calls


class OwnershipReading(Recording):
    """Lifecycle with #881's real ownership read: ``task_state`` +
    ``own_agent_id`` and the production ``task_is_active`` rule
    (in_progress/waiting + mine)."""

    _ACTIVE_STATUSES = bridge.McCliLifecycle._ACTIVE_STATUSES

    def __init__(self, status: str, assigned: str = "ME"):
        super().__init__()
        self.status = status
        self.assigned = assigned

    def task_state(self, task_id):
        return {"status": self.status, "assigned_agent_id": self.assigned}

    def own_agent_id(self, task_id):
        return "ME"

    def task_is_active(self, task_id):
        return bridge.McCliLifecycle.task_is_active(self, task_id)


def test_unclaimed_inbox_card_is_still_ours_for_a_retry():
    """#881 x #884: a delivered, not-yet-claimed card is legitimately
    ``inbox``. The ownership check before a retry must not read that as
    "taken away" (halted_external) — the run would end silently with no
    blocker, because the safety net only looks at in_progress cards."""
    lc = OwnershipReading("inbox")
    gate = bridge.FirstActivityAck(lc, "T1")
    runs: list[int] = []

    def run_once():
        runs.append(1)
        return _dead_model_outcome()

    action = bridge.drive_live_run(
        lc, run_once, task_id="T1", board_requires_review=False,
        retries_left=1, ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert action.action != "halted_external", (action, lc.calls)
    assert len(runs) == 2, runs
    assert "blocker" in lc.kinds(), lc.calls


def test_unclaimed_card_given_to_someone_else_halts():
    lc = OwnershipReading("inbox", assigned="OTHER")
    gate = bridge.FirstActivityAck(lc, "T1")
    action = bridge.drive_live_run(
        lc, _dead_model_outcome, task_id="T1", board_requires_review=False,
        retries_left=1, ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert action.action == "halted_external", (action, lc.calls)


def test_claimed_card_back_in_inbox_halts():
    """Once claimed, ``inbox`` means MC took the card back — stop."""
    lc = OwnershipReading("inbox")
    action = bridge.drive_live_run(
        lc, _dead_model_outcome, task_id="T1", board_requires_review=False,
        retries_left=1, pre_acked=True, classify_fn=bridge.classify_acp,
    )
    assert action.action == "halted_external", (action, lc.calls)


# ── 3. serve loop, ACP branch, production wiring ────────────────────────────

_ENV_KEYS = (
    "PI_CODING_AGENT_DIR", "OMP_DRIVER", "OMP_ACP_CWD", "OMP_ACP_PERMISSIONS",
    "MSG_DELIVERY_MODE", "OMP_HOME", "OMP_TURN_SIGNAL_FILE", "OMP_TASK_LOCK_FILE",
    "OMP_MSG_QUEUE_DIR", "OMP_MSG_ACK_DIR", "OMP_MSG_NUDGE_STATE_FILE",
    "OMP_MSG_NUDGE_MSG_FILE", "OMP_MAX_RETRIES", "OMP_MAX_CONTINUES",
    "OMP_TASK_DEADLINE", "OMP_PROFILE",
)


def _serve_once(fixture: Path, *, retries: str = "1", continues: str = "0") -> list[tuple]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    orig_run = bridge.run_acp_once
    fakes: list = []
    try:
        with tempfile.TemporaryDirectory(prefix="ack-gate-") as td:
            tmp = Path(td)
            (tmp / "agent").mkdir()
            (tmp / "workspace").mkdir()
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
                "OMP_MAX_RETRIES": retries,
                "OMP_MAX_CONTINUES": continues,
            })
            os.environ.pop("OMP_TASK_DEADLINE", None)
            os.environ.pop("OMP_PROFILE", None)

            def spy_run(prompt, **kw):
                fake = InProcessFake(fixture, [])
                fakes.append(fake)
                kw["client_factory"] = lambda: fake.client
                return orig_run(prompt, **kw)

            bridge.run_acp_once = spy_run
            lc = Recording()
            task = {"id": "task-1", "board_id": "board-1",
                    "dispatch_attempt_id": "att-1", "prompt": "Do the thing.",
                    "workspace_path": str(tmp / "workspace")}
            states = iter([{"state": "new_task", "task": task}])
            bridge.serve_loop(
                poll_interval=0, max_iterations=1,
                _poll_fn=lambda: next(states, {"state": "idle"}),
                _lifecycle_factory=lambda t: lc, _run_factory=None,
                _sleep=lambda _s: None,
                _context_env_path=str(tmp / "mc-context.env"),
            )
            return lc.calls
    finally:
        bridge.run_acp_once = orig_run
        for f in fakes:
            try:
                f.close()
            except Exception:
                pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_serve_loop_never_claims_a_task_the_model_cannot_answer():
    """Sabotage scenario from the operator: runtime off, task assigned —
    the card must never become in_progress."""
    calls = _serve_once(UNREACHABLE, retries="1")
    kinds = [c[0] for c in calls]
    assert "ack" not in kinds, calls
    assert kinds and kinds[0] == "blocker", calls


def test_serve_loop_never_claims_on_omps_error_chunk():
    """Operator sabotage probe, live shape: Spark off → omp's ECONNREFUSED
    chunk + 'refusal'. Continue-nudges included — the card never becomes
    in_progress and ends blocked with the reason."""
    calls = _serve_once(ECONNREFUSED, retries="1", continues="2")
    kinds = [c[0] for c in calls]
    assert "ack" not in kinds, calls
    assert kinds and kinds[0] == "blocker", calls
    assert "Modell hat nicht geantwortet" in calls[0][3], calls[0]
    # The two continue-nudge notes were held, not lost.
    assert sum("continue-nudge" in c[2] for c in calls if c[0] == "comment") == 2, calls


def test_serve_loop_claims_once_the_model_answers():
    calls = _serve_once(NORMAL, retries="0")
    kinds = [c[0] for c in calls]
    assert kinds.count("ack") == 1, calls
    # The claim is the first thing MC hears about this task.
    assert kinds[0] == "ack", calls
