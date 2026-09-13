"""Chat over ACP (PR B) — der Sendepfad eines kopflosen Agenten.

Alles, was der Composer anbietet, laeuft hier ueber den Steuerkanal statt
ueber tmux: Prompt, Stop, Denk-Stufe, Modell. Und alles, was er ANZEIGT
(Effort-Chip, Slash-Palette, Modell-Auswahl), kommt aus der Zustandsdatei des
Chat-Daemons — nicht aus einer Pane-Sonde, die es bei einem kopflosen Agenten
gar nicht gibt.

Der Transport ist durchweg ein Doppel (``_FakeTransport``): kein
``docker exec``, kein HTTP. Slug ``acp-one`` ist erfunden.
"""
from __future__ import annotations



import pytest


class _StubAgent:
    def __init__(self, slug="acp-one", agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness
        self.id = slug


class _FakeTransport:
    """Zeichnet jede Operation auf und antwortet mit vorgegebenen Antworten."""

    def __init__(self, answers: dict | None = None):
        self.calls: list[tuple] = []
        self._answers = answers or {}

    def _answer(self, op: str) -> dict:
        return self._answers.get(op, {"ok": True})

    async def prompt(self, text: str) -> dict:
        self.calls.append(("prompt", text))
        return self._answer("prompt")

    async def cancel(self) -> dict:
        self.calls.append(("cancel",))
        return self._answer("cancel")

    async def config(self, id: str, value: str) -> dict:
        self.calls.append(("config", id, value))
        return self._answer("config")

    async def state(self) -> dict:
        self.calls.append(("state",))
        return self._answer("state")


@pytest.fixture
def acp_agent(monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "omp_acp_agent_slugs", "acp-one")
    return _StubAgent()


@pytest.fixture
def fake_transport(monkeypatch):
    from app.services import agent_chat_input

    transport = _FakeTransport()

    def _for(agent):
        return transport

    monkeypatch.setattr(agent_chat_input, "transport_for", _for)
    return transport


def _install(monkeypatch, transport):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent: transport)
    return transport


# ══════════════════════════════════════════════════════════════════════════
# Kanalwahl
# ══════════════════════════════════════════════════════════════════════════


def test_target_kind_is_acp_docker_for_listed_agent(acp_agent):
    from app.services.agent_chat_input import _target_kind

    assert _target_kind(acp_agent) == "acp-docker"


def test_target_kind_is_acp_http_for_hermes(monkeypatch):
    from app import config
    from app.services.agent_chat_input import _target_kind

    monkeypatch.setattr(config.settings, "hermes_driver", "acp")
    agent = _StubAgent(slug="hermes", agent_runtime="host", harness="hermes")
    assert _target_kind(agent) == "acp-http"


def test_target_kind_unchanged_for_tui_agent(acp_agent):
    """Ein nicht gelisteter omp-Agent bleibt auf ``docker`` (tmux)."""
    from app.services.agent_chat_input import _target_kind

    assert _target_kind(_StubAgent(slug="tui-one")) == "docker"


# ══════════════════════════════════════════════════════════════════════════
# send_text / send_keys
# ══════════════════════════════════════════════════════════════════════════


async def test_send_text_goes_through_the_transport(acp_agent, fake_transport, monkeypatch):
    """Kein einziges ``docker exec``: waere der TUI-Pfad noch aktiv, liefe der
    Text in ein Fenster, das dieser Agent gar nicht mehr bedient."""
    from app.services import agent_chat_input

    async def _boom(argv):  # pragma: no cover - darf nie laufen
        raise AssertionError(f"TUI-Pfad benutzt: {argv}")

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _boom)

    await agent_chat_input.send_text(acp_agent, "hallo Agent")

    assert fake_transport.calls == [("prompt", "hallo Agent")]


async def test_send_text_records_last_sent_for_the_preview(acp_agent, fake_transport):
    """Die Live-Vorschau haengt am zuletzt Getippten — der ACP-Pfad darf den
    Anker nicht verlieren, sonst zeigt sie bis zum Zugende alte Historie."""
    from app.services import agent_chat_input

    await agent_chat_input.send_text(acp_agent, "anker")
    assert agent_chat_input.pop_last_sent(str(acp_agent.id)) == "anker"


async def test_send_text_busy_raises_agent_busy(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    transport = _install(
        monkeypatch, _FakeTransport({"prompt": {"ok": False, "error": "busy"}})
    )

    with pytest.raises(agent_chat_input.AgentBusyError):
        await agent_chat_input.send_text(acp_agent, "zweiter Zug")
    assert transport.calls == [("prompt", "zweiter Zug")]


async def test_send_keys_escape_cancels(acp_agent, fake_transport):
    from app.services import agent_chat_input

    await agent_chat_input.send_keys(acp_agent, ["Escape"])
    assert fake_transport.calls == [("cancel",)]


async def test_send_keys_other_key_not_supported(acp_agent, fake_transport):
    """Es gibt keine TUI, in die ein ``Enter`` oder eine ``2`` gehen koennte —
    ehrliche 409 statt eines Tastendrucks ins Nichts."""
    from app.services import agent_chat_input

    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.send_keys(acp_agent, ["Enter"])
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.send_keys(acp_agent, ["Escape", "Escape"])
    assert fake_transport.calls == []


# ══════════════════════════════════════════════════════════════════════════
# set_effort / set_model
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def acp_state(monkeypatch):
    """Zustandsdatei des Daemons, wie der Chat-Daemon sie schreibt."""
    from app.services import agent_chat_input

    state = {
        "version": 1,
        "driver": "omp",
        "busy": False,
        "configOptions": [
            {
                "id": "thinking",
                "category": "thought_level",
                "currentValue": "medium",
                "options": [
                    {"value": "off", "name": "Off"},
                    {"value": "medium", "name": "Medium"},
                    {"value": "high", "name": "High"},
                ],
            },
            {
                "id": "model",
                "category": "model",
                "currentValue": "model-a",
                "options": [
                    {"value": "model-a", "name": "Model A"},
                    {"value": "model-b", "name": "Model B"},
                ],
            },
        ],
        "commands": [
            {"name": "usage", "description": "Show usage", "input": None},
            {"name": "model", "description": None, "input": None},
        ],
    }
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: state)
    return state


async def test_set_effort_sends_config_thinking(acp_agent, fake_transport, acp_state):
    from app.services import agent_chat_input

    await agent_chat_input.set_effort(acp_agent, "high")
    assert fake_transport.calls == [("config", "thinking", "high")]


async def test_set_effort_rejected_by_the_daemon(acp_agent, acp_state, monkeypatch):
    from app.services import agent_chat_input

    _install(
        monkeypatch,
        _FakeTransport({"config": {"ok": False, "error": "rpc_error", "detail": "nope"}}),
    )

    with pytest.raises(agent_chat_input.EffortSwitchRejectedError):
        await agent_chat_input.set_effort(acp_agent, "high")


async def test_set_effort_unknown_level_is_a_bad_request(acp_agent, fake_transport, acp_state):
    """Die Leiter kommt aus dem Zustand des Agenten, nicht aus einer Tabelle."""
    from app.services import agent_chat_input

    with pytest.raises(ValueError):
        await agent_chat_input.set_effort(acp_agent, "ultracode")
    assert fake_transport.calls == []


async def test_set_effort_without_state_is_not_supported(acp_agent, fake_transport, monkeypatch):
    """Ohne Zustandsdatei zeigt der Chip nichts an — dann darf auch ein
    direkter POST nichts schalten."""
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: None)
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_effort(acp_agent, "high")
    assert fake_transport.calls == []


async def test_set_model_sends_config_model(acp_agent, fake_transport, acp_state):
    from app.services import agent_chat_input

    await agent_chat_input.set_model(acp_agent, "model-b")
    assert fake_transport.calls == [("config", "model", "model-b")]


async def test_set_model_not_supported_on_tui_agent(acp_agent):
    from app.services import agent_chat_input

    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_model(_StubAgent(slug="tui-one"), "model-b")


# ══════════════════════════════════════════════════════════════════════════
# Capabilities aus der Zustandsdatei
# ══════════════════════════════════════════════════════════════════════════


async def test_effort_capabilities_from_state(acp_agent, acp_state):
    from app.services import agent_chat_input

    caps = await agent_chat_input.effort_capabilities(acp_agent)
    assert caps["effortLevels"] == ["off", "medium", "high"]
    assert caps["canSwitchEffort"] is True
    assert caps["effort"] == "medium"
    assert caps["effortReason"] is None


async def test_effort_capabilities_without_state(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: None)
    caps = await agent_chat_input.effort_capabilities(acp_agent)
    assert caps["effortLevels"] == []
    assert caps["canSwitchEffort"] is False
    assert caps["effortReason"] == "acp_state_missing"


async def test_slash_commands_from_state(acp_agent, acp_state):
    from app.services import agent_chat_input

    caps = await agent_chat_input.slash_command_capabilities(acp_agent)
    assert caps["slashCommands"] == [
        {"name": "usage", "description": "Show usage"},
        {"name": "model", "description": None},
    ]


async def test_slash_commands_without_state(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: None)
    caps = await agent_chat_input.slash_command_capabilities(acp_agent)
    assert caps["slashCommands"] == []


async def test_model_options_from_state(acp_agent, acp_state):
    """``command`` ist der nackte Wert: der Composer schickt ihn als
    ``/model <command>`` — ein vorangestelltes "/model " ergaebe
    ``/model /model model-b``."""
    from app.services import agent_chat_input

    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    assert [(o["command"], o["label"]) for o in caps["modelOptions"]] == [
        ("model-a", "Model A"),
        ("model-b", "Model B"),
    ]
    assert caps["model"] == "model-a"


async def test_model_options_without_state(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: None)
    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    assert caps["modelOptions"] == []
    assert caps["model"] is None


# ══════════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════════


async def test_router_maps_busy_to_409(auth_client, make_agent, monkeypatch):
    """Ein zweiter Prompt waehrend eines laufenden Zugs ist kein Serverfehler,
    sondern eine Absage, die der Composer anzeigen kann."""
    import app.routers.agent_chat as agent_chat_mod
    from app.services.agent_chat_input import AgentBusyError

    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge")

    async def _busy(a, text):
        raise AgentBusyError()

    monkeypatch.setattr(agent_chat_mod, "send_text", _busy)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "zweiter Zug"}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["reason"] == "agent_busy"


async def test_router_maps_unreachable_to_502(auth_client, make_agent, monkeypatch):
    """Daemon tot = "da ist gerade niemand" — 502 mit eigener Begruendung,
    nicht dieselbe 409 wie eine inhaltliche Absage."""
    import app.routers.agent_chat as agent_chat_mod
    from app.services.acp_chat_transport import AcpChatUnreachableError

    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge")

    async def _dead(a, text):
        raise AcpChatUnreachableError("socket weg")

    monkeypatch.setattr(agent_chat_mod, "send_text", _dead)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hallo"}
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["reason"] == "acp_unreachable"


# ══════════════════════════════════════════════════════════════════════════
# Adapter-Registrierung (Hermes liest das omp-Transkriptformat)
# ══════════════════════════════════════════════════════════════════════════


def test_hermes_uses_the_omp_transcript_adapter(tmp_path, monkeypatch):
    """Ohne diese Registrierung waere der Hermes-Zweig in
    ``omp_chat.resolve_transcript_dir`` toter Code: der History-Endpunkt
    fragt IMMER ueber ``adapter_for``, und ein unbekannter Harness landet
    beim Claude-Adapter, der fuer einen Host-Agenten nichts findet."""
    from app.services import omp_chat
    from app.services.transcript_adapters import adapter_for

    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    agent = _StubAgent(slug="hermes", agent_runtime="host", harness="hermes")

    adapter = adapter_for(agent)
    assert adapter.resolve_transcript_dir(agent) == (
        tmp_path / ".mc/agents/hermes/omp-sessions"
    )


async def test_agents_endpoints_expose_headless_chat(auth_client, make_agent, monkeypatch):
    """Das Frontend versteckt den Chat/Terminal-Umschalter anhand dieses
    Feldes — es muss in BEIDEN Antworten stehen, Liste wie Detail."""
    from app import config

    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge", harness="omp")
    monkeypatch.setattr(config.settings, "omp_acp_agent_slugs", agent.slug)

    detail = await auth_client.get(f"/api/v1/agents/{agent.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["headless_chat"] is True

    listing = await auth_client.get("/api/v1/agents")
    rows = {row["id"]: row for row in listing.json()}
    assert rows[str(agent.id)]["headless_chat"] is True
