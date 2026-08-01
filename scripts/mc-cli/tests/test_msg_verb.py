"""`mc msg` — Datei-Anhang via --vault-path (Slack-Umbau R3).

Diese Tests existieren, weil genau dieses Flag einmal STILL verloren ging:
die Backend-Seite (MessageCreate.vault_path) hatte Tests, die CLI-Seite
nicht — ein `git checkout` im Worktree warf die uncommitteten CLI-Zeilen
weg und nichts wurde rot. Jetzt pinnt die Suite beide Haelften.
"""
import os
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402


class _Args:
    text = "Beleg anbei"
    type = "message"
    thread = None
    vault_path = None


def _client(response=None):
    client = MagicMock()
    calls = []

    def request(method, path, body=None, **kw):
        calls.append({"method": method, "path": path, "body": body})
        return response if response is not None else {"ok": True}

    client.request.side_effect = request
    client.calls = calls
    return client


def test_msg_vault_path_travels_in_the_body():
    class A(_Args):
        vault_path = "wrappers/files/beleg.md"

    client = _client()
    rc = commands._cmd_msg(A(), client, MagicMock())
    assert rc == 0
    call = client.calls[0]
    assert call["path"] == "/api/v1/agent/tasks/current/messages"
    assert call["body"]["vault_path"] == "wrappers/files/beleg.md"


def test_msg_without_vault_path_sends_no_field():
    client = _client()
    commands._cmd_msg(_Args(), client, MagicMock())
    assert "vault_path" not in client.calls[0]["body"]


def test_msg_vault_path_works_with_explicit_thread():
    class A(_Args):
        thread = "thread-uuid"
        vault_path = "wrappers/files/beleg.md"

    client = _client()
    commands._cmd_msg(A(), client, MagicMock())
    call = client.calls[0]
    assert call["path"] == "/api/v1/agent/threads/thread-uuid/messages"
    assert call["body"]["vault_path"] == "wrappers/files/beleg.md"


def test_msg_parser_accepts_vault_path():
    from mc_cli.__main__ import build_parser

    args = build_parser().parse_args(
        ["msg", "hallo", "--vault-path", "wrappers/files/x.md"]
    )
    assert args.vault_path == "wrappers/files/x.md"
