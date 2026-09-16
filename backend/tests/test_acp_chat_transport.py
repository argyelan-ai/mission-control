"""Chat over ACP (PR B) — Transportschicht + Kopflos-Erkennung.

Drei Schichten, alle ohne echten Container und ohne echten Host:

1. ``headless_chat_kind`` / ``Agent.headless_chat`` — die Wahrheitstabelle aus
   der Spezifikation (docs/specs/chat-over-acp.md), inkl. Sabotage: der
   globale Notausstieg OMP_DRIVER_DEFAULT=native stellt den ganzen omp-Kader
   auf den TUI-Pfad zurueck.
2. ``DockerCtlTransport`` — argv-Bau, JSON-Auswertung, Exit 3 (Socket tot).
   Der Subprozess ist gemockt; hier laeuft nie ein ``docker exec``.
3. ``HttpCtlTransport`` — URL/Body gegen ein ``httpx.MockTransport``.

Slugs sind bewusst erfunden (``acp-one``/``hermes``), nicht die der echten
Flotte — dieselbe Regel wie im Rest der Testsuite.
"""
from __future__ import annotations

import json

import httpx
import pytest

# asyncio_mode = "auto" (pyproject): async-Tests brauchen keine Markierung,
# und die Haelfte hier ist bewusst synchron (reine Funktionen).


class _StubAgent:
    """Enten-typisierter Ersatz fuer die Agent-Zeile — genau die drei Felder,
    die ``headless_chat_kind`` liest."""

    def __init__(self, slug=None, agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness


@pytest.fixture
def acp_slug():
    """Neutraler Slugs — unter ADR-084 haengt der Treiber am Harness, der
    Name ist fuer die Entscheidung irrelevant."""
    return "acp-one"


# ══════════════════════════════════════════════════════════════════════════
# Wahrheitstabelle: wer chattet kopflos?
# ══════════════════════════════════════════════════════════════════════════


def test_headless_kind_docker_for_omp_agent(acp_slug):
    """ADR-084: ACP ist eine Eigenschaft des omp-Harness — jeder omp-Agent
    (auch ein frisch angelegter) chattet kopflos."""
    from app.services.acp_chat_transport import headless_chat_kind

    agent = _StubAgent(slug=acp_slug, agent_runtime="cli-bridge", harness="omp")
    assert headless_chat_kind(agent) == "acp-docker"


def test_headless_kind_none_under_global_native_rollback(acp_slug, monkeypatch):
    """Sabotage: OMP_DRIVER_DEFAULT=native ist der EINE globale Notausstieg —
    der ganze omp-Kader faellt auf den TUI-Pfad zurueck, ohne Namensliste."""
    from app import config
    from app.services.acp_chat_transport import headless_chat_kind

    monkeypatch.setattr(config.settings, "omp_driver_default", "native")
    agent = _StubAgent(slug=acp_slug, agent_runtime="cli-bridge", harness="omp")
    assert headless_chat_kind(agent) is None


def test_headless_kind_none_for_claude_harness(acp_slug):
    from app.services.acp_chat_transport import headless_chat_kind

    agent = _StubAgent(slug=acp_slug, agent_runtime="cli-bridge", harness="claude")
    assert headless_chat_kind(agent) is None


def test_headless_kind_http_for_hermes_with_acp_driver(monkeypatch):
    from app import config
    from app.services.acp_chat_transport import headless_chat_kind

    monkeypatch.setattr(config.settings, "hermes_driver", "acp")
    agent = _StubAgent(slug="hermes", agent_runtime="host", harness="hermes")
    assert headless_chat_kind(agent) == "acp-http"


def test_headless_kind_none_for_hermes_on_native_driver(monkeypatch):
    from app import config
    from app.services.acp_chat_transport import headless_chat_kind

    monkeypatch.setattr(config.settings, "hermes_driver", "native")
    agent = _StubAgent(slug="hermes", agent_runtime="host", harness="hermes")
    assert headless_chat_kind(agent) is None

def test_agent_model_serializes_headless_chat(acp_slug):
    """Das Feld muss in JEDER Serialisierung stehen (GET /agents und
    /agents/{id}) — gleiche Bauart wie ``runtime_switchable``."""
    from app.models.agent import Agent

    agent = Agent(name="Acp One", slug=acp_slug, agent_runtime="cli-bridge", harness="omp")
    dumped = agent.model_dump()
    assert dumped["headless_chat"] is True

    other = Agent(name="Tui One", slug="tui-one", agent_runtime="cli-bridge", harness="claude")
    assert other.model_dump()["headless_chat"] is False


# ══════════════════════════════════════════════════════════════════════════
# DockerCtlTransport
# ══════════════════════════════════════════════════════════════════════════


class _FakeProc:
    def __init__(self, rc: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = rc
        self._out = (stdout, stderr)

    async def communicate(self):
        return self._out

    def kill(self):  # pragma: no cover - nur im Timeout-Pfad
        pass


def _fake_exec(recorder: list, proc: _FakeProc):
    async def _run(*argv, **kwargs):
        recorder.append(list(argv))
        return proc

    return _run


async def test_docker_transport_prompt_argv_and_json(monkeypatch, acp_slug):
    from app.services import acp_chat_transport

    calls: list[list[str]] = []
    monkeypatch.setattr(
        acp_chat_transport.asyncio, "create_subprocess_exec",
        _fake_exec(calls, _FakeProc(0, b'{"ok": true, "turn": 7}\n')),
    )

    transport = acp_chat_transport.DockerCtlTransport(acp_slug)
    result = await transport.prompt("hallo")

    assert result == {"ok": True, "turn": 7}
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:2] == ["docker", "exec"]
    assert f"mc-agent-{acp_slug}" in argv
    assert argv[-3:-1] == ["prompt", "--json"]
    assert json.loads(argv[-1]) == {"text": "hallo"}


async def test_docker_transport_cancel_sends_no_payload(monkeypatch, acp_slug):
    from app.services import acp_chat_transport

    calls: list[list[str]] = []
    monkeypatch.setattr(
        acp_chat_transport.asyncio, "create_subprocess_exec",
        _fake_exec(calls, _FakeProc(0, b'{"ok": true}')),
    )

    assert await acp_chat_transport.DockerCtlTransport(acp_slug).cancel() == {"ok": True}
    assert calls[0][-1] == "cancel"


async def test_docker_transport_busy_is_a_normal_answer(monkeypatch, acp_slug):
    """Exit 2 = ``ok:false``: eine ANTWORT, kein Transportfehler."""
    from app.services import acp_chat_transport

    monkeypatch.setattr(
        acp_chat_transport.asyncio, "create_subprocess_exec",
        _fake_exec([], _FakeProc(2, b'{"ok": false, "error": "busy"}')),
    )

    result = await acp_chat_transport.DockerCtlTransport(acp_slug).prompt("x")
    assert result == {"ok": False, "error": "busy"}


async def test_docker_transport_exit_3_raises_unreachable(monkeypatch, acp_slug):
    from app.services import acp_chat_transport

    monkeypatch.setattr(
        acp_chat_transport.asyncio, "create_subprocess_exec",
        _fake_exec([], _FakeProc(3, b'{"ok": false, "error": "unreachable"}')),
    )

    with pytest.raises(acp_chat_transport.AcpChatUnreachableError):
        await acp_chat_transport.DockerCtlTransport(acp_slug).state()


async def test_docker_transport_garbage_stdout_raises_unreachable(monkeypatch, acp_slug):
    """Container weg -> ``docker exec`` selbst scheitert (rc=1, kein JSON).
    Das ist kein ``ok:false``, das ist "keine Antwort"."""
    from app.services import acp_chat_transport

    monkeypatch.setattr(
        acp_chat_transport.asyncio, "create_subprocess_exec",
        _fake_exec([], _FakeProc(1, b"", b"No such container")),
    )

    with pytest.raises(acp_chat_transport.AcpChatUnreachableError):
        await acp_chat_transport.DockerCtlTransport(acp_slug).state()


# ══════════════════════════════════════════════════════════════════════════
# HttpCtlTransport (Hermes)
# ══════════════════════════════════════════════════════════════════════════


async def test_http_transport_posts_config_to_chat_endpoint():
    from app.services import acp_chat_transport

    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "configOptions": []})

    transport = acp_chat_transport.HttpCtlTransport(
        "http://host.example:18794", transport=httpx.MockTransport(_handler)
    )
    result = await transport.config("thinking", "high")

    assert result == {"ok": True, "configOptions": []}
    assert str(seen[0].url) == "http://host.example:18794/chat/config"
    assert json.loads(seen[0].content) == {"id": "thinking", "value": "high"}


async def test_http_transport_409_is_busy_not_an_exception():
    from app.services import acp_chat_transport

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "error": "busy"})

    transport = acp_chat_transport.HttpCtlTransport(
        "http://host.example:18794", transport=httpx.MockTransport(_handler)
    )
    assert await transport.prompt("x") == {"ok": False, "error": "busy"}


async def test_http_transport_connect_error_raises_unreachable():
    from app.services import acp_chat_transport

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    transport = acp_chat_transport.HttpCtlTransport(
        "http://host.example:18794", transport=httpx.MockTransport(_handler)
    )
    with pytest.raises(acp_chat_transport.AcpChatUnreachableError):
        await transport.state()


async def test_http_transport_502_raises_unreachable():
    from app.services import acp_chat_transport

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"ok": False, "error": "unreachable"})

    transport = acp_chat_transport.HttpCtlTransport(
        "http://host.example:18794", transport=httpx.MockTransport(_handler)
    )
    with pytest.raises(acp_chat_transport.AcpChatUnreachableError):
        await transport.state()


# ══════════════════════════════════════════════════════════════════════════
# transport_for + Zustandsdatei
# ══════════════════════════════════════════════════════════════════════════


def test_transport_for_picks_channel_by_target_kind(acp_slug, monkeypatch):
    from app import config
    from app.services import acp_chat_transport

    monkeypatch.setattr(config.settings, "hermes_driver", "acp")

    docker = acp_chat_transport.transport_for(
        _StubAgent(slug=acp_slug, agent_runtime="cli-bridge", harness="omp")
    )
    assert isinstance(docker, acp_chat_transport.DockerCtlTransport)

    http = acp_chat_transport.transport_for(
        _StubAgent(slug="hermes", agent_runtime="host", harness="hermes")
    )
    assert isinstance(http, acp_chat_transport.HttpCtlTransport)


def test_transport_for_rejects_tui_agent(acp_slug):
    from app.services import acp_chat_transport
    from app.services.agent_chat_input import InputNotSupportedError

    with pytest.raises(InputNotSupportedError):
        acp_chat_transport.transport_for(
            _StubAgent(slug="tui-one", agent_runtime="cli-bridge", harness="claude")
        )


def test_read_acp_chat_state_picks_newest_under_root(tmp_path, monkeypatch, acp_slug):
    from app.services import acp_chat_transport, omp_chat

    root = tmp_path / ".mc" / "agents" / acp_slug / "omp-sessions"
    old = root / "--workspace--"
    new = root / "--workspace-two--"
    for d in (old, new):
        d.mkdir(parents=True)
    (old / "acp-chat-state.json").write_text(json.dumps({"turn": 1}))
    (new / "acp-chat-state.json").write_text(json.dumps({"turn": 9}))
    import os
    os.utime(old / "acp-chat-state.json", (1, 1))

    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    agent = _StubAgent(slug=acp_slug, agent_runtime="cli-bridge", harness="omp")
    assert acp_chat_transport.read_acp_chat_state(agent) == {"turn": 9}


def test_read_acp_chat_state_missing_is_none(tmp_path, monkeypatch, acp_slug):
    from app.services import acp_chat_transport, omp_chat

    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    agent = _StubAgent(slug=acp_slug, agent_runtime="cli-bridge", harness="omp")
    assert acp_chat_transport.read_acp_chat_state(agent) is None
