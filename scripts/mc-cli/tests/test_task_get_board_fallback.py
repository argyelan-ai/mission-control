"""`mc task-get <id>` works without BOARD_ID in the env (round-2 review r1 S1,
23.10.2026).

The chat session of an omp agent runs `mc` without the run's task context:
the chat daemon strips the run ids and points MC_CONTEXT_ENV_PATH at its own
file, and BOARD_ID is set only for a run. The run-context block and the
chat guard's 409 both tell the chat to read with `mc task-get <id>` — which
died with "TASK_ID und BOARD_ID müssen in der Env gesetzt sein". The live
answer then had a failed tool call and an ochre summary (user test t1
NICE-3). Now the board comes from the caller's own `/agent/me`, the same
fallback `mc delegate` uses.

  pytest scripts/mc-cli/tests/test_task_get_board_fallback.py -v
"""
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import _cmd_task_get  # noqa: E402
from mc_cli.config import Config  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

TID = "d3e397c0-9983-4d6a-b866-af984d5c534e"
BOARD = "b0a7d000-0000-4000-8000-000000000003"


class _Client:
    def __init__(self, me=None):
        self.me = {"id": "agent-1", "board_id": BOARD} if me is None else me
        self.calls: list = []

    def request(self, method, path, **kw):
        self.calls.append((method, path))
        if path == "/api/v1/agent/me":
            return self.me
        return {"id": TID, "board_id": BOARD, "status": "in_progress", "title": "Probe"}


def _cfg(*, task_id=None, board_id=None) -> Config:
    return Config(api_url="http://x", agent_token="t", task_id=task_id,
                  board_id=board_id, dispatch_attempt_id=None, session_role="chat")


def _run(cfg, client) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert _cmd_task_get(None, client, cfg) == 0
    return buf.getvalue()


def test_task_get_with_an_id_and_no_board_reads_the_board_from_me():
    client = _Client()
    out = _run(_cfg(task_id=TID), client)
    assert client.calls == [
        ("GET", "/api/v1/agent/me"),
        ("GET", f"/api/v1/agent/boards/{BOARD}/tasks/{TID}/detail"),
    ]
    assert '"status": "in_progress"' in out


def test_task_get_in_a_run_uses_the_env_board_without_an_extra_call():
    client = _Client()
    _run(_cfg(task_id=TID, board_id="run-board"), client)
    assert client.calls == [("GET", f"/api/v1/agent/boards/run-board/tasks/{TID}/detail")]


def test_task_get_without_any_task_id_still_says_what_is_missing():
    with pytest.raises(UsageError):
        _cmd_task_get(None, _Client(), _cfg())


def test_task_get_without_a_board_anywhere_says_so():
    with pytest.raises(UsageError, match="BOARD_ID"):
        _cmd_task_get(None, _Client(me={"id": "agent-1", "board_id": None}), _cfg(task_id=TID))
