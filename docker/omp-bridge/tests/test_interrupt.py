#!/usr/bin/env python3
"""Fix 3 (omp stop-signal) — tests for the heartbeat control channel:
InterruptState, the _on_control heartbeater callback, the interrupt ladder
in _observe_native_turn, and the Kind.INTERRUPTED classification.

Covers (task contract, >= 6 tests):
  - hard signal ends the run WITHOUT retry/blocker escalation
  - soft signal ends the run; serve loop delivers the nudge and continues
  - grace timeout falls to the next ladder rung (Escape -> C-c -> watchdog)
  - heartbeat response WITHOUT `control` changes nothing (legacy behavior)
  - hard beats soft when both arrive
  - INTERRUPTED is NEVER classified as abort_hang (even after a rung-4 kill)
  - heartbeater's _on_control callback fires from the parsed response

Run:  python3 test_interrupt.py     (standalone)   OR   pytest -v
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402
from bridge import Kind  # noqa: E402

REFLECTION = (
    "## Was wurde gemacht\nDatei erstellt und getestet.\n"
    "## Was hat funktioniert\nAlles lief sauber.\n"
    "## Was war unklar\nNichts.\n"
    "## Lesson fuer Agent-Memory\nImmer das Signal-File pruefen.\n"
    "TASK_COMPLETE"
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt or 0.001


def _te(stop_reason, *, text="", err=None, idx=0):
    return {
        "kind": "turn_end", "turnIndex": idx, "stopReason": stop_reason,
        "errorMessage": err, "errorStatus": None, "toolError": False, "text": text,
    }


class _Harness:
    """The test_native_tui._Harness pattern, extended with a scriptable
    interrupt signal that fires after N sleep ticks."""

    def __init__(self, batches, *, alive=True, fire_after_tick=None,
                 interrupt_kind="hard", interrupt_reason="test"):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="omp-interrupt-")
        self.sig = os.path.join(self.tmp, "turn-signal.ndjson")
        open(self.sig, "w", encoding="utf-8").close()
        self.run_log: list[list[str]] = []
        self.batches = list(batches)
        self.clock = _Clock()
        self.fire_after_tick = fire_after_tick
        self.state = bridge.InterruptState()
        self._ticks = 0
        self._alive = alive
        self._interrupt_kind = interrupt_kind
        self._interrupt_reason = interrupt_reason

        def fake_run(args):
            self.run_log.append(args)
            if args and args[0] == "list-panes":
                return 0, "4242\n"
            if args and args[0] == "capture-pane":
                return 0, "╰─                              ─╯\n"
            return 0, ""

        self.ctrl = bridge.NativeTuiController(
            session="alpha", signal_file=self.sig, _run=fake_run,
            _pid_alive=lambda _pid: self._alive,
            _sleep=lambda _s: None,
        )

    def sleep(self, dt):
        self.clock.sleep(dt)
        self._ticks += 1
        if self.fire_after_tick is not None and self._ticks >= self.fire_after_tick:
            self.state.signal(self._interrupt_kind, self._interrupt_reason)
            self.fire_after_tick = None  # once
        if self.batches:
            batch = self.batches.pop(0)
            with open(self.sig, "a", encoding="utf-8") as fh:
                for rec in batch:
                    fh.write(json.dumps(rec) + "\n")

    def run(self, **kw):
        defaults = dict(
            cwd="/workspace/proj", prompt="Do the thing.\n" + REFLECTION,
            task_file_path=os.path.join(self.tmp, "task-1.md"), isolate=True,
            ready_timeout=1000, turn_deadline=100000, idle_timeout=100000,
            poll_interval=1.0, now=self.clock.now, sleep=self.sleep,
            interrupt_state=self.state,
        )
        defaults.update(kw)
        return bridge.run_native_turn(self.ctrl, **defaults)

    def cmds(self, verb):
        return [a for a in self.run_log if a and a[0] == verb]


class _Recording(bridge.MCLifecycle):
    def __init__(self):
        self.calls = []

    def ack(self, task_id):
        self.calls.append(("ack", task_id))

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish", task_id, review))

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", task_id, blocker_type))

    def comment(self, task_id, text):
        self.calls.append(("comment", task_id, text))

    def task_is_active(self, task_id):
        return True


# ---------------------------------------------------------------------------
# InterruptState primitives
# ---------------------------------------------------------------------------

def test_interrupt_state_hard_beats_soft():
    s = bridge.InterruptState()
    s.signal("soft", "nudge waiting")
    s.signal("hard", "operator stop")
    assert s.kind == "hard" and s.reason == "operator stop"

    s2 = bridge.InterruptState()
    s2.signal("hard", "stop")
    s2.signal("soft", "late nudge")
    assert s2.kind == "hard"  # a later soft never downgrades a hard
    print("PASS test_interrupt_state_hard_beats_soft")


def test_interrupt_state_clear_resets():
    s = bridge.InterruptState()
    s.signal("hard", "x")
    s.clear()
    assert not s.fired() and s.kind is None and s.reason is None
    print("PASS test_interrupt_state_clear_resets")


# ---------------------------------------------------------------------------
# hard: signal mid-turn ends the run via the ladder (Escape), no escalation
# ---------------------------------------------------------------------------

def test_hard_interrupt_ends_run_without_retry_or_blocker():
    # Session ready, turn hangs; after 2 ticks the hard signal fires.
    # The ladder sends Escape; omp answers with turn_end aborted.
    h = _Harness(
        [[{"kind": "session_start"}]],
        fire_after_tick=2, interrupt_kind="hard",
        interrupt_reason="run_control=stopped",
    )
    # After the Escape (tick 3), deliver the aborted turn_end.
    original_sleep = h.sleep

    def sleep_with_abort(dt):
        original_sleep(dt)
        if h.state.fired() and h.cmds("send-keys") and not h.batches:
            if not getattr(h, "_aborted_delivered", False):
                h._aborted_delivered = True
                with open(h.sig, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(_te("aborted", err="Interrupted by user")) + "\n")

    h.sleep = sleep_with_abort
    outcome = h.run(interrupt_grace=5)

    cls = bridge.classify(outcome)
    assert cls.kind is Kind.INTERRUPTED, cls
    assert outcome.interrupt_kind == "hard"
    # NOT classified as hang even though the ladder's rung-1 waited:
    assert cls.kind is not Kind.ABORT_HANG
    # decide_lifecycle: NO retry, NO blocker — terminal-benign halt.
    action = bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=2)
    assert action.action == "halted_interrupted"
    # Escape was sent (rung 2 of the ladder).
    assert h.cmds("send-keys"), "ladder must send Escape"
    print("PASS test_hard_interrupt_ends_run_without_retry_or_blocker")


def test_hard_interrupt_full_flow_no_blocker_comment():
    # Through drive_live_run: an interrupted run must produce NO blocker,
    # NO retry comment — the backend owns the stopped state.
    h = _Harness(
        [[{"kind": "session_start"}]],
        fire_after_tick=2, interrupt_kind="hard",
        interrupt_reason="blocked durch Fremdakteur",
    )
    original_sleep = h.sleep

    def sleep_with_abort(dt):
        original_sleep(dt)
        if h.state.fired() and h.cmds("send-keys") and not getattr(h, "_aborted_delivered", False):
            h._aborted_delivered = True
            with open(h.sig, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_te("aborted", err="Interrupted by user")) + "\n")

    h.sleep = sleep_with_abort
    outcome = bridge.run_native_turn(
        h.ctrl, cwd="/workspace/proj", prompt="p\n" + REFLECTION,
        task_file_path=os.path.join(h.tmp, "t.md"), isolate=True,
        ready_timeout=1000, turn_deadline=100000, idle_timeout=100000,
        poll_interval=1.0, now=h.clock.now, sleep=h.sleep,
        interrupt_state=h.state,
    )
    lc = _Recording()
    bridge.drive_live_run(
        lc, lambda: outcome, task_id="t1", board_requires_review=True,
        retries_left=2, pre_acked=True, continues_left=2,
        continue_once=lambda n: outcome,
    )
    kinds = [c[0] for c in lc.calls]
    assert "blocker" not in kinds, lc.calls
    assert "finish" not in kinds
    assert not any(c[0] == "comment" and "omp abort" in c[2] for c in lc.calls)
    print("PASS test_hard_interrupt_full_flow_no_blocker_comment")


# ---------------------------------------------------------------------------
# Grace timeout falls through the rungs to the watchdog
# ---------------------------------------------------------------------------

def test_grace_timeout_falls_to_next_rung_then_watchdog():
    # Signal fires, but omp NEVER answers any rung -> ladder falls through
    # Escape -> C-c -> watchdog kill (respawn). Kind stays INTERRUPTED
    # (never abort_hang) because outcome.interrupted is set.
    h = _Harness(
        [[{"kind": "session_start"}]],
        fire_after_tick=1, interrupt_kind="hard", interrupt_reason="stop",
    )
    outcome = h.run(interrupt_grace=2)
    assert outcome.interrupted is True
    assert outcome.watchdog_killed is True  # rung 4 fired
    assert h.cmds("send-keys")             # Escape (rung 2)
    assert any("C-c" in a for a in h.cmds("send-keys"))  # C-c (rung 3)
    assert h.cmds("respawn-window")        # watchdog relaunch
    cls = bridge.classify(outcome)
    assert cls.kind is Kind.INTERRUPTED    # NOT ABORT_HANG
    print("PASS test_grace_timeout_falls_to_next_rung_then_watchdog")


# ---------------------------------------------------------------------------
# Response without `control` -> legacy behavior (sabotage probe)
# ---------------------------------------------------------------------------

def test_heartbeat_response_without_control_changes_nothing():
    # _on_control is NOT called when the response lacks the control field.
    calls = []
    seen = []

    def fake_send(status):
        seen.append(status)
        return {"ok": True, "agent": "alpha"}  # NO control key

    stop = bridge.start_heartbeater(
        "http://x", "tok", interval=0.01,
        _task_active=lambda: True, _send=fake_send,
        _on_control=lambda k, r: calls.append((k, r)),
    )
    import time as _t
    _t.sleep(0.05)
    stop.set()
    assert seen and not calls, calls
    print("PASS test_heartbeat_response_without_control_changes_nothing")


def test_heartbeater_on_control_fires_on_hard_and_soft():
    import time as _t
    for expect, resp in (
        ("hard", {"control": {"interrupt": "hard", "reason": "stop"}}),
        ("soft", {"control": {"interrupt": "soft", "reason": "nudge"}}),
    ):
        calls = []

        def fake_send(_status, _resp=resp):
            return _resp

        stop = bridge.start_heartbeater(
            "http://x", "tok", interval=0.01,
            _task_active=lambda: True, _send=fake_send,
            _on_control=lambda k, r: calls.append((k, r)),
        )
        _t.sleep(0.05)
        stop.set()
        assert calls and calls[0][0] == expect, calls
    print("PASS test_heartbeater_on_control_fires_on_hard_and_soft")


# ---------------------------------------------------------------------------
# soft: end of run, no escalation; serve loop continues (nudge path)
# ---------------------------------------------------------------------------

def test_soft_interrupt_ends_run_and_allows_continue():
    h = _Harness(
        [[{"kind": "session_start"}]],
        fire_after_tick=2, interrupt_kind="soft",
        interrupt_reason="blocker message waiting",
    )
    original_sleep = h.sleep

    def sleep_with_abort(dt):
        original_sleep(dt)
        if h.state.fired() and h.cmds("send-keys") and not getattr(h, "_aborted_delivered", False):
            h._aborted_delivered = True
            with open(h.sig, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_te("aborted", err="Interrupted by user")) + "\n")

    h.sleep = sleep_with_abort
    outcome = h.run(interrupt_grace=5)
    cls = bridge.classify(outcome)
    assert cls.kind is Kind.INTERRUPTED
    assert outcome.interrupt_kind == "soft"
    # Same terminal-benign decision as hard — the CONTINUE happens in the
    # serve loop (next dispatch / nudge), never as a retry here.
    action = bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=2)
    assert action.action == "halted_interrupted"
    print("PASS test_soft_interrupt_ends_run_and_allows_continue")


# ---------------------------------------------------------------------------
# INTERRUPTED is never classified as abort_hang — even a clean natural end
# during the grace window of an interrupt keeps the INTERRUPTED verdict.
# ---------------------------------------------------------------------------

def test_interrupted_never_classified_as_abort_hang():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_end = True
    o.final_stop_reason = "aborted"
    o.error_message = "Interrupted by user"
    o.interrupted = True
    o.interrupt_kind = "hard"
    o.interrupt_reason = "operator stop"
    # Even with a watchdog kill recorded (ladder rung 4), INTERRUPTED wins:
    o.watchdog_killed = True
    o.watchdog_reason = "interrupt_ladder"
    cls = bridge.classify(o)
    assert cls.kind is Kind.INTERRUPTED
    assert cls.kind is not Kind.ABORT_HANG
    assert "hang" not in cls.reason
    print("PASS test_interrupted_never_classified_as_abort_hang")


def test_interrupted_survives_watchdog_kill_flag():
    # Pure classification edge: watchdog_killed alone is ABORT_HANG, but with
    # interrupted=True the INTERRUPTED check runs FIRST.
    o = bridge.RunOutcome()
    o.saw_session = True
    o.watchdog_killed = True
    o.watchdog_reason = "deadline"
    o.interrupted = True
    o.interrupt_kind = "soft"
    assert bridge.classify(o).kind is Kind.INTERRUPTED
    # And the control-channel fields exist on a fresh outcome.
    fresh = bridge.RunOutcome()
    assert fresh.interrupted is False and fresh.interrupt_kind is None
    print("PASS test_interrupted_survives_watchdog_kill_flag")


# ---------------------------------------------------------------------------
# Standalone runner (matches test_native_tui.py's pattern)
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
