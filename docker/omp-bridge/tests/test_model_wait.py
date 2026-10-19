#!/usr/bin/env python3
"""The model is not there: wait for it, do not blame the task (19.10.2026).

Follow-up to "delivered is not working" (ADR-097). Two live findings:

* Task 4de17c6a: the model endpoint was off. The bridge no longer claims the
  card (ADR-097), but it still retried into the dead endpoint and then set a
  blocker. Nothing is broken in the task; the model is just not there.
* Task 43788774: the model worked, then its engine died. omp answered with
  ``ECONNREFUSED: Unable to connect ...`` as one text chunk and stopReason
  ``refusal`` — omp's own pattern ``refus(al|ed)`` matches "ECONNREFUSED".
  ``classify_acp`` only looked for network errors under ``error``, so the
  run fell through to "missing sentinel": two continue-nudges into a dead
  engine, then a blocker that read like a refusal.

Contract under test:

1. ``classify_acp``: no sentinel + omp's unreachable wording (error message
   or the END of the turn text) -> ``ABORT_TRANSIENT_API`` with reason
   ``model_unreachable`` — whatever the stopReason (``refusal``,
   ``end_turn``). A real finish that mentions such a word stays FINISH.
2. ``drive_live_run`` with an ack gate, card not claimed, model unreachable:
   ``awaiting_model`` — no retry, no blocker, no comment (an agent comment
   on an unclaimed card would claim it). MC's ACK ladder returns the card
   to the inbox with the reason (backend, task_runner).
3. Card claimed, model lost mid-run: one blocker that names the model —
   no continue-nudge, no retry into the dead endpoint.
4. The task channel's phase file (``acp-task-state.json``) for the chat:
   ``delivered`` while the model has not answered, ``working`` once it did,
   ``idle`` after the run.
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
import test_ack_on_first_activity as base  # noqa: E402

ECONN_TEXT = (
    "ECONNREFUSED: Unable to connect. Is the computer able to access the url?"
)


def _outcome(*, text="", stop="refusal", worked=False, error=None) -> bridge.RunOutcome:
    o = bridge.RunOutcome()
    o.saw_session = o.saw_agent_start = o.saw_agent_end = True
    o.final_stop_reason = stop
    o.final_text = text
    o.model_worked = worked
    o.error_message = error
    return o


# ── 1. classification ───────────────────────────────────────────────────────

def test_econnrefused_refusal_is_a_lost_model_not_a_refusal():
    cls = bridge.classify_acp(_outcome(text=ECONN_TEXT, stop="refusal"))
    assert cls.kind is bridge.Kind.ABORT_TRANSIENT_API, cls
    assert cls.reason == "model_unreachable", cls
    assert cls.retryable is True
    assert "Modell nicht erreichbar" in cls.detail, cls.detail


def test_retry_budget_line_at_end_turn_is_a_lost_model():
    text = "Ich pruefe die Dateien.\n\nRetry budget exhausted after 10 retries: " + ECONN_TEXT
    cls = bridge.classify_acp(_outcome(text=text, stop="end_turn", worked=True))
    assert cls.reason == "model_unreachable", cls


def test_lost_model_is_retried_not_nudged():
    cls = bridge.classify_acp(_outcome(text=ECONN_TEXT, stop="refusal"))
    action = bridge.decide_lifecycle(
        cls, board_requires_review=False, retries_left=1, continues_left=2,
    )
    assert action.action == "retry", action


def test_a_real_finish_that_mentions_the_word_stays_finish():
    text = (
        "Fertig." + base._REFLECTION.replace(
            "Das Modell antwortete sofort und sauber.",
            "Der Fehler 'connection refused' im Log war alt und ist behoben.",
        )
    )
    cls = bridge.classify_acp(_outcome(text=text, stop="end_turn", worked=True))
    assert cls.kind is bridge.Kind.FINISH, cls


def test_the_word_early_in_a_long_turn_is_not_a_lost_model():
    """Only the END of the text counts — a turn that discussed the error
    and then just forgot the sentinel is still a missing sentinel."""
    text = "Im Log stand ECONNREFUSED von gestern.\n" + ("Analyse. " * 120)
    cls = bridge.classify_acp(_outcome(text=text, stop="end_turn", worked=True))
    assert cls.kind is bridge.Kind.SILENT_ABORT_NO_SENTINEL, cls


def test_plain_missing_sentinel_is_unchanged():
    cls = bridge.classify_acp(_outcome(text="Ich habe angefangen.", stop="end_turn", worked=True))
    assert cls.kind is bridge.Kind.SILENT_ABORT_NO_SENTINEL, cls


# ── 2. unclaimed + unreachable -> wait, no retry, no blocker ────────────────

def test_unclaimed_card_with_an_unreachable_model_waits():
    lc = base.Recording()
    gate = bridge.FirstActivityAck(lc, "T1")
    runs: list[int] = []

    def run_once():
        runs.append(1)
        return _outcome(text=ECONN_TEXT, stop="refusal")

    action = bridge.drive_live_run(
        lc, run_once, task_id="T1", board_requires_review=False,
        retries_left=2, continues_left=2, continue_once=lambda _n: run_once(),
        ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert action.action == "awaiting_model", (action, lc.calls)
    assert runs == [1], "no retry into a dead endpoint"
    # No ack, no blocker, and no comment: a comment would claim the card.
    assert lc.calls == [], lc.calls


def test_connection_error_without_a_turn_end_waits_too():
    lc = base.Recording()
    gate = bridge.FirstActivityAck(lc, "T1")
    action = bridge.drive_live_run(
        lc, base._dead_model_outcome, task_id="T1", board_requires_review=False,
        retries_left=2, ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert action.action == "awaiting_model", (action, lc.calls)
    assert lc.calls == [], lc.calls


def test_without_an_ack_gate_the_old_retry_ladder_runs():
    """Native / injected path: no gate, behaviour unchanged (retry)."""
    lc = base.Recording()
    runs: list[int] = []

    def run_once():
        runs.append(1)
        return _outcome(text=ECONN_TEXT, stop="refusal")

    bridge.drive_live_run(
        lc, run_once, task_id="T1", board_requires_review=False,
        retries_left=1, pre_acked=True, classify_fn=bridge.classify_acp,
    )
    assert len(runs) == 2, runs


# ── 3. claimed + model lost mid-run -> one blocker naming the model ─────────

def test_model_lost_after_the_claim_is_one_blocker_naming_the_model():
    lc = base.Recording()
    gate = bridge.FirstActivityAck(lc, "T1")
    runs: list[int] = []

    def run_once():
        runs.append(1)
        gate.trigger()  # the model worked first
        return _outcome(text="Ich lese den Code.\n" + ECONN_TEXT, stop="refusal", worked=True)

    action = bridge.drive_live_run(
        lc, run_once, task_id="T1", board_requires_review=False,
        retries_left=2, continues_left=2, continue_once=lambda _n: run_once(),
        ack_gate=gate, classify_fn=bridge.classify_acp,
    )
    assert action.action == "blocker", (action, lc.calls)
    assert runs == [1], "no nudge / retry into a dead endpoint"
    kinds = lc.kinds()
    assert kinds[0] == "ack" and kinds.count("blocker") == 1, lc.calls
    question = next(c[3] for c in lc.calls if c[0] == "blocker")
    assert "Modell nicht mehr erreichbar" in question, question
    assert "ECONNREFUSED" in question, question


# ── 4. serve loop: no claim, no blocker; phase file for the chat ────────────

def _serve_with_phases(fixture: Path, **kw) -> tuple[list[tuple], list[str]]:
    phases: list[str] = []
    orig = bridge.TaskStateFile.write

    def spy(self, phase, task_id=None):
        phases.append(phase)
        return orig(self, phase, task_id)

    bridge.TaskStateFile.write = spy
    try:
        calls = base._serve_once(fixture, **kw)
    finally:
        bridge.TaskStateFile.write = orig
    return calls, phases


def test_serve_loop_waits_for_a_dead_model_without_claim_or_blocker():
    calls, phases = _serve_with_phases(base.ECONNREFUSED, retries="1", continues="2")
    kinds = [c[0] for c in calls]
    assert "ack" not in kinds and "blocker" not in kinds, calls
    assert "comment" not in kinds, calls
    assert phases[0] == "delivered" and phases[-1] == "idle", phases
    assert "working" not in phases, phases


def test_serve_loop_reports_working_once_the_model_answers():
    calls, phases = _serve_with_phases(base.NORMAL, retries="0")
    assert [c[0] for c in calls].count("ack") == 1, calls
    assert phases[0] == "delivered", phases
    assert "working" in phases and phases[-1] == "idle", phases


def test_task_state_file_is_atomic_json(tmp_path):
    path = tmp_path / "sessions" / bridge.TASK_STATE_FILENAME
    state = bridge.TaskStateFile(path)
    state.write("delivered", "T1")
    data = json.loads(path.read_text())
    assert data["phase"] == "delivered" and data["taskId"] == "T1"
    first = data["updatedAt"]
    state.write("working", "T1")
    data = json.loads(path.read_text())
    assert data["phase"] == "working"
    state.write("idle")
    data = json.loads(path.read_text())
    assert data["phase"] == "idle" and data["taskId"] is None
    assert first  # stamped
    assert [p.name for p in path.parent.iterdir()] == [bridge.TASK_STATE_FILENAME]


def test_task_state_path_sits_at_the_sessions_root(tmp_path, monkeypatch):
    """Where the backend reads it: the root of the omp-sessions mount."""
    monkeypatch.delenv("OMP_TASK_STATE_FILE", raising=False)
    monkeypatch.delenv("OMP_PROFILE", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    path = bridge._task_state_path()
    assert path == tmp_path / "agent" / "sessions" / bridge.TASK_STATE_FILENAME, path


def test_a_write_problem_never_breaks_the_run(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    state = bridge.TaskStateFile(blocker / "sub" / bridge.TASK_STATE_FILENAME)
    state.write("working", "T1")  # must not raise
    state.touch()
