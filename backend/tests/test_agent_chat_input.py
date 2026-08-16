"""Task A5 — chat input (text + keys) delivery + router.

Two layers:

1. ``services/agent_chat_input.py`` unit tests: docker-exec argv construction
   for cli-bridge agents (single-line literal vs. multi-line bracketed
   paste), key allowlist validation, Boss WS byte delivery, and the 409-
   triggering ``InputNotSupportedError`` for every other host agent.
2. Router tests: ``POST /agents/{id}/chat/input|keys`` — 204 on success, 422
   on bad input, 404 for an unknown agent, 409 for unsupported runtimes —
   with the service functions monkeypatched so router tests never shell out
   to docker or open a real WebSocket.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class _StubAgent:
    """Duck-typed stand-in for the DB-backed Agent row — mirrors the
    ``agent.slug`` / ``agent.agent_runtime`` contract ``_target_kind`` reads,
    same convention as ``transcript_chat.resolve_transcript_dir``'s tests."""

    def __init__(self, slug: str, agent_runtime: str):
        self.slug = slug
        self.agent_runtime = agent_runtime


class _FakeWSConn:
    def __init__(self, sent: list[bytes], events: list[tuple] | None = None):
        self._sent = sent
        self._events = events

    async def send(self, payload: bytes) -> None:
        self._sent.append(payload)
        if self._events is not None:
            self._events.append(("send", payload))

    async def __aenter__(self) -> "_FakeWSConn":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeWSClient:
    """``events`` interleaves ``("send", payload)`` entries with whatever a
    test also appends to it (e.g. a monkeypatched ``asyncio.sleep`` recording
    ``("sleep", delay)``) — needed to assert frame/delay ordering, not just
    the final set of bytes sent."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.connected_urls: list[str] = []
        self.events: list[tuple] = []

    def connect(self, url: str, **kwargs):
        self.connected_urls.append(url)
        return _FakeWSConn(self.sent, self.events)


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — docker (cli-bridge) delivery
# ══════════════════════════════════════════════════════════════════════════


async def test_send_text_docker_single_line_one_call(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "hello agent")

    assert len(calls) == 1
    argv = calls[0]
    assert argv[:2] == ["docker", "exec"]
    assert "-e" in argv and "LANG=C.UTF-8" in argv
    assert "-u" in argv and "agent" in argv
    assert "mc-agent-rex" in argv
    assert argv[-3:] == ["-l", "--", "hello agent"]


async def test_send_text_docker_multiline_two_calls_bracketed_paste(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "line one\nline two")

    assert len(calls) == 2
    first, second = calls
    assert first[-3] == "-l"
    assert first[-2] == "--"
    assert first[-1] == "\x1b[200~line one\nline two\x1b[201~"
    assert second[-1] == "Enter"
    assert "-l" not in second


async def test_send_text_docker_dash_prefixed_single_line_gets_double_dash(monkeypatch):
    """Reproduces the review finding: text starting with '-' would otherwise
    be parsed by tmux as a flag (even after -l) and silently dropped."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "-h")

    assert calls[0][-3:] == ["-l", "--", "-h"]


async def test_send_text_docker_dash_bullet_single_line_gets_double_dash(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "- bullet")

    assert calls[0][-3:] == ["-l", "--", "- bullet"]


async def test_send_keys_docker_named_key_no_literal_flag(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_keys(agent, ["Escape"])

    assert len(calls) == 1
    assert calls[0][-1] == "Escape"
    assert "-l" not in calls[0]


async def test_send_keys_docker_digit_key_uses_literal_flag(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_keys(agent, ["1", "y"])

    assert len(calls) == 2
    assert calls[0][-3:] == ["-l", "--", "1"]
    assert calls[1][-3:] == ["-l", "--", "y"]


async def test_send_keys_rejects_non_allowlisted_key(monkeypatch):
    from app.services import agent_chat_input

    called = False

    async def _fake_run(argv):
        nonlocal called
        called = True

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(ValueError):
        await agent_chat_input.send_keys(agent, ["Escape", "F5"])

    assert not called  # nothing delivered once one key fails validation


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — Boss (host) delivery
# ══════════════════════════════════════════════════════════════════════════


async def test_send_text_boss_sends_bytes_over_ws(monkeypatch):
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.send_text(agent, "deploy the thing")

    assert fake_client.connected_urls == ["ws://host.docker.internal:7682/"]
    assert fake_client.sent == [b"deploy the thing", b"\r"]


async def test_send_text_boss_sends_text_then_enter_as_separate_frames_with_delay(monkeypatch):
    """Fix round 2 — reproduced live: text + '\\r' sent as one frame (or two
    frames with no gap) makes the Claude TUI treat the Enter as part of a
    paste and never submit. Text must land, THEN a delay, THEN Enter as its
    own frame."""
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    async def _fake_sleep(delay):
        fake_client.events.append(("sleep", delay))

    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.send_text(agent, "deploy the thing")

    assert fake_client.events == [
        ("send", b"deploy the thing"),
        ("sleep", 0.15),
        ("send", b"\r"),
    ]


async def test_send_text_boss_host_alias_slug(monkeypatch):
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    agent = _StubAgent(slug="boss-host", agent_runtime="host")
    await agent_chat_input.send_text(agent, "hi")

    assert fake_client.sent == [b"hi", b"\r"]


async def test_send_keys_boss_sends_mapped_bytes(monkeypatch):
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.send_keys(agent, ["Up", "Enter"])

    assert fake_client.sent == [b"\x1b[A", b"\r"]


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — unsupported runtimes
# ══════════════════════════════════════════════════════════════════════════


async def test_send_text_other_host_agent_raises_input_not_supported():
    from app.services.agent_chat_input import InputNotSupportedError, send_text

    agent = _StubAgent(slug="hermes", agent_runtime="host")
    with pytest.raises(InputNotSupportedError):
        await send_text(agent, "hi")


async def test_send_keys_other_host_agent_raises_input_not_supported():
    from app.services.agent_chat_input import InputNotSupportedError, send_keys

    agent = _StubAgent(slug="hermes", agent_runtime="host")
    with pytest.raises(InputNotSupportedError):
        await send_keys(agent, ["Enter"])


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/input
# ══════════════════════════════════════════════════════════════════════════


async def test_post_chat_input_204_and_forwards_text(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    calls = []

    async def _fake_send_text(a, text):
        calls.append((a.id, text))

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_text", _fake_send_text)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hello"}
    )

    assert resp.status_code == 204, resp.text
    assert calls == [(agent.id, "hello")]


async def test_post_chat_input_422_empty_text(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "   "}
    )

    assert resp.status_code == 422


async def test_post_chat_input_422_too_long(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "x" * 20001}
    )

    assert resp.status_code == 422


async def test_post_chat_input_422_nul_byte(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hello\x00world"}
    )

    assert resp.status_code == 422


async def test_post_chat_input_422_other_control_char(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hello\x1bworld"}
    )

    assert resp.status_code == 422


async def test_post_chat_input_allows_newline_and_tab(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    async def _fake_send_text(a, text):
        pass

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_text", _fake_send_text)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "line one\n\tline two"}
    )

    assert resp.status_code == 204, resp.text


async def test_post_chat_input_404_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.post(
        f"/api/v1/agents/{uuid.uuid4()}/chat/input", json={"text": "hi"}
    )
    assert resp.status_code == 404


async def test_post_chat_input_409_unsupported_runtime(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hi"}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "input_not_supported"}


async def test_post_chat_input_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hi"}
    )

    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/keys
# ══════════════════════════════════════════════════════════════════════════


async def test_post_chat_keys_204_and_forwards_keys(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    calls = []

    async def _fake_send_keys(a, keys):
        calls.append((a.id, keys))

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_keys", _fake_send_keys)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Escape", "Enter"]}
    )

    assert resp.status_code == 204, resp.text
    assert calls == [(agent.id, ["Escape", "Enter"])]


async def test_post_chat_keys_422_non_allowlisted(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["F5"]}
    )

    assert resp.status_code == 422


async def test_post_chat_keys_422_too_many_keys(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"] * 17}
    )

    assert resp.status_code == 422


async def test_post_chat_keys_allows_max_keys(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    async def _fake_send_keys(a, keys):
        pass

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_keys", _fake_send_keys)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"] * 16}
    )

    assert resp.status_code == 204, resp.text


async def test_post_chat_keys_404_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.post(
        f"/api/v1/agents/{uuid.uuid4()}/chat/keys", json={"keys": ["Enter"]}
    )
    assert resp.status_code == 404


async def test_post_chat_keys_409_unsupported_runtime(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"]}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "input_not_supported"}


async def test_post_chat_keys_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"]}
    )

    assert resp.status_code == 401
