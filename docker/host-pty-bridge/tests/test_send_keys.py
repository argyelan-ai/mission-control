"""host-pty-bridge — `?mode=keys`: Zustellung per `tmux send-keys` mit Ack.

Hintergrund (05.09.2026): Der Chat schrieb Boss-Nachrichten als rohe Bytes in
ein Wegwerf-Pseudo-Terminal, in dem gerade erst `tmux attach` anlief, und
schloss die Verbindung direkt nach dem letzten Byte (-> tmux-Client wird
beendet). Beides verliert Bytes je nach Timing: einmal fehlte das Enter
(Text sass unabgeschickt in der Eingabe), einmal der ganze Text — und die
Bridge loggte trotzdem "wrote 57 bytes". `send-keys` geht direkt an den
tmux-Server, ohne Terminal, und die Bridge antwortet erst, wenn tmux
zurueck ist.

Standalone: python3 -m pytest docker/host-pty-bridge/tests -q
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402

SOCK = "/tmp/tmux-501/default"


def test_literal_key_becomes_send_keys_dash_l():
    argv = server.send_keys_argv(SOCK, "boss-host:0", {"literal": "prüfe task 957"})
    assert argv == [
        "tmux", "-S", SOCK, "send-keys", "-t", "boss-host:0", "-l", "--", "prüfe task 957",
    ]


def test_named_key_becomes_plain_send_keys():
    argv = server.send_keys_argv(SOCK, "boss-host:0", {"named": "Enter"})
    assert argv == ["tmux", "-S", SOCK, "send-keys", "-t", "boss-host:0", "Enter"]


@pytest.mark.parametrize("bad", [
    {"named": "C-c"},            # nicht in der Allowliste
    {"named": "; rm -rf /"},
    {"literal": 5},
    {"unknown": "x"},
    "Enter",
])
def test_rejects_anything_outside_the_allowlist(bad):
    with pytest.raises(ValueError):
        server.send_keys_argv(SOCK, "boss-host:0", bad)


class _FakeWS:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def send(self, msg):
        self.sent.append(json.loads(msg))


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_keys_mode_runs_every_key_in_order_then_acks():
    calls = []

    async def fake_run(argv):
        calls.append(argv)
        return ""  # leerer pane_mode: kein Mode aktiv

    ws = _FakeWS([json.dumps({"type": "send_keys", "keys": [
        {"literal": "hallo"}, {"named": "Enter"},
    ]})])
    _run(server.keys_handler(ws, "boss-host:0", SOCK, run=fake_run))

    assert calls[0][-1] == "#{pane_mode}"       # Mode-Probe geht den Keys voraus
    assert [c[-1] for c in calls[1:]] == ["hallo", "Enter"]
    assert ws.sent == [{"type": "ack", "ok": True, "sent": 2}]


def test_keys_mode_refuses_the_batch_when_pane_is_in_copy_mode():
    """Copy-mode verschluckt send-keys bei rc=0 still — die Bridge muss den
    Batch ablehnen (ok:false, pane_mode), statt Erfolg zu melden. Der Chat
    macht daraus 502, der Operator sieht den Fehler statt einer verschwundenen
    Nachricht. Faellt der Check weg, laeuft dieser Test auf ok:true."""
    calls = []

    async def fake_run(argv):
        calls.append(argv)
        if "display-message" in argv:
            return "copy-mode"
        return ""

    ws = _FakeWS([json.dumps({"type": "send_keys", "keys": [
        {"literal": "hallo"}, {"named": "Enter"},
    ]})])
    _run(server.keys_handler(ws, "boss-host:0", SOCK, run=fake_run))

    assert [c for c in calls if "send-keys" in c] == []   # keine Taste ging raus
    assert ws.sent == [{"type": "ack", "ok": False, "pane_mode": "copy-mode",
                        "error": "pane is in 'copy-mode'; keys would be swallowed — leave copy-mode first"}]


def test_keys_mode_probe_failure_falls_through_to_send_keys():
    """Schlaegt die Mode-Probe fehl (z. B. Server weg), entscheidet send-keys
    selbst — dessen rc/Fehler landet eh im Ack."""
    calls = []

    async def fake_run(argv):
        calls.append(argv)
        if "display-message" in argv:
            raise RuntimeError("no server running on /tmp/tmux-501/default")
        return ""

    ws = _FakeWS([json.dumps({"type": "send_keys", "keys": [{"named": "Enter"}]})])
    _run(server.keys_handler(ws, "boss-host:0", SOCK, run=fake_run))

    assert any("send-keys" in c for c in calls)
    assert ws.sent == [{"type": "ack", "ok": True, "sent": 1}]


def test_keys_mode_reports_tmux_failure_in_the_ack_and_stops_the_batch():
    calls = []

    async def fake_run(argv):
        calls.append(argv)
        if "display-message" in argv:
            return ""   # Probe laeuft, erst send-keys faellt auf die Nase
        raise RuntimeError("no server running on /tmp/tmux-501/default")

    ws = _FakeWS([json.dumps({"type": "send_keys", "keys": [
        {"literal": "hallo"}, {"named": "Enter"},
    ]})])
    _run(server.keys_handler(ws, "boss-host:0", SOCK, run=fake_run))

    assert len(calls) == 2                      # Probe + erster send-keys; Enter wird nach dem Fehler nicht mehr versucht
    assert ws.sent[0]["type"] == "ack"
    assert ws.sent[0]["ok"] is False
    assert "no server running" in ws.sent[0]["error"]


def test_keys_mode_rejects_malformed_frames_without_touching_tmux():
    calls = []

    async def fake_run(argv):
        calls.append(argv)

    ws = _FakeWS([
        "not json",
        json.dumps({"type": "resize", "cols": 80, "rows": 24}),
        json.dumps({"type": "send_keys", "keys": [{"named": "C-c"}]}),
    ])
    _run(server.keys_handler(ws, "boss-host:0", SOCK, run=fake_run))

    assert calls == []
    assert [m["ok"] for m in ws.sent] == [False, False, False]


def test_resolve_target_accepts_mode_keys():
    session, sock, mode = server.resolve_target_and_mode("mode=keys")
    assert (session, sock) == (server.DEFAULT_SESSION, server.DEFAULT_SOCKET)
    assert mode == "keys"


def test_resolve_target_defaults_to_pty_mode_and_rejects_unknown_modes():
    assert server.resolve_target_and_mode("")[2] == "pty"
    with pytest.raises(ValueError):
        server.resolve_target_and_mode("mode=shell")
