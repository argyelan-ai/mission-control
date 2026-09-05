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

    ws = _FakeWS([json.dumps({"type": "send_keys", "keys": [
        {"literal": "hallo"}, {"named": "Enter"},
    ]})])
    _run(server.keys_handler(ws, "boss-host:0", SOCK, run=fake_run))

    assert [c[-1] for c in calls] == ["hallo", "Enter"]
    assert ws.sent == [{"type": "ack", "ok": True, "sent": 2}]


def test_keys_mode_reports_tmux_failure_in_the_ack_and_stops_the_batch():
    calls = []

    async def fake_run(argv):
        calls.append(argv)
        raise RuntimeError("no server running on /tmp/tmux-501/default")

    ws = _FakeWS([json.dumps({"type": "send_keys", "keys": [
        {"literal": "hallo"}, {"named": "Enter"},
    ]})])
    _run(server.keys_handler(ws, "boss-host:0", SOCK, run=fake_run))

    assert len(calls) == 1                      # Enter wird nach dem Fehler nicht mehr versucht
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
