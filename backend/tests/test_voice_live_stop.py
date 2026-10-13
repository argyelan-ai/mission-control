"""Live voice in the chat — stop and approvals (voice PR 4, ADR-091 draft).

Pins:

- ``is_stop_command``: only an utterance that as a WHOLE is a stop command
  counts ("Stopp", "Stop", "Halt", "Abbrechen", "Stopp <this agent>", ...).
  "Stopp den Container" is a request to pass on, never a cancel.
- ``stop_agent`` re-checks the operator's verbatim transcript server-side —
  the realtime model calling the tool is not enough — and then runs the chat
  Stop button's own path (headless: take back the queue, ACP ``cancel``;
  TUI: ``send_keys(["Escape"])``). It says "stopped" only when the turn is
  seen to end.
- Approvals: a voice message is never typed into an open permission prompt
  or a choice menu (Enter there would pick the highlighted option); the
  voice has no tool that sends keys.

The ACP transport and the pane are doubles; agent names are made up.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import voice_live, voice_live_tools
from app.services.voice_live_tools import is_stop_command

FAKE_KEY = "sk-test-" + "LONGLIVED-0123456789abcdef"
URL = "/api/v1/voice/live/tool"


# ── the classifier ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "said",
    [
        "Stopp",
        "Stopp.",
        "stop!",
        "Stop",
        "Halt!",
        "Abbrechen",
        "Abbrechen, bitte.",
        "Bitte stopp!",
        "Stopp jetzt",
        "Stop now.",
        "Stop, stop!",
        "Stopp, sofort.",
        "Hör auf!",
        "Hoer auf",
        "Okay, stopp.",
        "Please stop.",
        "Cancel",
        "Stopp Alpha",
        "Alpha, stopp!",
        "stop alpha",
        "Stopp Alfa",  # speech-to-text mishears names
        "  STOPP  ",
    ],
)
def test_stop_phrases(said):
    assert is_stop_command(said, "Alpha") is True


@pytest.mark.parametrize(
    "said",
    [
        "",
        "   ",
        "Stopp den Container",
        "Stop the tests",
        "Stopp, und mach dann die Tests",
        "Stopp die Tests und schreib den Bericht",
        "nicht stoppen",
        "Stopp nicht",
        "Kannst du stoppen?",
        "Warum hast du gestoppt?",
        "Stoppuhr starten",
        "Bus stop",
        "Halt mal kurz die Tests an",
        "Das ist halt so",
        "Abbrechen der Migration vorbereiten",
        "Stopp Beta",  # another agent: not a cancel of THIS one
        "Hör auf zu testen",
        "don't stop",
        "ja",
        "stopp " * 20,
    ],
)
def test_not_stop_phrases(said):
    assert is_stop_command(said, "Alpha") is False


def test_multi_word_agent_name():
    assert is_stop_command("Stopp Voice Target", "Voice Target") is True
    assert is_stop_command("Stopp Voice", "Voice Target") is False


def test_short_name_needs_exact_match():
    assert is_stop_command("Stopp Rex", "Rex") is True
    assert is_stop_command("Stopp Rey", "Rex") is False


# ── the tool endpoint ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_calls(monkeypatch):
    voice_live_tools.reset_for_tests()
    # Confirmation polling without real waiting.
    monkeypatch.setattr(voice_live_tools, "STOP_CONFIRM_S", 0.05)
    monkeypatch.setattr(voice_live_tools, "STOP_POLL_S", 0.01)
    yield
    voice_live_tools.reset_for_tests()


@pytest.fixture
def voice_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", FAKE_KEY)


class _FakeTransport:
    def __init__(self, state):
        self.calls: list[tuple] = []
        self.state = state
        self.queued: list[dict] = []
        self.cancel_ends_turn = True

    async def prompt(self, text, mode=None):
        self.calls.append(("prompt", text, mode))
        return {"ok": True}

    async def cancel(self):
        self.calls.append(("cancel",))
        if self.cancel_ends_turn:
            self.state["turn"] = "idle"
        return {"ok": True}

    async def queue_clear(self):
        self.calls.append(("queue_clear",))
        dropped, self.queued = self.queued, []
        return {"ok": True, "dropped": dropped}


@pytest.fixture
def acp(monkeypatch):
    """Every agent is a headless (ACP) agent whose turn is running."""
    from app.services import acp_chat_transport, agent_chat_input

    state = {"turn": "working"}
    transport = _FakeTransport(state)
    monkeypatch.setattr(agent_chat_input, "transport_for", lambda _a: transport)
    monkeypatch.setattr(agent_chat_input, "headless_chat_kind", lambda _a: "acp-docker")
    monkeypatch.setattr(acp_chat_transport, "headless_chat_kind", lambda _a: "acp-docker")
    monkeypatch.setattr(acp_chat_transport, "read_acp_turn_status", lambda _a: state["turn"])
    return transport


@pytest.fixture
def tui(monkeypatch):
    """Every agent is a TUI (docker) agent; keys and typed text are recorded,
    the tailer state is controllable, no pane can be captured."""
    from app.services import acp_chat_transport, agent_chat_input, pane_state, transcript_chat

    rec = {"keys": [], "text": [], "state": {"status": "working", "aliveness": "active"}}

    async def _keys(agent, keys):
        rec["keys"].append(list(keys))
        rec["state"] = {"status": "idle", "aliveness": "active"}

    async def _text(agent, text):
        rec["text"].append(text)

    monkeypatch.setattr(agent_chat_input, "send_keys", _keys)
    monkeypatch.setattr(agent_chat_input, "send_text", _text)
    monkeypatch.setattr(acp_chat_transport, "headless_chat_kind", lambda _a: None)
    monkeypatch.setattr(agent_chat_input, "headless_chat_kind", lambda _a: None)
    monkeypatch.setattr(transcript_chat.tailer_manager, "last_state", lambda _id: rec["state"])

    async def _no_pane(_agent):
        return None

    monkeypatch.setattr(pane_state, "capture_pane", _no_pane)
    return rec


async def _call(make_agent, name="Alpha", slug="voice-one", **kw):
    agent = await make_agent(name=name, slug=slug, harness="omp", **kw)
    return agent, voice_live_tools.open_call(agent.id)


def _stop(agent, ref, transcript="Stopp"):
    return {
        "call_ref": ref,
        "agent_id": str(agent.id),
        "name": "stop_agent",
        "arguments": {},
        "transcript": transcript,
    }


async def test_stop_runs_the_stop_button_path_on_acp(auth_client, voice_on, make_agent, acp):
    acp.queued = [{"kind": "prompt", "text": "[per Stimme] noch Y"}]
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_stop(agent, ref, "Stopp!"))
    assert r.status_code == 200, r.text
    body = r.json()
    # Stop button order: take the queue back first, then cancel.
    assert acp.calls == [("queue_clear",), ("cancel",)]
    assert body["output"]["stopped"] is True
    assert body["output"]["confirmed"] is True
    assert "Gestoppt." in body["output"]["say"]
    assert body["stopped"] is True
    assert body["taken_back"] == ["[per Stimme] noch Y"]


async def test_stop_on_tui_sends_escape(auth_client, voice_on, make_agent, tui):
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_stop(agent, ref, "Stopp Alpha."))
    assert r.status_code == 200, r.text
    assert tui["keys"] == [["Escape"]]
    out = r.json()["output"]
    assert out["stopped"] is True and out["confirmed"] is True
    assert r.json()["taken_back"] is None  # TUI: the CLI keeps its own queue


async def test_stop_refused_when_transcript_is_not_a_pure_stop(auth_client, voice_on, make_agent, acp):
    """The model called stop_agent, but the operator said more than "Stopp":
    a request to pass on, never a cancel."""
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_stop(agent, ref, "Stopp den Container"))
    assert r.status_code == 200
    out = r.json()["output"]
    assert out["stopped"] is False
    assert out["reason"] == "not_a_stop_command"
    assert "send_to_agent" in out["say"]
    assert acp.calls == []
    assert r.json()["stopped"] is False


async def test_stop_refused_without_verbatim_words(auth_client, voice_on, make_agent, acp):
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_stop(agent, ref, ""))
    assert r.json()["output"]["reason"] == "no_transcript"
    assert acp.calls == []


async def test_stop_does_nothing_when_agent_is_idle(auth_client, voice_on, make_agent, acp):
    acp.state["turn"] = "idle"
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_stop(agent, ref))
    out = r.json()["output"]
    assert out["stopped"] is False and out["reason"] == "not_working"
    assert acp.calls == []


async def test_stop_not_confirmed_never_says_gestoppt(auth_client, voice_on, make_agent, acp):
    acp.cancel_ends_turn = False
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_stop(agent, ref))
    out = r.json()["output"]
    assert ("cancel",) in acp.calls
    assert out["stopped"] is True and out["confirmed"] is False
    assert "Do NOT say" in out["say"]


async def test_stop_for_another_agent_refused(auth_client, voice_on, make_agent, acp):
    agent_a, ref_a = await _call(make_agent)
    agent_b = await make_agent(name="Beta", slug="voice-two", harness="omp")
    r = await auth_client.post(URL, json=_stop(agent_b, ref_a))
    assert r.status_code == 403
    assert acp.calls == []


async def test_stop_rate_limited(auth_client, voice_on, make_agent, acp):
    agent, ref = await _call(make_agent)
    for _ in range(voice_live_tools.RATE_LIMITS["stop_agent"]):
        acp.state["turn"] = "working"
        assert (await auth_client.post(URL, json=_stop(agent, ref))).status_code == 200
    r = await auth_client.post(URL, json=_stop(agent, ref))
    assert r.status_code == 429


async def test_stop_delivery_failure_is_not_stopped(auth_client, voice_on, make_agent, tui, monkeypatch):
    from app.services import agent_chat_input

    async def _boom(agent, keys):
        raise agent_chat_input.BossDeliveryError("no ack")

    monkeypatch.setattr(agent_chat_input, "send_keys", _boom)
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_stop(agent, ref))
    out = r.json()["output"]
    assert out["stopped"] is False
    assert "Stop button" in out["say"]


# ── approvals ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["approve", "send_keys", "answer_prompt"])
async def test_no_tool_can_press_keys(auth_client, voice_on, make_agent, tui, name):
    agent, ref = await _call(make_agent)
    r = await auth_client.post(
        URL,
        json={"call_ref": ref, "agent_id": str(agent.id), "name": name, "arguments": {"keys": ["1"]}, "transcript": "ja"},
    )
    assert r.status_code == 422
    assert tui["keys"] == [] and tui["text"] == []


@pytest.mark.parametrize("status", ["permission_prompt", "waiting_input"])
async def test_spoken_yes_is_never_typed_into_an_open_prompt(auth_client, voice_on, make_agent, tui, status):
    """A voice message ends with Enter. Typed into an open permission prompt
    or a choice menu, Enter picks the highlighted option — that would be an
    approval by voice. Refused before anything is typed."""
    tui["state"] = {"status": status, "aliveness": "active"}
    agent, ref = await _call(make_agent)
    r = await auth_client.post(
        URL,
        json={
            "call_ref": ref,
            "agent_id": str(agent.id),
            "name": "send_to_agent",
            "arguments": {"text": "ja"},
            "transcript": "ja",
        },
    )
    out = r.json()["output"]
    assert out["delivered"] is False
    assert out["reason"] == "approval_pending"
    assert "tap" in out["say"]
    assert tui["text"] == [] and tui["keys"] == []


async def test_open_prompt_seen_on_a_fresh_pane(auth_client, voice_on, make_agent, tui, monkeypatch):
    """The tailer may lag; a docker agent's pane is read fresh."""
    from app.services import pane_state

    tui["state"] = {"status": "working", "aliveness": "active"}

    async def _pane(_agent):
        return "pane"

    monkeypatch.setattr(pane_state, "capture_pane", _pane)
    from app.services import transcript_adapters

    class _Adapter:
        @staticmethod
        def parse_pane_state(_pane, _active):
            return {"status": "permission_prompt"}

    monkeypatch.setattr(transcript_adapters, "adapter_for", lambda _a: _Adapter)
    agent, ref = await _call(make_agent)
    r = await auth_client.post(
        URL,
        json={"call_ref": ref, "agent_id": str(agent.id), "name": "send_to_agent", "arguments": {"text": "ja"}, "transcript": "ja"},
    )
    assert r.json()["output"]["reason"] == "approval_pending"
    assert tui["text"] == []


async def test_normal_request_still_passes_on_while_working(auth_client, voice_on, make_agent, tui):
    agent, ref = await _call(make_agent)
    r = await auth_client.post(
        URL,
        json={
            "call_ref": ref,
            "agent_id": str(agent.id),
            "name": "send_to_agent",
            "arguments": {"text": "Stopp den Container"},
            "transcript": "Stopp den Container",
        },
    )
    assert r.json()["output"]["delivered"] is True
    assert len(tui["text"]) == 1 and tui["keys"] == []


# ── session config: barge-in stops only the voice ────────────────────────


def test_barge_in_is_server_vad_interrupt_only():
    cfg = voice_live.build_session_config("Alpha", "de")
    td = cfg["audio"]["input"]["turn_detection"]
    # The operator talking cancels and truncates the voice's own response...
    assert td["interrupt_response"] is True
    assert td["create_response"] is True
    # ...and the only tool that touches the agent's turn is stop_agent.
    names = {t["name"] for t in cfg["tools"]}
    assert names == {"send_to_agent", "get_agent_status", "stop_agent"}
    stop = next(t for t in cfg["tools"] if t["name"] == "stop_agent")
    assert stop["parameters"]["properties"] == {}
    text = cfg["instructions"]
    assert "Talking over you never stops the agent." in text
