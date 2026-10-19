#!/usr/bin/env python3
"""Contract: the bridge reads the task through the REAL `mc task-get` output.

Review finding (19.10.2026, PR #881): ``McCliLifecycle.task_state()`` parses
the JSON object on ``mc task-get``'s stdout. The CLI printed every dict that
has an ``id`` through ``_emit()``'s one-line short form, so ``mc task-get``
printed only the task UUID. ``_query_json`` then found no ``{`` and returned
None -> ``task_is_active`` was always None (fail-open) and the end-of-run
safety net never fired. The ownership tests only fed ``json.dumps(task)``
into a mocked ``subprocess.run`` -- they tested the mock, not the CLI.

These tests run the real ``mc_cli.commands._cmd_task_get`` (fake HTTP client),
capture its stdout and hand exactly that text to the bridge.

  pytest docker/omp-bridge/tests/test_task_get_cli_contract.py -v
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "mc-cli"))

import bridge  # noqa: E402
from mc_cli import commands as mc_commands  # noqa: E402

ME = "7f1c2a9e-0000-4000-8000-000000000001"
OTHER = "7f1c2a9e-0000-4000-8000-000000000002"
TID = "d3e397c0-9983-4d6a-b866-af984d5c534e"
BOARD = "b0a7d000-0000-4000-8000-000000000003"


class _FakeClient:
    """What the backend's GET .../tasks/{t}/detail returns (task.model_dump()
    + assigned_agent_name), as JSON-decoded by the CLI client."""

    def __init__(self, status: str, assigned: str | None):
        self.status = status
        self.assigned = assigned
        self.paths: list = []

    def request(self, method, path, **kw):
        self.paths.append((method, path))
        return {
            "id": TID,
            "board_id": BOARD,
            "title": "Probe",
            "description": "line one\nline two",
            "status": self.status,
            "assigned_agent_id": self.assigned,
            "assigned_agent_name": "alpha",
            "dispatch_attempt_id": "A1",
        }


class _FakeCfg:
    def require_task_context(self):
        return BOARD, TID


def real_task_get_stdout(status: str, assigned: str | None) -> str:
    """Exactly what `mc task-get <id>` prints for this task row."""
    client = _FakeClient(status, assigned)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mc_commands._cmd_task_get(None, client, _FakeCfg())
    assert rc == 0
    assert client.paths == [("GET", f"/api/v1/agent/boards/{BOARD}/tasks/{TID}/detail")]
    return buf.getvalue()


class _Proc:
    def __init__(self, rc: int, stdout: str, stderr: str = ""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class _Row:
    """The task row behind the fake `mc` binary. Reads go through the REAL
    CLI output; writes are recorded and move the row like the backend."""

    def __init__(self, status: str, assigned: str | None = ME):
        self.status = status
        self.assigned = assigned
        self.calls: list = []

    def run(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append(args)
        if args[0] == "task-get":
            return _Proc(0, real_task_get_stdout(self.status, self.assigned),
                         "warning: something on stderr")
        if args[0] == "blocked":
            self.status = "blocked"
        return _Proc(0, "")


def _lc() -> bridge.McCliLifecycle:
    return bridge.McCliLifecycle(
        api_url="http://x", token="t", task_id=TID, board_id=BOARD,
        attempt_id="A1", mc_bin="mc", agent_id=ME,
    )


def _with(row: _Row, fn):
    orig = bridge.subprocess.run
    bridge.subprocess.run = row.run
    try:
        return fn()
    finally:
        bridge.subprocess.run = orig


# ── the CLI side of the contract ─────────────────────────────────────────


def test_task_get_prints_the_task_as_json_not_only_its_id():
    out = real_task_get_stdout("in_progress", ME)
    assert out.strip() != TID, "mc task-get printed only the id (the _emit short form)"
    import json
    data = json.loads(out)
    assert data["status"] == "in_progress"
    assert data["assigned_agent_id"] == ME
    assert data["description"] == "line one\nline two"


# ── the bridge side, fed with the real CLI output ────────────────────────


def test_task_state_reads_status_and_assignee_from_real_cli_output():
    row = _Row("in_progress")
    assert _with(row, lambda: _lc().task_state(TID)) == {
        "status": "in_progress", "assigned_agent_id": ME,
    }


def test_task_is_active_answers_true_and_false_not_none():
    assert _with(_Row("in_progress"), lambda: _lc().task_is_active(TID)) is True
    assert _with(_Row("waiting"), lambda: _lc().task_is_active(TID)) is True
    for status in ("review", "done", "blocked", "inbox", "cancelled"):
        assert _with(_Row(status), lambda: _lc().task_is_active(TID)) is False, status
    assert _with(_Row("in_progress", OTHER), lambda: _lc().task_is_active(TID)) is False


def test_safety_net_blocks_a_task_left_open_with_the_real_cli():
    """The d3e397c0 shape end to end: run over, task still in_progress and
    mine -> comment + technical_problem blocker."""
    row = _Row("in_progress")
    err = io.StringIO()
    with redirect_stderr(err):
        stepped_in = _with(row, lambda: bridge.ensure_task_not_left_open(_lc(), TID, None))
    assert stepped_in is True, err.getvalue()
    verbs = [c[0] for c in row.calls]
    assert "comment" in verbs, row.calls
    assert "blocked" in verbs, row.calls
    blocked = next(c for c in row.calls if c[0] == "blocked")
    assert "technical_problem" in blocked, blocked


def test_safety_net_leaves_a_finished_task_alone_with_the_real_cli():
    row = _Row("review")
    assert _with(row, lambda: bridge.ensure_task_not_left_open(_lc(), TID, None)) is False
    assert [c[0] for c in row.calls] == ["task-get"]


def _silent_abort():
    """omp ended the turn without TASK_COMPLETE and without an error -- the
    model ran `mc finish` itself but left out the sentinel."""
    o = bridge.RunOutcome()
    o.saw_session = True
    o.error_message = "connection refused"
    return o


def test_task_already_in_review_is_not_nudged_or_retried():
    """Review finding (major): with task_state always None the bridge
    re-ran / nudged a card the model had already moved to review (risk of a
    second push). With the real CLI output it stops with halted_external."""
    row = _Row("review")
    runs: list = []
    nudges: list = []

    def run_once():
        runs.append(1)
        return _silent_abort()

    def continue_once(text):
        nudges.append(text)
        return _silent_abort()

    err = io.StringIO()
    with redirect_stderr(err):
        action = _with(row, lambda: bridge.drive_live_run(
            _lc(), run_once, task_id=TID, board_requires_review=True,
            retries_left=2, pre_acked=True,
            continues_left=2, continue_once=continue_once,
        ))
    assert action.action == "halted_external", (action.action, err.getvalue())
    assert len(runs) == 1, "retried a task that is already in review"
    assert nudges == [], "nudged a task that is already in review"
    assert "status=review" in err.getvalue()
    verbs = [c[0] for c in row.calls]
    assert "blocked" not in verbs and "comment" not in verbs, row.calls
