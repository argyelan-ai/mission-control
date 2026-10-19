#!/usr/bin/env python3
"""The bridge never leaves an open task without a blocker and a comment.

Incident 19.10.2026 (task d3e397c0, reconstructed from DB + transcript): the
first run of a bench task ended silently. ``drive_live_run`` asks
``lifecycle.task_is_active()`` before every retry/nudge and stops with
``halted_external`` on ``False`` — no comment, no blocker — and the serve loop
ignored that result. The task stayed ``in_progress`` for 10 minutes until the
operator moved it by hand.

``task_is_active`` compared ``mc me``'s ``current_task.id``. For a worker in
subagent-dispatch mode MC deliberately does NOT set ``current_task_id``
(``agent_task_status.py``, ``agents.py`` claim path), so ``current_task`` is
null and the check said "no longer active" for a task that was very much the
agent's own.

Now:
  * the check reads the TASK (``mc task-get <id>``): active means status
    ``in_progress``/``waiting`` and assigned to me;
  * ``halted_external`` is logged with the status and assignee it saw;
  * after every run the serve loop re-reads the task; one still
    ``in_progress`` and mine gets a comment plus a ``technical_problem``
    blocker.

  pytest docker/omp-bridge/tests/test_task_ownership.py -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import bridge  # noqa: E402
from test_msg_nudge import _finish_outcome  # noqa: E402

ME = "agent-1"
TID = "T1"


# ── McCliLifecycle: ownership comes from the task, not from `mc me` ─────────


class _Proc:
    def __init__(self, rc: int, stdout: str, stderr: str = ""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _fake_mc(task: dict | None, me: dict | None = None, *, rc: int = 0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd[1:])
        if cmd[1] == "task-get":
            if task is None:
                return _Proc(1, "", "HTTP 404")
            return _Proc(rc, json.dumps(task), "some warning on stderr")
        if cmd[1] == "me":
            return _Proc(0, json.dumps(me or {}))
        return _Proc(0, "")
    return run


def _lc(agent_id=ME) -> bridge.McCliLifecycle:
    return bridge.McCliLifecycle(
        api_url="http://x", token="t", task_id=TID, board_id="B",
        attempt_id="A", mc_bin="mc", agent_id=agent_id,
    )


def _with(run_fn, fn):
    orig = bridge.subprocess.run
    bridge.subprocess.run = run_fn
    try:
        return fn()
    finally:
        bridge.subprocess.run = orig


def test_worker_task_without_current_task_is_still_active():
    """The d3e397c0 case: my task, in_progress, but `mc me` says
    current_task null (subagent-dispatch worker)."""
    task = {"id": TID, "status": "in_progress", "assigned_agent_id": ME}
    me = {"id": ME, "current_task": None}
    assert _with(_fake_mc(task, me), lambda: _lc().task_is_active(TID)) is True


def test_task_get_is_read_from_stdout_even_with_stderr_noise():
    task = {"id": TID, "status": "in_progress", "assigned_agent_id": ME}
    state = _with(_fake_mc(task), lambda: _lc().task_state(TID))
    assert state == {"status": "in_progress", "assigned_agent_id": ME}


def test_waiting_is_still_active():
    task = {"id": TID, "status": "waiting", "assigned_agent_id": ME}
    assert _with(_fake_mc(task), lambda: _lc().task_is_active(TID)) is True


def test_closed_task_is_not_active():
    for status in ("review", "done", "cancelled", "blocked", "inbox", "failed"):
        task = {"id": TID, "status": status, "assigned_agent_id": ME}
        assert _with(_fake_mc(task), lambda: _lc().task_is_active(TID)) is False, status


def test_reassigned_task_is_not_active():
    task = {"id": TID, "status": "in_progress", "assigned_agent_id": "someone-else"}
    assert _with(_fake_mc(task), lambda: _lc().task_is_active(TID)) is False
    task = {"id": TID, "status": "in_progress", "assigned_agent_id": None}
    assert _with(_fake_mc(task), lambda: _lc().task_is_active(TID)) is False


def test_undeterminable_stays_fail_open():
    assert _with(_fake_mc(None), lambda: _lc().task_is_active(TID)) is None

    def boom(cmd, **kw):
        raise OSError("no mc binary")

    assert _with(boom, lambda: _lc().task_is_active(TID)) is None
    assert _with(_fake_mc({"id": TID}, rc=0), lambda: _lc().task_is_active(TID)) is None


def test_own_id_falls_back_to_mc_me_once():
    calls: list = []
    task = {"id": TID, "status": "in_progress", "assigned_agent_id": ME}
    lc = _lc(agent_id=None)
    run = _fake_mc(task, {"id": ME, "current_task": None}, calls=calls)
    assert _with(run, lambda: lc.task_is_active(TID)) is True
    assert _with(run, lambda: lc.task_is_active(TID)) is True
    assert sum(1 for c in calls if c[0] == "me") == 1


def test_known_agent_id_never_asks_mc_me():
    calls: list = []
    task = {"id": TID, "status": "in_progress", "assigned_agent_id": ME}
    _with(_fake_mc(task, calls=calls), lambda: _lc().task_is_active(TID))
    assert ["me"] not in calls


# ── drive_live_run: halted_external names what it saw ───────────────────────


class _StatefulLifecycle(bridge.MCLifecycle):
    """Models the task row the way the backend does for the calls we make."""

    def __init__(self, status: str = "inbox", *, finish_moves: bool = True,
                 assigned: str = ME):
        self.status = status
        self.assigned = assigned
        self.finish_moves = finish_moves
        self.calls: list = []

    def ack(self, task_id):
        self.calls.append(("ack", task_id))
        self.status = "in_progress"

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish", task_id, review))
        if self.finish_moves:
            self.status = "review" if review else "done"

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", task_id, blocker_type, question))
        self.status = "blocked"

    def comment(self, task_id, text):
        self.calls.append(("comment", task_id, text))

    def task_state(self, task_id):
        self.last_task_state = {"status": self.status, "assigned_agent_id": self.assigned}
        return self.last_task_state

    def own_agent_id(self, task_id):
        return ME

    def task_is_active(self, task_id):
        state = self.task_state(task_id)
        return state["status"] in ("in_progress", "waiting") and state["assigned_agent_id"] == ME


def _abort_outcome():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.error_message = "connection refused"
    return o


def test_halted_external_logs_status_and_assignee():
    lc = _StatefulLifecycle("review")
    err = io.StringIO()
    with redirect_stderr(err):
        action = bridge.drive_live_run(
            lc, _abort_outcome, task_id=TID, board_requires_review=True,
            retries_left=2, pre_acked=True,
        )
    assert action.action == "halted_external"
    assert "status=review" in err.getvalue()
    assert f"assigned={ME}" in err.getvalue()


# ── serve_loop safety net: never an open task without blocker + comment ────


def _serve(lc, run_once, d):
    task = {"id": TID, "board_id": "B", "prompt": "# New Task: Probe",
            "dispatch_attempt_id": "A1"}
    it = iter([{"state": "new_task", "task": task, "my_agent_id": ME}])
    bridge.serve_loop(
        poll_interval=0, max_iterations=1,
        _poll_fn=lambda: next(it, {"state": "idle"}),
        _lifecycle_factory=lambda t: lc,
        _run_factory=lambda t, cwd: run_once,
        _sleep=lambda _s: None,
        _context_env_path=os.path.join(d, "ctx.env"),
        _msg_queue_dir=os.path.join(d, "queue"), _msg_ack_dir=os.path.join(d, "ack"),
        _task_lock_path=os.path.join(d, "task.lock"),
        _nudge_state_file=os.path.join(d, "nudge-state"),
        _nudge_msg_file=os.path.join(d, "nudge.msg"),
    )


def test_run_that_leaves_the_task_open_gets_a_blocker_and_a_comment():
    """finish() returned, but the task is still in_progress and mine (any
    path that forgets the terminal state) -> the net blocks it, loudly."""
    lc = _StatefulLifecycle(finish_moves=False)
    with tempfile.TemporaryDirectory() as d:
        _serve(lc, _finish_outcome, d)
    blockers = [c for c in lc.calls if c[0] == "blocker"]
    assert len(blockers) == 1, lc.calls
    assert blockers[0][2] == "technical_problem"
    assert "noch offen" in blockers[0][3]
    assert any(c[0] == "comment" and "noch offen" in c[2] for c in lc.calls)


def test_a_properly_finished_task_is_left_alone():
    lc = _StatefulLifecycle()
    with tempfile.TemporaryDirectory() as d:
        _serve(lc, _finish_outcome, d)
    assert lc.status == "review"
    assert not [c for c in lc.calls if c[0] == "blocker"]


def test_a_task_someone_else_owns_now_is_left_alone():
    lc = _StatefulLifecycle(finish_moves=False, assigned="someone-else")
    with tempfile.TemporaryDirectory() as d:
        _serve(lc, _finish_outcome, d)
    assert not [c for c in lc.calls if c[0] == "blocker"]


def test_the_agent_id_from_poll_reaches_the_real_lifecycle():
    """serve_loop hands poll's my_agent_id to McCliLifecycle, so ownership is
    checked without an extra `mc me` round trip."""
    lc = bridge.McCliLifecycle(api_url="u", token="t", task_id=TID, board_id="B",
                               attempt_id="A", agent_id=ME)
    assert lc.agent_id == ME
