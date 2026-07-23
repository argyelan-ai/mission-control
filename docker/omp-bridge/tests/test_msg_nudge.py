#!/usr/bin/env python3
"""W2.1 nudge+pull (ADR-071) — unit tests for MSG_DELIVERY_MODE=nudge in
bridge.py. Python-omp port of scripts/grok-bridge.py's deliver_messages_nudge.

Covers:
  - _nudge_thread_seqs: per-thread max, malformed entries skipped
  - _MsgDelivery.nudge: first nudge injects once + writes state + never acks
  - dedup before the remind window, re-nudge on a higher seq, remind after
    the window elapses
  - empty new_messages resets (removes) the state file
  - turn-gate closed → no inject, no state write
  - inject_file failure → state untouched (retry next poll)
  - stale paste-mode queue files are cleared
  - a corrupt state file degrades to re-nudging (never crashes)
  - the nudge path is fully wrapped — an exception never escapes
  - default mode ("paste") never touches any nudge code path

Run: python3 test_msg_nudge.py   (standalone)   OR   pytest -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402


def _msg(seq, tid, body="hallo", sender="mark", mtype="dm"):
    return {"id": f"m{seq}", "thread_id": tid, "seq": seq, "sender": sender,
            "message_type": mtype, "body": body}


class _StubTui:
    """Minimal stand-in for NativeTuiController — nudge only ever calls
    inject_file(path). `results` is a queue of bools it returns per call."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.injected: list[str] = []

    def inject_file(self, path, **kw):
        self.injected.append(path)
        return self.results.pop(0) if self.results else True


def _delivery(tmp, tui, *, signal="sig.ndjson", remind_seconds=600):
    return bridge._MsgDelivery(
        tui,
        signal_file=os.path.join(tmp, signal),
        queue_dir=os.path.join(tmp, "queue"),
        ack_dir=os.path.join(tmp, "ack"),
        task_lock_path=os.path.join(tmp, "task.lock"),
        nudge_state_file=os.path.join(tmp, "nudge-state"),
        nudge_msg_file=os.path.join(tmp, "nudge.msg"),
        remind_seconds=remind_seconds,
        log=lambda _m: None,
    )


# ── _nudge_thread_seqs ───────────────────────────────────────────────────────

def test_nudge_thread_seqs_per_thread_max_and_malformed_skip():
    messages = [
        _msg(1, "a"), _msg(5, "a"), _msg(3, "a"),
        _msg(9, "b"),
        {"thread_id": "c"},          # missing seq → skipped
        {"seq": 4},                  # missing thread_id → skipped
        {"thread_id": "d", "seq": "not-an-int"},  # bad seq → skipped
    ]
    seqs = bridge._nudge_thread_seqs(messages)
    assert seqs == {"a": 5, "b": 9}, seqs
    print("PASS test_nudge_thread_seqs_per_thread_max_and_malformed_skip")


# ── first nudge: inject once, write state, never ack ────────────────────────

def test_first_nudge_injects_once_writes_state_no_ack():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        deliv.nudge([_msg(3, "th")])

        assert tui.injected == [deliv.nudge_msg_file]
        text = open(deliv.nudge_msg_file, encoding="utf-8").read()
        assert "bis seq 3" in text and "mc inbox" in text, text
        assert "📬" in text

        state = bridge._nudge_state_read(deliv.nudge_state_file)
        assert "th" in state and state["th"][0] == 3, state

        # Nudge mode never acks locally — no ack file, no ack dir at all.
        assert not os.path.exists(deliv.ack_dir)
        assert deliv._awaiting_offset is not None, "gate must hold until turn_end"
    print("PASS test_first_nudge_injects_once_writes_state_no_ack")


# ── dedup before the remind window ───────────────────────────────────────────

def test_dedup_same_seq_before_remind_window_no_reinject():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui, remind_seconds=600)
        deliv.nudge([_msg(3, "th")])
        assert len(tui.injected) == 1

        # Turn ends so the gate reopens; same seq again, well within the
        # remind window → must NOT re-nudge.
        with open(deliv.signal_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")
        deliv.nudge([_msg(3, "th")])
        assert len(tui.injected) == 1, "identical seq within remind window must not re-nudge"
    print("PASS test_dedup_same_seq_before_remind_window_no_reinject")


# ── higher seq → re-nudge ────────────────────────────────────────────────────

def test_higher_seq_triggers_renudge():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True, True])
        deliv = _delivery(d, tui, remind_seconds=600)
        deliv.nudge([_msg(3, "th")])
        with open(deliv.signal_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")

        deliv.nudge([_msg(7, "th")])
        assert len(tui.injected) == 2, "a higher seq in the same thread must re-nudge immediately"
        state = bridge._nudge_state_read(deliv.nudge_state_file)
        assert state["th"][0] == 7, state
    print("PASS test_higher_seq_triggers_renudge")


# ── remind after the window elapses ──────────────────────────────────────────

def test_remind_after_window_elapsed():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True, True])
        deliv = _delivery(d, tui, remind_seconds=0.01)
        deliv.nudge([_msg(3, "th")])
        assert len(tui.injected) == 1
        with open(deliv.signal_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")

        # Backdate the state's epoch beyond the (tiny) remind window instead
        # of sleeping in a test.
        bridge._nudge_state_write(deliv.nudge_state_file, {"th": 3}, now=0)

        deliv.nudge([_msg(3, "th")])  # same seq, but remind window has elapsed
        assert len(tui.injected) == 2, "an elapsed remind window must re-nudge even for the same seq"
    print("PASS test_remind_after_window_elapsed")


# ── empty new_messages resets state ──────────────────────────────────────────

def test_empty_new_messages_removes_state_file():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        deliv.nudge([_msg(3, "th")])
        assert os.path.exists(deliv.nudge_state_file)

        deliv.nudge([])
        assert not os.path.exists(deliv.nudge_state_file)
        assert len(tui.injected) == 1, "empty new_messages must not inject anything"
    print("PASS test_empty_new_messages_removes_state_file")


# ── turn gate closed ─────────────────────────────────────────────────────────

def test_gate_closed_no_inject_no_state():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        open(deliv.task_lock_path, "w").write("1")  # a dispatch is in flight
        deliv.nudge([_msg(3, "th")])

        assert tui.injected == []
        assert not os.path.exists(deliv.nudge_state_file)
    print("PASS test_gate_closed_no_inject_no_state")


# ── inject_file failure → state untouched, retry next poll ──────────────────

def test_inject_failure_leaves_state_untouched():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([False])
        deliv = _delivery(d, tui)
        deliv.nudge([_msg(3, "th")])

        assert len(tui.injected) == 1  # attempted
        assert not os.path.exists(deliv.nudge_state_file), "failed verify must not record state"
        assert deliv._awaiting_offset is None, "no processing turn started"
        assert not deliv._holds_lock, "lock must not linger on a failed inject"
    print("PASS test_inject_failure_leaves_state_untouched")


# ── stale paste-mode queue cleanup ───────────────────────────────────────────

def test_stale_paste_queue_files_cleared():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        bridge.queue_messages({"new_messages": [_msg(1, "th", body="old paste-mode leftover")]},
                               deliv.queue_dir)
        assert bridge.msg_queue_files(deliv.queue_dir) == ["00000001__th.msg"]

        deliv.nudge([_msg(3, "th")])
        assert bridge.msg_queue_files(deliv.queue_dir) == [], "stale queue files must be cleared"
    print("PASS test_stale_paste_queue_files_cleared")


# ── corrupt state file degrades to re-nudge ──────────────────────────────────

def test_corrupt_state_file_degrades_to_renudge():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        os.makedirs(os.path.dirname(deliv.nudge_state_file), exist_ok=True)
        with open(deliv.nudge_state_file, "w", encoding="utf-8") as fh:
            fh.write("this is not a valid state line\n")
            fh.write("th 5\n")            # too few fields
            fh.write("th2 notanint 1\n")  # bad seq

        deliv.nudge([_msg(3, "th")])
        assert len(tui.injected) == 1, "a corrupt/unreadable entry must degrade to re-nudging"
    print("PASS test_corrupt_state_file_degrades_to_renudge")


# ── nudge path never crashes the poll loop ───────────────────────────────────

class _RaisingTui:
    def inject_file(self, path, **kw):
        raise RuntimeError("tmux exploded")


def test_nudge_swallows_exceptions():
    with tempfile.TemporaryDirectory() as d:
        deliv = _delivery(d, _RaisingTui())
        deliv.nudge([_msg(3, "th")])  # must not raise
        assert not os.path.exists(deliv.nudge_state_file)
    print("PASS test_nudge_swallows_exceptions")


def test_serve_loop_survives_nudge_error():
    # bridge.MSG_DELIVERY_MODE is read once at import time from the env; the
    # serve_loop dispatch checks the module attribute directly (not the env
    # var) each iteration, so flipping the attribute in-place (restored after
    # the test) toggles the code path without reload()ing the module — a
    # reload would mint new Enum class objects (Kind, ...) and break `is`
    # identity comparisons other already-imported test modules rely on.
    task = {"id": "task-1", "board_id": "b1", "dispatch_attempt_id": "att-1",
            "prompt": "Do the thing."}
    orig_mode = bridge.MSG_DELIVERY_MODE
    with tempfile.TemporaryDirectory() as d:
        bridge.MSG_DELIVERY_MODE = "nudge"
        try:
            # nudge-state parent is a FILE → os.makedirs raises NotADirectoryError.
            blocker = os.path.join(d, "afile")
            open(blocker, "w").close()
            bad_state = os.path.join(blocker, "nudge-state")
            lc = _RecordingLifecycle()
            it = iter([{"state": "new_task", "task": task, "new_messages": [_msg(1, "th")]}])
            bridge.serve_loop(
                poll_interval=0, max_iterations=1,
                _poll_fn=lambda: next(it, {"state": "idle"}),
                _lifecycle_factory=lambda t: lc,
                _run_factory=lambda t, cwd: _finish_outcome,
                _sleep=lambda _s: None,
                _context_env_path=os.path.join(d, "ctx.env"),
                _msg_queue_dir=os.path.join(d, "queue"), _msg_ack_dir=os.path.join(d, "ack"),
                _task_lock_path=os.path.join(d, "task.lock"),
                _nudge_state_file=bad_state, _nudge_msg_file=os.path.join(d, "nudge.msg"),
            )
            assert ("ack", "task-1") in lc.calls
            assert any(c[0] == "finish" for c in lc.calls)
        finally:
            bridge.MSG_DELIVERY_MODE = orig_mode
    print("PASS test_serve_loop_survives_nudge_error")


# ── default mode = paste, nudge code never touched ───────────────────────────

def _finish_outcome():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_start = True
    o.saw_agent_end = True
    o.final_stop_reason = "stop"
    o.final_text = (
        "## Was wurde gemacht\nDatei erstellt und getestet, alles laeuft.\n"
        "## Was hat funktioniert\nDer deterministische Fix, zweiter Lauf gruen.\n"
        "## Was war unklar\nNichts Wesentliches, Aufgabe war eindeutig.\n"
        "## Lesson fuer Agent-Memory\nImmer erst reproduzieren, dann fixen.\n"
        "TASK_COMPLETE"
    )
    return o


class _RecordingLifecycle(bridge.MCLifecycle):
    def __init__(self):
        self.calls = []

    def ack(self, task_id):
        self.calls.append(("ack", task_id))

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish", task_id, review))

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", task_id, blocker_type))

    def comment(self, task_id, text):
        self.calls.append(("comment", task_id))


def test_default_mode_paste_never_touches_nudge_code():
    assert bridge.MSG_DELIVERY_MODE == "paste", bridge.MSG_DELIVERY_MODE
    task = {"id": "task-1", "board_id": "b1", "dispatch_attempt_id": "att-1",
            "prompt": "Do the thing."}
    with tempfile.TemporaryDirectory() as d:
        nudge_state = os.path.join(d, "nudge-state")
        lc = _RecordingLifecycle()
        it = iter([{"state": "new_task", "task": task, "new_messages": [_msg(1, "th")]}])
        bridge.serve_loop(
            poll_interval=0, max_iterations=1,
            _poll_fn=lambda: next(it, {"state": "idle"}),
            _lifecycle_factory=lambda t: lc,
            _run_factory=lambda t, cwd: _finish_outcome,
            _sleep=lambda _s: None,
            _context_env_path=os.path.join(d, "ctx.env"),
            _msg_queue_dir=os.path.join(d, "queue"), _msg_ack_dir=os.path.join(d, "ack"),
            _task_lock_path=os.path.join(d, "task.lock"),
            _nudge_state_file=nudge_state, _nudge_msg_file=os.path.join(d, "nudge.msg"),
        )
        assert ("ack", "task-1") in lc.calls
        assert any(c[0] == "finish" for c in lc.calls)
        # Default paste mode: the paste queue got the message (expected,
        # unchanged behaviour); the NUDGE state file must never be created.
        assert not os.path.exists(nudge_state), "paste mode must never touch nudge state"
    print("PASS test_default_mode_paste_never_touches_nudge_code")


def test_serve_loop_commv2_off_touches_no_nudge_state():
    task = {"id": "task-1", "board_id": "b1", "dispatch_attempt_id": "att-1",
            "prompt": "Do the thing."}
    orig_mode = bridge.MSG_DELIVERY_MODE
    with tempfile.TemporaryDirectory() as d:
        bridge.MSG_DELIVERY_MODE = "nudge"
        try:
            nudge_state = os.path.join(d, "nudge-state")
            lc = _RecordingLifecycle()
            it = iter([{"state": "new_task", "task": task}])  # no new_messages key: comm_v2=false
            bridge.serve_loop(
                poll_interval=0, max_iterations=1,
                _poll_fn=lambda: next(it, {"state": "idle"}),
                _lifecycle_factory=lambda t: lc,
                _run_factory=lambda t, cwd: _finish_outcome,
                _sleep=lambda _s: None,
                _context_env_path=os.path.join(d, "ctx.env"),
                _msg_queue_dir=os.path.join(d, "queue"), _msg_ack_dir=os.path.join(d, "ack"),
                _task_lock_path=os.path.join(d, "task.lock"),
                _nudge_state_file=nudge_state, _nudge_msg_file=os.path.join(d, "nudge.msg"),
            )
            assert ("ack", "task-1") in lc.calls
            assert any(c[0] == "finish" for c in lc.calls)
            assert not os.path.exists(nudge_state), "comm_v2=false must never touch nudge state, even in nudge mode"
        finally:
            bridge.MSG_DELIVERY_MODE = orig_mode
    print("PASS test_serve_loop_commv2_off_touches_no_nudge_state")



# ── review fix 2026-07-23: nudge fires at the boundary, never pre-dispatch ────

def test_serve_loop_nudges_after_dispatch_not_before():
    """A payload carrying new_task AND new_messages must run the task FIRST
    and nudge only at the post-dispatch boundary — a pre-dispatch nudge gets
    its turn killed by run_native_turn's relaunch while the state file
    already says "nudged" (no re-nudge until the remind window)."""
    task = {"id": "task-1", "board_id": "b1", "dispatch_attempt_id": "att-1",
            "prompt": "Do the thing."}
    order = []
    orig_mode = bridge.MSG_DELIVERY_MODE
    orig_nudge = bridge._MsgDelivery.nudge
    with tempfile.TemporaryDirectory() as d:
        bridge.MSG_DELIVERY_MODE = "nudge"

        def spy_nudge(self, messages):
            order.append("nudge")
            return orig_nudge(self, messages)

        def run_factory(t, cwd):
            def run():
                order.append("run")
                return _finish_outcome()
            return run

        bridge._MsgDelivery.nudge = spy_nudge
        try:
            it = iter([{"state": "new_task", "task": task,
                        "new_messages": [_msg(1, "th")]}])
            bridge.serve_loop(
                poll_interval=0, max_iterations=1,
                _poll_fn=lambda: next(it, {"state": "idle"}),
                _lifecycle_factory=lambda t: _RecordingLifecycle(),
                _run_factory=run_factory,
                _sleep=lambda _s: None,
                _context_env_path=os.path.join(d, "ctx.env"),
                _msg_queue_dir=os.path.join(d, "queue"),
                _msg_ack_dir=os.path.join(d, "ack"),
                _task_lock_path=os.path.join(d, "task.lock"),
                _nudge_state_file=os.path.join(d, "nudge-state"),
                _nudge_msg_file=os.path.join(d, "nudge.msg"),
            )
        finally:
            bridge._MsgDelivery.nudge = orig_nudge
            bridge.MSG_DELIVERY_MODE = orig_mode
    assert "run" in order and "nudge" in order, order
    assert order.index("run") < order.index("nudge"), order
    print("PASS test_serve_loop_nudges_after_dispatch_not_before")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
