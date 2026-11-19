"""Sending into a further chat is routed by the chat's harness
(``acp_chat_chats.resolve_chat``, follow-up of the further-chats view).

Contract of ``POST /agents/{id}/chat/input`` with a ``chatId``:

* flag ``CHAT_RECORD_ENABLED`` off: today's behaviour — 404
  ``chat_record_disabled``, nothing is sent;
* a further **Claude** chat of a Claude container agent reaches the Claude
  chat runner (``claude_chat_runner.open_chat`` with that chat id) and the
  text is typed into that chat's own window — before this, ``resolve_chat``
  admitted omp chats only and answered 404 ``chat_not_found``;
* a further **omp** chat of an omp container agent still goes to the chat
  daemon by id (unchanged);
* an unknown chat id, another agent's chat: 404 ``chat_not_found``;
* a chat of this agent whose harness this agent cannot run as a further chat:
  409 ``chat_harness_unsupported`` with the harness — a clear refusal, never
  routed to the wrong process.

No ``docker exec``: the runner and the tmux typing are doubles. Slugs are made up.
"""
from __future__ import annotations

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.chat_record import Chat
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio


@pytest.fixture
def flag(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


@pytest.fixture
def runner(monkeypatch):
    """The Claude chat runner and the tmux typing behind it, as doubles."""
    from app.services import agent_chat_input

    seen: dict = {"opened": [], "typed": []}

    async def _open_chat(agent, chat_id, **kw):
        seen["opened"].append((agent.slug, chat_id))
        return f"chats-{agent.slug}:chat-{chat_id}"

    async def _ready(agent, chat_id=None):
        return None

    async def _exec(argv, *a, **kw):
        seen["typed"].append(list(argv))

    monkeypatch.setattr(agent_chat_input.claude_chat_runner, "open_chat", _open_chat)
    monkeypatch.setattr(agent_chat_input, "_wait_for_send_readiness", _ready)
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _exec)
    return seen


class _FakeTransport:
    def __init__(self, chat_id=None):
        self.chat_id = chat_id
        self.calls: list[tuple] = []

    async def prompt(self, text, mode=None, origin=None, context=None):
        self.calls.append(("prompt", self.chat_id, text))
        return {"ok": True, "turn": 1}


@pytest.fixture
def wire(monkeypatch):
    """The omp chat daemon's transport, by the chat it names."""
    from app.services import agent_chat_input

    made: list[_FakeTransport] = []

    def _for(agent, chat_id=None, **kw):
        t = _FakeTransport(chat_id)
        made.append(t)
        return t

    monkeypatch.setattr(agent_chat_input, "transport_for", _for)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state",
                        lambda agent: {"version": 1, "busy": False, "commands": []})
    return made


async def _claude(make_agent, slug="claude-routing"):
    return await make_agent(name="Claude Routing", slug=slug,
                            agent_runtime="cli-bridge", harness="claude")


async def _omp(make_agent, slug="omp-routing"):
    return await make_agent(name="Omp Routing", slug=slug,
                            agent_runtime="cli-bridge", harness="omp")


async def _further_chat(client, agent, *, harness: str | None = None) -> str:
    """A further chat of ``agent`` through the record's own route (what the
    chat list's "new chat" calls); ``harness`` overrides the stored harness."""
    resp = await client.post("/api/v1/chat-record/chats", json={"agentId": str(agent.id)})
    assert resp.status_code == 201, resp.text
    chat_id = resp.json()["id"]
    if harness is not None:
        async with AsyncSession(test_engine) as s:
            chat = await s.get(Chat, uuid.UUID(chat_id))
            chat.harness = harness
            s.add(chat)
            await s.commit()
    return chat_id


async def _send(client, agent, text, chat_id):
    return await client.post(f"/api/v1/agents/{agent.id}/chat/input",
                             json={"text": text, "chatId": chat_id})


# ── Claude: the reported bug ───────────────────────────────────────────────


async def test_a_further_claude_chat_reaches_the_runner(auth_client, make_agent, runner, flag):
    agent = await _claude(make_agent)
    chat_id = await _further_chat(auth_client, agent)

    resp = await _send(auth_client, agent, "hello second chat", chat_id)

    assert resp.status_code == 204, resp.text
    assert runner["opened"] == [(agent.slug, chat_id)]
    # typed into the chat's own window, never window 0
    target = f"chats-{agent.slug}:chat-{chat_id}"
    assert runner["typed"], "nothing was typed"
    for argv in runner["typed"]:
        assert target in argv, argv
    assert any("hello second chat" in argv for argv in runner["typed"])


async def test_the_claude_agents_default_chat_is_its_own_window(auth_client, make_agent, runner, flag):
    agent = await _claude(make_agent)
    await _further_chat(auth_client, agent)  # also records the agent's default chat
    async with AsyncSession(test_engine) as s:
        from sqlmodel import select

        default = (await s.exec(select(Chat).where(
            Chat.legacy_agent_id == agent.id, Chat.agent_default == True  # noqa: E712
        ))).one()
    resp = await _send(auth_client, agent, "main", str(default.id))
    assert resp.status_code == 204, resp.text
    assert runner["opened"] == []


# ── omp: unchanged ─────────────────────────────────────────────────────────


async def test_a_further_omp_chat_still_goes_to_the_daemon(auth_client, make_agent, wire, runner, flag):
    agent = await _omp(make_agent)
    chat_id = await _further_chat(auth_client, agent)

    resp = await _send(auth_client, agent, "for omp", chat_id)

    assert resp.status_code == 204, resp.text
    assert [c for t in wire for c in t.calls] == [("prompt", chat_id, "for omp")]
    assert runner["opened"] == []


# ── refusals ───────────────────────────────────────────────────────────────


async def test_an_unknown_chat_id_is_not_found(auth_client, make_agent, runner, wire, flag):
    claude = await _claude(make_agent)
    omp = await _omp(make_agent)
    for agent in (claude, omp):
        resp = await _send(auth_client, agent, "x", str(uuid.uuid4()))
        assert resp.status_code == 404 and resp.json() == {"reason": "chat_not_found"}
    assert runner["opened"] == [] and runner["typed"] == []
    assert [c for t in wire for c in t.calls] == []


async def test_another_agents_claude_chat_is_not_found(auth_client, make_agent, runner, flag):
    first = await _claude(make_agent, slug="claude-first")
    second = await _claude(make_agent, slug="claude-second")
    chat_id = await _further_chat(auth_client, first)
    resp = await _send(auth_client, second, "x", chat_id)
    assert resp.status_code == 404 and resp.json() == {"reason": "chat_not_found"}
    assert runner["opened"] == []


async def test_a_chat_on_a_harness_the_agent_cannot_run_is_a_clear_refusal(
    auth_client, make_agent, runner, wire, flag,
):
    # a Claude chat on an omp agent (e.g. left over from a harness change)
    omp = await _omp(make_agent)
    chat_id = await _further_chat(auth_client, omp, harness="claude")
    resp = await _send(auth_client, omp, "x", chat_id)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"reason": "chat_harness_unsupported", "harness": "claude"}

    # an omp chat on a Claude agent
    claude = await _claude(make_agent)
    chat_id = await _further_chat(auth_client, claude, harness="omp")
    resp = await _send(auth_client, claude, "x", chat_id)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"reason": "chat_harness_unsupported", "harness": "omp"}

    assert runner["opened"] == [] and runner["typed"] == []
    assert [c for t in wire for c in t.calls] == []


# ── flag off: today's behaviour ────────────────────────────────────────────


async def test_flag_off_keeps_todays_refusal(auth_client, make_agent, runner, flag, monkeypatch):
    agent = await _claude(make_agent)
    chat_id = await _further_chat(auth_client, agent)
    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)

    resp = await _send(auth_client, agent, "x", chat_id)

    assert resp.status_code == 404 and resp.json() == {"reason": "chat_record_disabled"}
    assert runner["opened"] == [] and runner["typed"] == []

    # without a chat id: the agent's own window, as before
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "plain"})
    assert resp.status_code == 204, resp.text
    assert runner["opened"] == []
    assert runner["typed"] and all(
        not any(str(a).startswith("chats-") for a in argv) for argv in runner["typed"]
    )
