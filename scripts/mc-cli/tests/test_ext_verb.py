"""Tests for `mc ext` — the agent's way to ask for a capability.

`mc ext request` files an install request (skills only) that waits for the
operator; it never installs anything itself. `mc ext search` looks in the
local skills library, `mc ext status` shows the agent's own requests.
"""
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.__main__ import build_parser  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
BOARD = "33333333-3333-3333-3333-333333333333"
CHAT = "44444444-4444-4444-4444-444444444444"
TASK = "55555555-5555-5555-5555-555555555555"
APPROVAL = "66666666-6666-6666-6666-666666666666"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MC_CHAT_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _client(responses=None):
    responses = responses or {}
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, query=None, **kw):
        client.calls.append((method, path, body, query))
        if (method, path) in responses:
            return responses[(method, path)]
        if path == "/api/v1/agent/me":
            return {"id": ME, "board_id": BOARD}
        if path == f"/api/v1/agent/boards/{BOARD}/agents":
            return [{"id": ME, "name": "Me"}, {"id": OTHER, "name": "Helper"}]
        if method == "POST":
            return {"approval_id": APPROVAL, "status": "pending", "existing": False}
        return {}

    client.request.side_effect = request
    return client


def _cfg(task_id=None):
    cfg = MagicMock()
    cfg.task_id = task_id
    cfg.board_id = None
    return cfg


def _args(argv):
    return build_parser().parse_args(argv)


def _post_body(client):
    posts = [c for c in client.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == "/api/v1/agent/install-requests"
    return posts[0][2]


def test_request_files_a_skill_request_for_myself(capsys):
    client = _client()
    rc = commands._cmd_ext(_args([
        "ext", "request", "paperless-filing",
        "--source", "github:anthropic/paperless-filing",
        "--reason", "operator asked to file letters",
    ]), client, _cfg())
    assert rc == 0
    body = _post_body(client)
    assert body["type"] == "skill"
    assert body["operation"] == "install"
    assert body["name"] == "paperless-filing"
    assert body["source"] == "github:anthropic/paperless-filing"
    assert body["target_agent_id"] == ME
    assert "ref" not in body and "chat_id" not in body and "task_id" not in body
    out = capsys.readouterr().out
    assert APPROVAL in out and "mc ext status" in out


def test_request_passes_ref_target_chat_and_task(capsys):
    client = _client()
    commands._cmd_ext(_args([
        "ext", "request", "x-skill", "--source", "github:obra/x-skill",
        "--ref", "abc123", "--for", "helper", "--chat", CHAT,
        "--reason", "helper needs it for the card",
    ]), client, _cfg(task_id=TASK))
    body = _post_body(client)
    assert body["ref"] == "abc123"
    assert body["target_agent_id"] == OTHER
    assert body["chat_id"] == CHAT
    assert body["task_id"] == TASK


def test_request_takes_the_chat_from_the_environment(monkeypatch):
    monkeypatch.setenv("MC_CHAT_ID", CHAT)
    client = _client()
    commands._cmd_ext(_args([
        "ext", "request", "x", "--source", "~/.mc/skills/x", "--reason", "needed now",
    ]), client, _cfg())
    assert _post_body(client)["chat_id"] == CHAT


def test_request_finds_the_chat_of_a_further_claude_chat(monkeypatch):
    # A further Claude Code chat runs with CLAUDE_CONFIG_DIR=<config>/chats/<chat id>/
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"/home/agent/.claude/chats/{CHAT}/")
    client = _client()
    commands._cmd_ext(_args([
        "ext", "request", "x", "--source", "~/.mc/skills/x", "--reason", "needed now",
    ]), client, _cfg())
    assert _post_body(client)["chat_id"] == CHAT


def test_request_ignores_a_config_dir_that_is_not_a_chat(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/agent/.claude")
    client = _client()
    commands._cmd_ext(_args([
        "ext", "request", "x", "--source", "~/.mc/skills/x", "--reason", "needed now",
    ]), client, _cfg())
    assert "chat_id" not in _post_body(client)


def test_request_unknown_target_is_a_usage_error():
    with pytest.raises(UsageError):
        commands._cmd_ext(_args([
            "ext", "request", "x", "--source", "~/.mc/skills/x",
            "--for", "nobody", "--reason", "needed now",
        ]), _client(), _cfg())


def test_request_says_when_the_same_request_is_already_waiting(capsys):
    client = _client({("POST", "/api/v1/agent/install-requests"):
                      {"approval_id": APPROVAL, "status": "pending", "existing": True}})
    commands._cmd_ext(_args([
        "ext", "request", "x", "--source", "~/.mc/skills/x", "--reason", "needed now",
    ]), client, _cfg())
    assert "already" in capsys.readouterr().out


def test_request_needs_source_and_reason():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ext", "request", "x", "--reason", "needed now"])
    with pytest.raises(SystemExit):
        parser.parse_args(["ext", "request", "x", "--source", "~/.mc/skills/x"])


def test_search_reads_the_search_route(capsys):
    client = _client({("GET", "/api/v1/agent/install-requests/search"): {
        "library": [{"name": "paperless-filing", "description": "File letters",
                     "source": "~/.mc/skills/paperless-filing"}],
        "allowed_sources": ["^github:(anthropic)/[a-z0-9_-]+$"],
    }})
    rc = commands._cmd_ext(_args(["ext", "search", "file", "letters"]), client, _cfg())
    assert rc == 0
    assert client.calls == [("GET", "/api/v1/agent/install-requests/search", None,
                             {"q": "file letters"})]
    out = capsys.readouterr().out
    assert "paperless-filing" in out and "~/.mc/skills/paperless-filing" in out
    assert "github:" in out


def test_status_with_id_reads_one_request(capsys):
    client = _client({("GET", f"/api/v1/agent/install-requests/{APPROVAL}"): {
        "approval_id": APPROVAL, "status": "approved", "name": "x",
        "action_type": "install_skill", "commit": "0123456789abcdef",
        "failure_reason": None,
    }})
    rc = commands._cmd_ext(_args(["ext", "status", APPROVAL]), client, _cfg())
    assert rc == 0
    out = capsys.readouterr().out
    assert "approved" in out and "x" in out


def test_status_without_id_lists_my_requests(capsys):
    client = _client({("GET", "/api/v1/agent/install-requests"): {"requests": [
        {"approval_id": APPROVAL, "status": "pending", "name": "x",
         "action_type": "install_skill", "commit": None, "failure_reason": None},
    ]}})
    commands._cmd_ext(_args(["ext", "status"]), client, _cfg())
    out = capsys.readouterr().out
    assert APPROVAL in out and "pending" in out


def test_status_json_prints_the_raw_answer(capsys):
    answer = {"requests": []}
    client = _client({("GET", "/api/v1/agent/install-requests"): answer})
    commands._cmd_ext(_args(["ext", "status", "--json"]), client, _cfg())
    assert json.loads(capsys.readouterr().out) == answer


def test_ext_is_registered_without_a_scope():
    spec = commands.REGISTRY["ext"]
    assert spec.scope == ""
    assert "POST /install-requests" in spec.endpoints
