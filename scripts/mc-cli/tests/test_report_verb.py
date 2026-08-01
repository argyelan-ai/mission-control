"""`mc report` — kanal-neutraler Operator-Report + `mc telegram` als Alias.

Das Backend hat einen kanal-neutralen Report-Weg: POST /me/report ist die
kanonische Route, /me/telegram und /telegram/send bleiben Aliasse. Die CLI
spiegelt das: `mc report` ist das kanonische Verb, `mc telegram` der
historische Alias — EIN Handler, zwei REGISTRY-Eintraege.

Wichtig fuers Verstaendnis: die `endpoints`-Tupel der CommandSpecs sind reine
Introspektion (backend/tests/test_mc_cli_endpoints.py) — es gibt KEINEN
automatischen Fallback-Mechanismus im Client. Das 404-Hopping ist hand-codiert
im Handler (Muster von `mc telegram` uebernommen); diese Tests pinnen die
Kette /me/report → /me/telegram → /telegram/send.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.errors import ClientError, UsageError  # noqa: E402


class _Args:
    command = "report"
    text = "Report fertig: alles gruen."
    photo = None
    file = None
    vault_path = None
    task_id = None


def _client(responses):
    """responses: list of (path_substr, value-or-exception), in call order."""
    client = MagicMock()
    calls = []

    def request(method, path, body=None, **kw):
        calls.append({"method": method, "path": path, "body": body})
        for i, (p, value) in enumerate(responses):
            if p in path:
                responses.pop(i)
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unmocked request: {method} {path}")

    client.request.side_effect = request
    client.calls = calls
    return client


def _cfg():
    cfg = MagicMock()
    cfg.require_task_context.return_value = ("board-uuid", "task-uuid")
    return cfg


# ── (a) `mc report` sendet an /me/report ───────────────────────────────────


def test_report_posts_to_me_report():
    client = _client([
        ("/me/report", {"ok": True, "message_id": 42, "channels": ["slack"]}),
    ])
    rc = commands._cmd_report(_Args(), client, _cfg())
    assert rc == 0
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/agent/me/report"
    assert call["body"]["text"] == _Args.text
    assert call["body"]["task_id"] == "task-uuid"


def test_report_emits_channel_neutral_response(capsys):
    """Neue Antwortform {ok, message_id, channels} hat kein 'id'-Feld →
    _emit druckt das volle JSON, nicht den id-Shortcut."""
    client = _client([
        ("/me/report", {"ok": True, "message_id": None, "channels": ["telegram", "slack"]}),
    ])
    commands._cmd_report(_Args(), client, _cfg())
    out = capsys.readouterr().out
    assert "channels" in out and "slack" in out


# ── (b) 404 auf /me/report → Fallback /me/telegram (→ /telegram/send) ──────


def test_report_falls_back_to_me_telegram_on_404():
    client = _client([
        ("/me/report", ClientError("HTTP 404 Not Found")),
        ("/me/telegram", {"ok": True, "message_id": 1, "channels": ["telegram"]}),
    ])
    rc = commands._cmd_report(_Args(), client, _cfg())
    assert rc == 0
    paths = [c["path"] for c in client.calls]
    assert paths == ["/api/v1/agent/me/report", "/api/v1/agent/me/telegram"]
    # Same body on the fallback — the schema is identical by contract.
    assert client.calls[0]["body"] == client.calls[1]["body"]


def test_report_falls_all_the_way_back_to_telegram_send():
    """Aeltestes Backend ohne /me/* — dritter Eintrag der Kette."""
    client = _client([
        ("/me/report", ClientError("HTTP 404 Not Found")),
        ("/me/telegram", ClientError("HTTP 404 Not Found")),
        ("/telegram/send", {"ok": True}),
    ])
    rc = commands._cmd_report(_Args(), client, _cfg())
    assert rc == 0
    assert client.calls[-1]["path"] == "/api/v1/agent/telegram/send"


def test_report_does_not_swallow_non_404_errors():
    """Nur 404 heisst 'Route existiert nicht' — ein 422 (z.B. Caption zu
    lang) muss sofort propagieren, kein blinder Retry auf dem Alias."""
    client = _client([
        ("/me/report", ClientError("HTTP 422 Unprocessable Entity")),
    ])
    with pytest.raises(ClientError):
        commands._cmd_report(_Args(), client, _cfg())
    assert len(client.calls) == 1


def test_report_404_on_last_endpoint_propagates():
    """Kette erschoepft → der letzte 404 fliegt, kein stilles None-Emit."""
    client = _client([
        ("/me/report", ClientError("HTTP 404 Not Found")),
        ("/me/telegram", ClientError("HTTP 404 Not Found")),
        ("/telegram/send", ClientError("HTTP 404 Not Found")),
    ])
    with pytest.raises(ClientError):
        commands._cmd_report(_Args(), client, _cfg())


# ── (d) `mc telegram` bleibt voll funktionsfaehig ──────────────────────────


def test_telegram_is_the_same_handler():
    assert commands.REGISTRY["telegram"].handler is commands.REGISTRY["report"].handler
    assert commands._cmd_telegram is commands._cmd_report


def test_telegram_still_delivers_via_the_chain():
    """Alias-Aufruf durch den gemeinsamen Handler: neues Backend → /me/report
    beantwortet den Alias genauso (identisches Body-Schema)."""

    class A(_Args):
        command = "telegram"

    client = _client([
        ("/me/report", {"ok": True, "message_id": 7, "channels": ["telegram"]}),
    ])
    rc = commands._cmd_telegram(A(), client, _cfg())
    assert rc == 0
    assert client.calls[0]["body"]["text"] == _Args.text


def test_telegram_on_old_backend_still_reaches_me_telegram():
    """Laufende Sessions auf altem Backend: /me/report 404t → /me/telegram
    traegt wie bisher."""

    class A(_Args):
        command = "telegram"

    client = _client([
        ("/me/report", ClientError("HTTP 404 Not Found")),
        ("/me/telegram", {"ok": True}),
    ])
    rc = commands._cmd_telegram(A(), client, _cfg())
    assert rc == 0
    assert client.calls[-1]["path"] == "/api/v1/agent/me/telegram"


# ── REGISTRY-Vertrag ───────────────────────────────────────────────────────


def test_report_registered_with_chat_write_scope():
    """Backend-Kontrakt: CANONICAL_VERB_SCOPES['report'] = 'chat:write' —
    test_verb_scopes_match_the_mc_cli_registry_values bindet beide Seiten."""
    spec = commands.REGISTRY["report"]
    assert spec.scope == "chat:write"
    assert spec.endpoints[0] == "POST /me/report"
    assert "POST /me/telegram" in spec.endpoints


def test_telegram_help_marks_the_alias():
    assert "Alias" in commands.REGISTRY["telegram"].help
    assert "mc report" in commands.REGISTRY["telegram"].help


def test_both_verbs_parse_identical_options():
    """argparse-Ebene: beide Verben nehmen text/--photo/--file gleich an."""
    from mc_cli.__main__ import build_parser

    parser = build_parser()
    for verb in ("report", "telegram"):
        args = parser.parse_args([verb, "hallo", "--photo", "d-1"])
        assert args.command == verb
        assert args.text == "hallo"
        assert args.photo == "d-1"
        assert args.file is None


def test_report_photo_and_file_are_mutually_exclusive():
    class A(_Args):
        photo = "d-1"
        file = "d-2"

    with pytest.raises(UsageError):
        commands._cmd_report(A(), _client([]), _cfg())


# ── --vault-path: Ad-hoc-Datei ohne Deliverable (Slack-Umbau R3) ───────────


def test_report_vault_path_travels_in_the_body():
    class A(_Args):
        vault_path = "wrappers/files/report.md"

    client = _client([("/me/report", {"ok": True, "channels": ["slack"]})])
    rc = commands._cmd_report(A(), client, _cfg())
    assert rc == 0
    assert client.calls[0]["body"]["vault_path"] == "wrappers/files/report.md"
    assert "deliverable_id" not in client.calls[0]["body"]


def test_report_vault_path_excludes_the_deliverable_flags():
    class A(_Args):
        vault_path = "wrappers/files/report.md"
        photo = "d-1"

    with pytest.raises(UsageError):
        commands._cmd_report(A(), _client([]), _cfg())


def test_both_verbs_parse_vault_path():
    from mc_cli.__main__ import build_parser

    parser = build_parser()
    for verb in ("report", "telegram"):
        args = parser.parse_args([verb, "hallo", "--vault-path", "wrappers/f/x.md"])
        assert args.vault_path == "wrappers/f/x.md"


def test_report_reads_stdin_on_dash(monkeypatch):
    """Stdin-Konvention gilt fuer das neue Verb ab Tag eins."""
    import io

    class _Stdin(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _Stdin("Report via heredoc"))

    class A(_Args):
        text = "-"

    client = _client([("/me/report", {"ok": True})])
    commands._cmd_report(A(), client, MagicMock())
    assert client.calls[0]["body"]["text"] == "Report via heredoc"
