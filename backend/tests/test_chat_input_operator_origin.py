"""The operator's chat message says who wrote it (``origin: "operator"``).

The chat daemon (docker/omp-bridge/acp_chat.py) offers "Send again" on a
card (``retryText``: a reply cut off by a restart, a dead child, a model that
does not answer) ONLY for a prompt that carries ``origin: "operator"`` —
fail-closed, so a task dispatch is never run twice by one tap. Until now no
caller sent it: the backend's composer path and Hermes' HTTP twin dropped
it, so the button never appeared live (spec chat-over-acp.md, "State
20.10.2026: no caller sends origin yet").

Contract:

* ``POST /agents/{id}/chat/input`` (the composer) sends ``origin:
  "operator"`` through the control channel;
* other callers of ``send_text`` (Jarvis' voice relay) stay unknown — no
  origin, no button (their text is not what the operator typed);
* both transports put ``origin`` on the wire only when set — without it the
  request is byte-identical to before (an older daemon);
* Hermes' HTTP twin hands ``origin`` on to the session.

The transport is a double: no ``docker exec``, no socket. Slugs are made up.
"""
from __future__ import annotations

import json

import httpx
import pytest

pytestmark = pytest.mark.asyncio


class _FakeTransport:
    def __init__(self):
        self.calls: list[tuple] = []

    async def prompt(self, text: str, mode: str | None = None, origin: str | None = None) -> dict:
        self.calls.append(("prompt", text, mode, origin))
        return {"ok": True, "turn": 1}


@pytest.fixture
def transport(monkeypatch):
    from app.services import agent_chat_input

    fake = _FakeTransport()
    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent: fake)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state",
                        lambda agent: {"version": 1, "busy": False, "commands": []})
    return fake


async def test_the_composer_sends_origin_operator(auth_client, make_agent, transport):
    agent = await make_agent(name="Origin Probe", slug="origin-probe",
                             agent_runtime="cli-bridge", harness="omp")
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input",
                                  json={"text": "hello there"})
    assert resp.status_code == 204, resp.text
    assert transport.calls == [("prompt", "hello there", "queue", "operator")]


async def test_send_text_without_origin_stays_unknown(transport):
    """Jarvis' voice relay and any other caller: no origin → no button."""
    from types import SimpleNamespace

    from app.services import agent_chat_input

    agent = SimpleNamespace(slug="voice-probe", agent_runtime="cli-bridge", harness="omp",
                            id="voice-probe")
    await agent_chat_input.send_text(agent, "relayed by voice")
    assert transport.calls == [("prompt", "relayed by voice", "queue", None)]


class _FakeProc:
    def __init__(self, stdout: bytes):
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


async def test_docker_transport_puts_origin_on_the_wire_only_when_set(monkeypatch):
    from app.services import acp_chat_transport

    calls: list[tuple] = []

    async def _exec(*argv, **_kw):
        calls.append(argv)
        return _FakeProc(b'{"ok": true}')

    monkeypatch.setattr(acp_chat_transport.asyncio, "create_subprocess_exec", _exec)
    transport = acp_chat_transport.DockerCtlTransport("origin-probe")
    await transport.prompt("hi", mode="queue", origin="operator")
    await transport.prompt("hi", mode="queue")
    assert json.loads(calls[0][-1]) == {"text": "hi", "mode": "queue", "origin": "operator"}
    assert json.loads(calls[1][-1]) == {"text": "hi", "mode": "queue"}


async def test_http_transport_puts_origin_on_the_wire_only_when_set():
    from app.services import acp_chat_transport

    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = acp_chat_transport.HttpCtlTransport(
        "http://host.example:18794", transport=httpx.MockTransport(_handler)
    )
    await transport.prompt("hi", mode="queue", origin="operator")
    await transport.prompt("hi", mode="queue")
    assert json.loads(seen[0].content) == {"text": "hi", "mode": "queue", "origin": "operator"}
    assert json.loads(seen[1].content) == {"text": "hi", "mode": "queue"}
