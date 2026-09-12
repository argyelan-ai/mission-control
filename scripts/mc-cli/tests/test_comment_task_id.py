"""`mc comment --task-id` (W5-C) — a Board Lead without an active task needs
a way to comment on a foreign card. Pins that `--task-id` routes to the new
board-agnostic endpoint instead of `cfg.require_task_context()`, and that
plain `mc comment` (no --task-id) keeps hitting the old board-scoped path
unchanged.
"""
import os
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402


class _Args:
    type = "handoff"
    message = "Bitte weitermachen"
    task_id = None


def _client(response=None):
    client = MagicMock()
    calls = []

    def request(method, path, body=None, **kw):
        calls.append({"method": method, "path": path, "body": body})
        return response if response is not None else {"id": "c1"}

    client.request.side_effect = request
    client.calls = calls
    return client


class _Cfg:
    def require_task_context(self):
        return "board-1", "task-1"


def test_comment_with_task_id_skips_active_task_context():
    class A(_Args):
        task_id = "foreign-task-99"

    client = _client()
    cfg = MagicMock()
    cfg.require_task_context.side_effect = AssertionError(
        "must not need active task context when --task-id is given"
    )
    rc = commands._cmd_comment(A(), client, cfg)
    assert rc == 0
    call = client.calls[0]
    assert call["path"] == "/api/v1/agent/tasks/foreign-task-99/comments"
    assert call["body"] == {"comment_type": "handoff", "content": "Bitte weitermachen"}


def test_comment_without_task_id_uses_active_task_context():
    client = _client()
    rc = commands._cmd_comment(_Args(), client, _Cfg())
    assert rc == 0
    call = client.calls[0]
    assert call["path"] == "/api/v1/agent/boards/board-1/tasks/task-1/comments"


def test_comment_parser_accepts_task_id_flag():
    from mc_cli.__main__ import build_parser

    args = build_parser().parse_args(
        ["comment", "handoff", "hallo", "--task-id", "abc-123"]
    )
    assert args.task_id == "abc-123"


def test_comment_parser_task_id_defaults_to_none():
    from mc_cli.__main__ import build_parser

    args = build_parser().parse_args(["comment", "message", "hallo"])
    assert args.task_id is None
