"""Stille Delegation ist ein Bug (Live-Vorfall 2026-09-16).

`mc delegate --description "..."` endete mit Exit 0, ohne Ausgabe und ohne
angelegte Karte. Die Ursprungshypothese "$ wird von der Shell verschluckt"
war falsch (empirisch widerlegt: ein `$` in der Beschreibung kommt unveraendert
beim Server an). Die echte Ursache: `client._send` gab bei leerem 2xx-Body
`None` zurueck, `_emit(None)` ist ein stiller No-Op, und `_cmd_delegate`
endete mit `return 0`.

Zwei Schichten schliessen das jetzt:
  1. `_send` faellt hart bei leerem 2xx-Body (ausser der Aufrufer will
     ausdruecklich keinen Body — `allow_empty`, z.B. die 204 von
     `GET /tasks/next` als "kein Werk").
  2. `_require_result` faengt den `null`-Body, der Schicht 1 passiert
     (nicht leer, sondern wortwoertlich "null").

Der Test faehrt einen echten lokalen HTTP-Server (kein Mock des HTTP-Layers)
und prueft beide Richtungen plus die Gegenprobe, dass ein gueltiges Ergebnis
weiterhin durchgeht und Exit 0 liefert.
"""

import json
import os
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import pytest  # noqa: E402

from mc_cli.client import Client  # noqa: E402
from mc_cli.config import Config  # noqa: E402
from mc_cli.commands import _cmd_delegate, _require_result  # noqa: E402
from mc_cli.errors import ServerError  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    """Antwortet mit dem Body/Status, den der jeweilige Test setzt."""

    mode = "json201"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        mode = _Handler.mode
        if mode == "json201":
            body = json.dumps(
                {"subtask_id": "s1", "assigned_to": "Dev", "your_status": "in_progress"}
            ).encode()
            self.send_response(201)
        elif mode == "empty201":
            body = b""
            self.send_response(201)
        elif mode == "empty204":
            body = b""
            self.send_response(204)
        elif mode == "null201":
            body = b"null"
            self.send_response(201)
        else:  # pragma: no cover - Sicherheitsnetz
            body = b"{}"
            self.send_response(201)

        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    do_PATCH = do_POST

    def log_message(self, *args):  # Testlauf nicht zumuellen
        pass


@pytest.fixture(scope="module")
def stub_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def client(stub_server, monkeypatch):
    monkeypatch.setenv("MC_API_URL", stub_server)
    monkeypatch.setenv("MC_AGENT_TOKEN", "tok")
    return Client(Config.from_env())


@dataclass(frozen=True)
class _Cfg:
    board_id: str = "b1"
    task_id: str = "t1"

    def require_task_context(self):
        return self.board_id, self.task_id


class _Args:
    def __init__(self, **kw):
        defaults = dict(
            title="Sub",
            to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001",
            description="Do the concrete thing please",
            priority=None,
            no_callback=False,
            origin_thread=None,
            parent=None,
            repo=None,
            no_repo_reason=None,
        )
        defaults.update(kw)
        self.__dict__.update(defaults)


# ── Schicht 1: leere 2xx-Bodies ──────────────────────────────────────────


@pytest.mark.parametrize("mode", ["empty201", "empty204"])
def test_empty_body_is_not_a_silent_success(client, mode):
    """Der Vorfall selbst: der Server bestaetigt, liefert aber nichts. Ein
    Exit 0 waere eine Luege — es gibt keinen Beleg, dass eine Karte entstand."""
    _Handler.mode = mode
    with pytest.raises(ServerError) as exc:
        client.request("POST", "/api/v1/x", body={"a": 1})
    assert "leere Antwort" in str(exc.value)
    assert exc.value.exit_code == 2


# ── Schicht 2: wortwoertliches `null` ────────────────────────────────────


def test_null_body_reaches_the_verb_and_is_caught_there(client):
    """`null` ist NICHT leer und passiert `_send`. Genau diese Luecke schliesst
    `_require_result` — ohne sie waere die zweite Schicht wirkungslos."""
    _Handler.mode = "null201"
    resp = client.request("POST", "/api/v1/x", body={"a": 1})
    assert resp is None  # Schicht 1 laesst das durch, wie beabsichtigt

    with pytest.raises(ServerError) as exc:
        _require_result(resp, verb="mc delegate")
    assert "unbelegt" in str(exc.value)
    assert exc.value.exit_code == 2


# ── Erlaubter Fall: echtes Ergebnis geht durch ───────────────────────────


def test_valid_json_still_passes(client):
    _Handler.mode = "json201"
    resp = client.request("POST", "/api/v1/x", body={"a": 1})
    assert resp == {"subtask_id": "s1", "assigned_to": "Dev", "your_status": "in_progress"}
    assert _require_result(resp, verb="mc delegate") is resp


def test_allow_empty_is_an_explicit_opt_in(client):
    """Die 204 von `GET /tasks/next` heisst bewusst "kein Werk" — ein Aufrufer,
    der das erwartet, darf `None` bekommen. Der Default bleibt hart."""
    _Handler.mode = "empty204"
    assert client.request("POST", "/api/v1/x", allow_empty=True) is None


# ── Das Verb selbst: Ende-zu-Ende ueber echtes HTTP ──────────────────────


def test_delegate_fails_loudly_on_empty_body(client):
    _Handler.mode = "empty201"
    with pytest.raises(ServerError):
        _cmd_delegate(_Args(), client, _Cfg())


def test_delegate_fails_loudly_on_null_body(client):
    _Handler.mode = "null201"
    with pytest.raises(ServerError) as exc:
        _cmd_delegate(_Args(), client, _Cfg())
    assert "unbelegt" in str(exc.value)


def test_delegate_succeeds_with_real_receipt(client, capsys):
    """Gegenprobe: mit echtem Ergebnis bleibt Exit 0 und die Kartennummer
    steht auf stdout — die Fixes duerfen den Normalfall nicht brechen."""
    _Handler.mode = "json201"
    rc = _cmd_delegate(_Args(), client, _Cfg())
    assert rc == 0
    assert "s1" in capsys.readouterr().out


# ── Mutation-Guard ───────────────────────────────────────────────────────


def test_require_result_is_not_a_noop():
    """Mutation-Guard: waere `_require_result` ein Durchreicher, wuerde der
    `null`-Fall wieder still mit 0 enden. Der Kontrastfall pinnt fest, dass
    die Funktion bei `None` wirft und bei Inhalt durchlaesst."""
    with pytest.raises(ServerError):
        _require_result(None, verb="mc delegate")
    payload = {"subtask_id": "s1"}
    assert _require_result(payload, verb="mc delegate") == payload


def test_shell_dollar_hypothesis_still_reaches_the_server(client):
    """Die widerlegte Hypothese als Gegenprobe: ein `$` in der Beschreibung
    kommt unveraendert an. Waere die Shell-Expansion die Ursache gewesen,
    muesste hier $TOKEN fehlen — das Gegenteil wird festgehalten."""
    _Handler.mode = "json201"
    seen = {}

    class _Cap(Client):
        def request(self, method, path, body=None, **kw):
            seen["body"] = body
            return super().request(method, path, body=body, **kw)

    cap = _Cap(Config.from_env())
    _cmd_delegate(_Args(description="Kosten von $100 pruefen"), cap, _Cfg())
    assert seen["body"]["description"] == "Kosten von $100 pruefen"
