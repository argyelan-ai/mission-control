"""Chats slice S2, chat-input side (``acp_chat_chats``): a composer message
can name a further chat of a headless omp agent, and ``/new`` becomes a
further chat — only while ``CHAT_RECORD_ENABLED`` is on.

Contract:

* flag off: exactly today's behaviour — the one-argument transport, ``/new``
  is the daemon's ``new_session`` (204), no chat is created; a ``chatId``
  is refused (404 ``chat_record_disabled``) instead of being dropped;
* flag on, ``/new`` on an omp agent: still the daemon's reset until its own
  switch ``CHAT_RECORD_NEW_AS_CHAT`` is on (the chat view does not follow
  ``newChat`` yet); with it: 201 ``{"newChat": …}``, a further chat in the
  record, the daemon's session untouched, a ``chat.session_reset`` event
  with who sent it; the agent's OWN ``/new`` command still goes to the
  harness as text;
* flag on, ``chatId``: the further chat goes to the daemon by id
  (``chatId`` on the wire for every op); the agent's default chat is the
  agent's own chat; another agent's chat is 404;
* only the omp container daemon holds further chats: a TUI agent or the
  Hermes host bridge refuse a chat id.

The transport is a double: no ``docker exec``. Slugs are made up.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.chat_record import Chat
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio


class _FakeTransport:
    def __init__(self, chat_id=None):
        self.chat_id = chat_id
        self.calls: list[tuple] = []

    async def prompt(self, text, mode=None, origin=None, context=None):
        self.calls.append(("prompt", self.chat_id, text))
        return {"ok": True, "turn": 1}

    async def new_session(self, mode=None, request_id=None):
        self.calls.append(("new_session", self.chat_id))
        return {"ok": True, "previousSessionId": "sid-old"}


@pytest.fixture
def wire(monkeypatch):
    """Every transport the input path asks for, by the chat it names."""
    from app.services import agent_chat_input

    made: list[_FakeTransport] = []

    def _for(agent, chat_id=None, **kw):
        assert not kw
        t = _FakeTransport(chat_id)
        made.append(t)
        return t

    monkeypatch.setattr(agent_chat_input, "transport_for", _for)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state",
                        lambda agent: {"version": 1, "busy": False, "commands": []})
    return made


def _calls(made):
    return [c for t in made for c in t.calls]


@pytest.fixture
def flag(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


@pytest.fixture
def new_as_chat(monkeypatch, flag):
    """``/new`` opens a further chat — its own switch on top of the record."""
    monkeypatch.setattr(settings, "chat_record_new_as_chat", True, raising=False)


async def _reset_events(agent_id):
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine) as s:
        return (await s.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "chat.session_reset",
                                        ActivityEvent.agent_id == agent_id)
        )).all()


async def _omp(make_agent, slug="omp-many"):
    return await make_agent(name="Omp Many", slug=slug, agent_runtime="cli-bridge", harness="omp")


async def _chats(agent_id):
    async with AsyncSession(test_engine) as s:
        return (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent_id))).all()


def _input(agent, **body):
    return f"/api/v1/agents/{agent.id}/chat/input", body


# ── flag off: today's behaviour ───────────────────────────────────────────


async def test_flag_off_new_is_the_daemon_reset_and_creates_no_chat(auth_client, make_agent, wire, monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 204, resp.text
    assert _calls(wire) == [("new_session", None)]
    assert await _chats(agent.id) == []

    url, body = _input(agent, text="hello")
    assert (await auth_client.post(url, json=body)).status_code == 204
    assert _calls(wire)[-1] == ("prompt", None, "hello")


async def test_flag_off_a_chat_id_is_refused_not_dropped(auth_client, make_agent, wire, monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)
    agent = await _omp(make_agent)
    url, body = _input(agent, text="hello", chatId=str(uuid.uuid4()))
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 404 and resp.json() == {"reason": "chat_record_disabled"}
    assert _calls(wire) == []


# ── flag on ────────────────────────────────────────────────────────────────


async def test_record_on_new_stays_the_daemon_reset_until_its_own_switch(auth_client, make_agent, wire, flag):
    """The chat view does not follow `newChat` yet: with only the record on,
    `/new` keeps today's reset — never a chat the operator cannot reach."""
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 204, resp.text
    assert _calls(wire) == [("new_session", None)]
    assert all(c.agent_default for c in await _chats(agent.id))


async def test_new_as_a_chat_is_on_record_like_every_reset(auth_client, make_agent, wire, new_as_chat):
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 201, resp.text
    events = await _reset_events(agent.id)
    assert len(events) == 1
    detail = events[0].detail
    assert detail["outcome"] == "new_chat" and detail["command"] == "/new"
    assert detail["new_chat_id"] == resp.json()["newChat"]["id"]
    assert detail["actor"] == "operator" and detail["via"] == "chat_input"
    assert "opened a new chat" in events[0].title


async def test_new_creates_a_further_chat_and_leaves_the_session(auth_client, make_agent, wire, new_as_chat):
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 201, resp.text
    chat = resp.json()["newChat"]
    assert chat["legacyAgentId"] == str(agent.id) and chat["agentDefault"] is False
    assert chat["harness"] == "omp" and chat["nativeSessionId"] is None
    assert chat["createdAt"]  # the chat list numbers an agent's chats by it
    assert _calls(wire) == []  # the open conversation is not reset
    chats = await _chats(agent.id)
    assert sorted(c.agent_default for c in chats) == [False, True]

    # a second /new = a third chat, typed in the further chat itself
    url, body = _input(agent, text="/clear", chatId=chat["id"])
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 201
    assert len(await _chats(agent.id)) == 3


async def test_the_agents_own_new_command_still_goes_to_the_harness(auth_client, make_agent, monkeypatch, new_as_chat):
    from app.services import agent_chat_input

    fake = _FakeTransport()
    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent, **kw: fake)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: {
        "version": 1, "busy": False, "commands": [{"name": "new", "description": "own"}]})
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 204
    assert fake.calls == [("prompt", None, "/new")]
    assert await _chats(agent.id) == []


async def test_a_message_to_a_further_chat_names_it_on_the_wire(auth_client, make_agent, wire, new_as_chat):
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    chat_id = (await auth_client.post(url, json=body)).json()["newChat"]["id"]

    url, body = _input(agent, text="for the second chat", chatId=chat_id)
    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 204, resp.text
    assert _calls(wire) == [("prompt", chat_id, "for the second chat")]

    # the agent's own chat: as before, no chat id
    url, body = _input(agent, text="main chat")
    assert (await auth_client.post(url, json=body)).status_code == 204
    assert _calls(wire)[-1] == ("prompt", None, "main chat")


async def test_the_default_chat_id_is_the_agents_own_chat(auth_client, make_agent, wire, new_as_chat):
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    await auth_client.post(url, json=body)
    default = next(c for c in await _chats(agent.id) if c.agent_default)
    url, body = _input(agent, text="hi", chatId=str(default.id))
    assert (await auth_client.post(url, json=body)).status_code == 204
    assert _calls(wire) == [("prompt", None, "hi")]


async def test_another_agents_chat_is_not_found(auth_client, make_agent, wire, new_as_chat):
    first = await _omp(make_agent, slug="omp-first")
    second = await _omp(make_agent, slug="omp-second")
    url, body = _input(first, text="/new")
    chat_id = (await auth_client.post(url, json=body)).json()["newChat"]["id"]
    for target_id in (chat_id, str(uuid.uuid4())):
        url, body = _input(second, text="x", chatId=target_id)
        resp = await auth_client.post(url, json=body)
        assert resp.status_code == 404 and resp.json() == {"reason": "chat_not_found"}
    assert _calls(wire) == []


async def test_an_archived_chat_takes_no_message(auth_client, make_agent, wire, new_as_chat):
    agent = await _omp(make_agent)
    url, body = _input(agent, text="/new")
    chat_id = (await auth_client.post(url, json=body)).json()["newChat"]["id"]
    resp = await auth_client.patch(f"/api/v1/chat-record/chats/{chat_id}", json={"archived": True})
    assert resp.status_code == 200, resp.text
    url, body = _input(agent, text="x", chatId=chat_id)
    assert (await auth_client.post(url, json=body)).status_code == 404
    assert _calls(wire) == []


async def test_only_the_omp_container_daemon_holds_further_chats(monkeypatch, flag):
    from app.services import acp_chat_chats, acp_chat_transport
    from app.services.agent_chat_input import InputNotSupportedError

    omp = SimpleNamespace(slug="o", agent_runtime="cli-bridge", harness="omp", id="o")
    tui = SimpleNamespace(slug="t", agent_runtime="cli-bridge", harness="claude", id="t")
    hermes = SimpleNamespace(slug="hermes", agent_runtime="host", harness="hermes", id="h")
    monkeypatch.setattr(settings, "hermes_driver", "acp")
    assert acp_chat_chats.further_chats_supported(omp) is True
    assert acp_chat_chats.further_chats_supported(tui) is False
    assert acp_chat_chats.further_chats_supported(hermes) is False
    with pytest.raises(InputNotSupportedError):
        acp_chat_transport.transport_for(hermes, chat_id=str(uuid.uuid4()))
    with pytest.raises(InputNotSupportedError):
        acp_chat_transport.transport_for(tui, chat_id=str(uuid.uuid4()))


# ── the wire ──────────────────────────────────────────────────────────────


class _Proc:
    returncode = 0

    async def communicate(self):
        return b'{"ok": true}', b""


async def test_every_op_of_a_further_chat_carries_its_id(monkeypatch):
    from app.services import acp_chat_transport

    argvs: list[tuple] = []

    async def _exec(*argv, **_kw):
        argvs.append(argv)
        return _Proc()

    monkeypatch.setattr(acp_chat_transport.asyncio, "create_subprocess_exec", _exec)
    chat_id = str(uuid.uuid4())
    t = acp_chat_transport.DockerCtlTransport("omp-x", chat_id=chat_id)
    await t.prompt("hi", mode="queue", origin="operator")
    await t.new_session(mode="queue", request_id="r1")
    await t.queue_clear()
    await t.cancel()
    await t.config("thinking", "high")
    await t.state()
    await t.answer("el-1", "accept", {"value": "Yes"})
    sent = [(a[-3], json.loads(a[-1])) for a in argvs]
    assert [op for op, _ in sent] == ["prompt", "new_session", "queue_clear", "cancel",
                                      "config", "state", "answer"]
    assert all(payload["chatId"] == chat_id for _, payload in sent)
    assert sent[0][1] == {"text": "hi", "origin": "operator", "mode": "queue", "chatId": chat_id}

    # without a chat: byte-identical to before (no --json for bare ops)
    argvs.clear()
    plain = acp_chat_transport.DockerCtlTransport("omp-x")
    await plain.cancel()
    await plain.prompt("hi")
    assert argvs[0][-1] == "cancel"
    assert json.loads(argvs[1][-1]) == {"text": "hi"}
