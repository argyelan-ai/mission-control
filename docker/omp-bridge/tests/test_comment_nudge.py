#!/usr/bin/env python3
"""Task-comment wake-up + finish-on-parked-task guard (incident 09.09.2026).

`GET /me/poll` returns `new_comments` and the backend ACKs them in the same
poll (at-most-once). poll.sh pastes them into the claude pane; the omp bridge
silently dropped them — an omp worker never saw review findings, "arbeite
weiter" or the resume-after-park note. Second finding from the same day: the
agent parked its task itself (`mc park` → waiting), `mc finish` refused the
precondition and the bridge "rescued" that into a blocker.

Run: python3 test_comment_nudge.py   OR   pytest -q
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402
from test_msg_nudge import _StubTui, _delivery, _RecordingLifecycle, _finish_outcome  # noqa: E402

TID = "0212db0e-34b3-41a4-9c4e-66f0d199b640"


def _comment(tid=TID, source="user"):
    return {"task_id": tid, "task_title": "Probe", "source": source,
            "comment_type": "system_notify", "content": "UNBLOCKED: weiter", "created_at": "x"}


def test_note_then_nudge_injects_once_and_clears():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        dv = _delivery(d, tui)
        dv.note_comments([_comment(), _comment()])
        assert dv._pending_comments == {TID: 2}
        dv.nudge_comments()
        assert len(tui.injected) == 1
        text = open(tui.injected[0], encoding="utf-8").read()
        assert f"mc task-get {TID}" in text and "2 neue(r)" in text
        assert dv._pending_comments == {}
        # nothing pending → no second injection
        dv.nudge_comments()
        assert len(tui.injected) == 1
    print("PASS test_note_then_nudge_injects_once_and_clears")


def test_gate_closed_defers_then_delivers():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        dv = _delivery(d, tui)
        dv.note_comments([_comment()])
        open(dv.task_lock_path, "w").write("1")  # a dispatch is in flight → gate closed
        dv.nudge_comments()
        assert tui.injected == [] and dv._pending_comments == {TID: 1}
        os.remove(dv.task_lock_path)
        dv.nudge_comments()
        assert len(tui.injected) == 1 and dv._pending_comments == {}
    print("PASS test_gate_closed_defers_then_delivers")


def test_inject_failure_keeps_pending_for_retry():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([False, True])
        dv = _delivery(d, tui)
        dv.note_comments([_comment()])
        dv.nudge_comments()
        assert dv._pending_comments == {TID: 1}
        dv.nudge_comments()
        assert dv._pending_comments == {}
    print("PASS test_inject_failure_keeps_pending_for_retry")


def test_malformed_comments_never_raise():
    with tempfile.TemporaryDirectory() as d:
        dv = _delivery(d, _StubTui())
        dv.note_comments(None)
        dv.note_comments([None, {}, {"task_id": ""}, "junk"])
        assert dv._pending_comments == {}
    print("PASS test_malformed_comments_never_raise")


def test_serve_loop_wakes_agent_for_comments_on_idle_poll():
    """Idle poll carrying new_comments → the boundary calls the comment
    wake-up with the pending count (the real TUI inject is stubbed the same
    way test_msg_nudge spies on nudge())."""
    seen = []
    orig = bridge._MsgDelivery.nudge_comments

    def spy(self):
        seen.append(dict(self._pending_comments))
        self._pending_comments.clear()

    bridge._MsgDelivery.nudge_comments = spy
    with tempfile.TemporaryDirectory() as d:
        payloads = iter([
            {"state": "idle", "new_comments": [_comment()]},
            {"state": "idle"},
        ])
        try:
            bridge.serve_loop(
                poll_interval=0, max_iterations=2,
                _poll_fn=lambda: next(payloads, {"state": "idle"}),
                _lifecycle_factory=lambda t: _RecordingLifecycle(),
                _run_factory=lambda t, cwd: _finish_outcome,
                _sleep=lambda _s: None,
                _context_env_path=os.path.join(d, "ctx.env"),
                _msg_queue_dir=os.path.join(d, "queue"), _msg_ack_dir=os.path.join(d, "ack"),
                _task_lock_path=os.path.join(d, "task.lock"),
                _nudge_state_file=os.path.join(d, "nudge-state"),
                _nudge_msg_file=os.path.join(d, "nudge.msg"),
            )
        finally:
            bridge._MsgDelivery.nudge_comments = orig
    assert seen[0] == {TID: 1}, seen
    assert seen[1] == {}, "second idle poll has nothing pending"
    print("PASS test_serve_loop_wakes_agent_for_comments_on_idle_poll")


def test_dispatch_drops_pending_comments_for_that_task():
    with tempfile.TemporaryDirectory() as d:
        dv = _delivery(d, _StubTui())
        dv.note_comments([_comment(), _comment(tid="other")])
        dv.drop_comments(TID)
        assert dv._pending_comments == {"other": 1}
    print("PASS test_dispatch_drops_pending_comments_for_that_task")


class _FinishProbe(bridge.McCliLifecycle):
    def __init__(self, stderr):
        self.calls = []
        self._stderr = stderr

    def _run(self, task_id, args, best_effort=False):
        self.calls.append(tuple(args[:1]))
        return (1, self._stderr)

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", blocker_type))


def test_finish_on_parked_task_raises_no_blocker():
    for status in ("waiting", "inbox", "done", "cancelled"):
        lc = _FinishProbe(
            f"mc finish: Task-Status ist '{status}' — `mc finish` erwartet 'in_progress' oder 'review'."
        )
        lc.finish(TID, "reflection", review=True)
        assert not any(c[0] == "blocker" for c in lc.calls), status
    print("PASS test_finish_on_parked_task_raises_no_blocker")


def test_finish_unexpected_failure_still_blocks():
    lc = _FinishProbe("HTTP 500 boom")
    lc.finish(TID, "reflection", review=True)
    assert ("blocker", "technical_problem") in lc.calls
    print("PASS test_finish_unexpected_failure_still_blocks")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
