"""`mc msg --attach <datei>` — Bild/Datei an eine Thread-Nachricht hängen.

Live-Befund 07.09.2026 (Gruppenchat): auf „bitte Screenshots" antworteten die
Agenten „technisch nicht möglich" — --vault-path bedient nur den Slack/
Telegram-Spiegel (Gruppen spiegeln nicht), --photo braucht einen Task.

Der Weg: Datei per Multipart an POST /agent/threads/{id}/attachment, der
zurückgegebene absolute Pfad wird als eigene Zeile `[Anhang: <pfad>]` an den
Text gehängt — exakt das Format des Operator-Composers, das Frontend macht
daraus die Kachel.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import client as client_mod  # noqa: E402
from mc_cli import commands  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402


class _Args:
    text = "Mockup 3, Hybrid + Mehr"
    type = "message"
    thread = "thread-uuid"
    vault_path = None
    attach = None


def _client(upload_response=None):
    client = MagicMock()
    calls = []

    def request(method, path, body=None, **kw):
        calls.append({"kind": "request", "method": method, "path": path, "body": body})
        return {"ok": True}

    def upload(path, file_path, **kw):
        calls.append({"kind": "upload", "path": path, "file_path": file_path})
        return upload_response or {
            "path": "/Users/x/.mc/references/agent/abc/0123456789abcdef-mockup.png",
            "name": "mockup.png", "bytes": 3, "isImage": True,
        }

    client.request.side_effect = request
    client.upload.side_effect = upload
    client.calls = calls
    return client


def test_attach_uploads_first_and_appends_the_anhang_line(tmp_path):
    png = tmp_path / "mockup.png"
    png.write_bytes(b"png")

    class A(_Args):
        attach = str(png)

    client = _client()
    rc = commands._cmd_msg(A(), client, MagicMock())
    assert rc == 0

    up, msg = client.calls
    assert up["kind"] == "upload"
    assert up["path"] == "/api/v1/agent/threads/thread-uuid/attachment"
    assert up["file_path"] == str(png)
    assert msg["path"] == "/api/v1/agent/threads/thread-uuid/messages"
    # Eigene Zeile, exakt wie der Composer sie schreibt (attachments.ts).
    assert msg["body"]["body"] == (
        "Mockup 3, Hybrid + Mehr\n"
        "[Anhang: /Users/x/.mc/references/agent/abc/0123456789abcdef-mockup.png]"
    )
    assert "vault_path" not in msg["body"]


def test_attach_requires_an_explicit_thread(tmp_path):
    png = tmp_path / "mockup.png"
    png.write_bytes(b"png")

    class A(_Args):
        thread = None
        attach = str(png)

    with pytest.raises(UsageError, match="--thread"):
        commands._cmd_msg(A(), _client(), MagicMock())


def test_attach_and_vault_path_exclude_each_other(tmp_path):
    png = tmp_path / "mockup.png"
    png.write_bytes(b"png")

    class A(_Args):
        attach = str(png)
        vault_path = "wrappers/files/x.md"

    with pytest.raises(UsageError, match="vault-path"):
        commands._cmd_msg(A(), _client(), MagicMock())


def test_attach_missing_file_fails_before_any_request(tmp_path):
    class A(_Args):
        attach = str(tmp_path / "gibt-es-nicht.png")

    client = _client()
    with pytest.raises(UsageError, match="nicht gefunden"):
        commands._cmd_msg(A(), client, MagicMock())
    assert client.calls == []


def test_msg_parser_accepts_attach():
    from mc_cli.__main__ import build_parser

    args = build_parser().parse_args(
        ["msg", "hallo", "--thread", "t", "--attach", "/tmp/x.png"]
    )
    assert args.attach == "/tmp/x.png"


def test_multipart_body_carries_the_file_bytes():
    """Der Client ist stdlib-only — der Multipart-Körper wird von Hand gebaut.
    Der Test pinnt, dass Dateiname, Content-Type und Bytes drinstehen und die
    Grenze (boundary) im Header UND im Körper dieselbe ist."""
    content_type, body = client_mod.encode_multipart(
        "file", "mockup.png", b"\x89PNG\r\nbytes", "image/png"
    )
    boundary = content_type.split("boundary=", 1)[1]
    assert content_type.startswith("multipart/form-data; boundary=")
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'name="file"; filename="mockup.png"' in body
    assert b"Content-Type: image/png" in body
    assert b"\x89PNG\r\nbytes" in body
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())
