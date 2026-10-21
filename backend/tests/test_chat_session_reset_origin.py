"""Incident 21.10.2026 — who reset the chat?

An operator's chat got a new, empty session: a ``/new`` (or ``/clear``)
reached ``POST /agents/{id}/chat/input`` and became the chat daemon's
``new_session`` job. Nobody could say who sent it — the backend's logs had
rotated and nothing durable recorded the origin.

Contract: every reset through chat input writes ONE activity event
``chat.session_reset`` (agent page feed, ``GET /activity``) naming

* the agent,
* who asked: a logged-in user (id + name), or a token acting as the first
  admin (``LOCAL_AUTH_TOKEN``, the host's MCP service) — named as the token,
  never passed off as the operator,
* the way (``via: chat_input`` + the command ``/new`` | ``/clear``),
* the session that was replaced and — as soon as the daemon has switched
  (its state mirror's ``lastReset``) — the new one.

A message that is no reset, and a reset the daemon refused, write nothing.
The transport is a double: no ``docker exec``, no socket. Slugs are made up.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

OLD = "01a0-old-session"
NEW = "01a1-new-session"
LOCAL_TOKEN = "local-reset-test-token-0123456789"


class _FakeTransport:
    """The control channel. An accepted ``new_session`` that is not queued
    switches the daemon a moment later, as its worker does."""

    def __init__(self, answers: dict | None = None):
        self.calls: list[tuple] = []
        self._answers = answers or {}
        self.on_switch = None

    async def prompt(self, text: str, mode: str | None = None) -> dict:
        self.calls.append(("prompt", text, mode))
        return self._answers.get("prompt", {"ok": True, "turn": 1})

    async def new_session(self, mode: str | None = None) -> dict:
        self.calls.append(("new_session", mode))
        answer = self._answers.get("new_session", {"ok": True, "previousSessionId": OLD})
        if answer.get("ok") and not answer.get("queued") and self.on_switch is not None:
            asyncio.get_running_loop().call_later(0.05, self.on_switch)
        return answer


@pytest.fixture
def daemon(monkeypatch):
    """The chat daemon as the backend sees it: the control channel (a
    double) and its state mirror (a dict the test moves on)."""
    import app.database
    from app.services import agent_chat_input

    transport = _FakeTransport()
    state: dict = {"version": 1, "driver": "omp", "sessionId": OLD, "busy": False,
                   "commands": [], "configOptions": [], "lastReset": None}
    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent: transport)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: dict(state))
    # The new session id is filled in by a follow-up with its own DB session.
    monkeypatch.setattr(app.database, "async_session_maker",
                        lambda: AsyncSession(test_engine, expire_on_commit=False))
    monkeypatch.setattr(agent_chat_input, "_RESET_FILL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(agent_chat_input, "_RESET_FILL_WINDOW_SECONDS", 5.0)

    class _Daemon:
        #: An agent image from before this change mirrors no ``lastReset``.
        mirrors_last_reset = True

    d = _Daemon()
    d.transport = transport
    d.state = state

    def switch():
        """What the daemon's worker does after ``new_session``: the mirror
        names the new session and the reset."""
        state["sessionId"] = NEW
        if d.mirrors_last_reset:
            state["lastReset"] = {"at": "2026-10-21T17:07:46Z", "via": "new_session",
                                  "previousSessionId": OLD, "sessionId": NEW}

    transport.on_switch = switch
    return d


async def _acp_agent(make_agent):
    return await make_agent(name="Reset Probe", slug="reset-probe",
                            agent_runtime="cli-bridge", harness="omp")


async def _reset_events(agent_id) -> list:
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "chat.session_reset")
        )).all()
    return [r for r in rows if r.agent_id == agent_id]


async def _settle_fills():
    from app.services import agent_chat_input

    pending = list(agent_chat_input._RESET_FILLS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ── 1. a reset by the logged-in operator ─────────────────────────────────────


@pytest.mark.parametrize("typed,command", [("/new", "/new"), ("  /CLEAR \n", "/clear")])
async def test_a_reset_through_chat_input_writes_who_how_and_both_sessions(
    auth_client, make_agent, daemon, typed, command
):
    agent = await _acp_agent(make_agent)

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": typed})
    assert resp.status_code == 204, resp.text
    assert daemon.transport.calls == [("new_session", "queue")]

    events = await _reset_events(agent.id)
    assert len(events) == 1
    event = events[0]
    detail = event.detail
    assert detail["via"] == "chat_input"
    assert detail["command"] == command
    assert detail["actor"] == {
        "kind": "user",
        "user_id": "00000000-0000-0000-0000-000000000099",
        "name": "Test Admin",
        "auth": "bearer_jwt",
    }
    assert detail["previous_session_id"] == OLD
    assert detail["queued"] is False
    assert event.severity == "info"
    assert command in event.title and "Test Admin" in event.title

    # The daemon switches a moment later; the event learns the new session.
    await _settle_fills()
    (event,) = await _reset_events(agent.id)
    assert event.detail["new_session_id"] == NEW
    assert event.detail["previous_session_id"] == OLD


async def test_the_reset_is_in_the_agents_activity_feed(auth_client, make_agent, daemon):
    agent = await _acp_agent(make_agent)
    await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    await _settle_fills()

    resp = await auth_client.get("/api/v1/activity", params={"agent_id": str(agent.id)})
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["event_type"] == "chat.session_reset"]
    assert len(rows) == 1
    assert rows[0]["detail"]["actor"]["name"] == "Test Admin"
    assert rows[0]["detail"]["new_session_id"] == NEW


# ── 2. a token is named as the token ─────────────────────────────────────────


async def test_a_reset_with_the_local_auth_token_names_the_token(client, make_agent, daemon,
                                                                 monkeypatch):
    """LOCAL_AUTH_TOKEN acts as the first admin. A script or agent using it
    must not show up as the operator himself."""
    import app.config
    from app.models.user import User

    admin_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=admin_id, email="first-admin@mc.local", name="First Admin",
                   role="admin", is_active=True))
        await s.commit()
    monkeypatch.setattr(app.config.settings, "local_auth_token", LOCAL_TOKEN)
    agent = await _acp_agent(make_agent)

    resp = await client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"},
                             headers={"Authorization": f"Bearer {LOCAL_TOKEN}"})
    assert resp.status_code == 204, resp.text

    (event,) = await _reset_events(agent.id)
    assert event.detail["actor"] == {
        "kind": "token",
        "name": "LOCAL_AUTH_TOKEN",
        "user_id": str(admin_id),
        "acting_as": "First Admin",
        "auth": "local_auth_token",
    }
    assert "LOCAL_AUTH_TOKEN" in event.title
    assert LOCAL_TOKEN not in str(event.detail) and LOCAL_TOKEN not in event.title


# ── 3. queued behind a running reply ─────────────────────────────────────────


async def test_a_reset_queued_behind_a_reply_is_recorded_at_once(auth_client, make_agent, daemon,
                                                               monkeypatch):
    """The daemon holds the job until the running reply ends — the event is
    written when the operator asked, marked queued; no new id yet."""
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_RESET_FILL_WINDOW_SECONDS", 0.2)
    daemon.transport._answers["new_session"] = {"ok": True, "queued": True, "queueId": "q1",
                                                "position": 1}
    agent = await _acp_agent(make_agent)

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    assert resp.status_code == 204
    await _settle_fills()

    (event,) = await _reset_events(agent.id)
    assert event.detail["queued"] is True
    assert event.detail["previous_session_id"] == OLD  # from the state mirror
    assert event.detail.get("new_session_id") is None
    # It can still be taken back: the title asks, it does not claim.
    assert "requested by Test Admin" in event.title
    assert "previous one is kept" not in event.title


async def test_a_reset_of_a_daemon_without_last_reset_still_learns_the_new_id(
    auth_client, make_agent, daemon
):
    """An agent image from before this change mirrors no ``lastReset`` — a
    changed ``sessionId`` is the switch then."""
    daemon.mirrors_last_reset = False
    agent = await _acp_agent(make_agent)
    await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    await _settle_fills()
    assert daemon.state["lastReset"] is None

    (event,) = await _reset_events(agent.id)
    assert event.detail["new_session_id"] == NEW


async def test_a_switch_that_is_not_this_reset_is_never_taken_for_it(
    auth_client, make_agent, daemon, monkeypatch
):
    """The mirror names ANOTHER reset (say the daemon restarted and could
    not resume): its session is not the one this ``/new`` opened."""
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_RESET_FILL_WINDOW_SECONDS", 0.2)

    def other_switch():
        daemon.state["sessionId"] = "01a9-other"
        daemon.state["lastReset"] = {"at": "2026-10-21T17:07:46Z", "via": "start",
                                     "reason": "load_failed", "previousSessionId": "01a8-x",
                                     "sessionId": "01a9-other"}

    daemon.transport.on_switch = other_switch
    agent = await _acp_agent(make_agent)
    await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    await _settle_fills()

    (event,) = await _reset_events(agent.id)
    assert event.detail.get("new_session_id") is None


# ── 4. nothing reset, nothing written ────────────────────────────────────────


@pytest.mark.parametrize("typed", ["hello", "/clear the build cache first", "/usage"])
async def test_a_message_that_is_no_reset_writes_no_event(auth_client, make_agent, daemon, typed):
    agent = await _acp_agent(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": typed})
    assert resp.status_code == 204
    assert [c[0] for c in daemon.transport.calls] == ["prompt"]
    assert await _reset_events(agent.id) == []


async def test_a_refused_reset_writes_no_event(auth_client, make_agent, daemon):
    daemon.transport._answers["new_session"] = {"ok": False, "error": "not_started"}
    agent = await _acp_agent(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    assert resp.status_code == 409
    assert await _reset_events(agent.id) == []


async def test_send_text_reports_the_reset_to_its_caller(daemon):
    """``send_text`` hands the reset back (the router records it); a plain
    message hands back nothing."""
    from app.services import agent_chat_input

    class _Stub:
        slug = "reset-probe"
        agent_runtime = "cli-bridge"
        harness = "omp"
        id = "reset-probe"

    reset = await agent_chat_input.send_text(_Stub(), "/clear")
    assert reset == agent_chat_input.ChatReset(command="/clear", previous_session_id=OLD,
                                               queued=False)
    assert await agent_chat_input.send_text(_Stub(), "hello") is None
