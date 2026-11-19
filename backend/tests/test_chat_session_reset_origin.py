"""Incident 21.10.2026 — who reset the chat?

An operator's chat got a new, empty session and nobody could say who sent
it: the backend's logs had rotated, nothing durable recorded the origin, and
the chat daemon can also be reached without MC (``acp_chat_ctl.py
new_session`` from a shell, or the agent's own tools).

Contract: every chat reset is ONE activity event ``chat.session_reset``
(agent page feed, ``GET /activity``):

1. **Chat input** (``/new`` | ``/clear``, exactly): written at once by the
   router — who sent it in the chat.* actor shape (a logged-in user, or a
   token acting as the first admin, never passed off as the operator), the
   way, who handles it (the chat daemon, the agent's own command, its
   terminal), the replaced session. A headless agent's ``new_session``
   carries a request id.
2. **The daemon's record** (``resets`` in its state mirror): the audit
   completes the request's event — done (the new session), failed, taken
   back, dropped by a restart — and writes an event for every reset nobody in
   MC asked for: ``system`` (a restart could not resume), ``outside_mc``
   (``new_session`` without a request id), ``unknown`` (a request id MC has
   no event for).

Only ids of a short, plain shape are stored — the mirror and the daemon's
answer come from the agent's own container. A message that is no reset, and
a reset the daemon refused, write nothing. The transport is a double: no
``docker exec``, no socket. Slugs are made up.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

OLD = "01a0-old-session"
S1 = "01a1-first-new"
S2 = "01a2-second-new"
LOCAL_TOKEN = "local-reset-test-token-0123456789"
ADMIN_ID = "00000000-0000-0000-0000-000000000099"


def _iso(seconds_ago: float = 0.0) -> str:
    at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeTransport:
    """The control channel: records each call with its request id."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.answers: dict = {}

    async def prompt(self, text: str, mode: str | None = None, origin: str | None = None) -> dict:
        self.calls.append(("prompt", text, mode))
        return self.answers.get("prompt", {"ok": True, "turn": 1})

    async def new_session(self, mode: str | None = None, request_id: str | None = None) -> dict:
        self.calls.append(("new_session", mode, request_id))
        return self.answers.get("new_session", {"ok": True, "previousSessionId": OLD})

    def request_ids(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "new_session"]


@pytest.fixture
def daemon(monkeypatch):
    """The chat daemon as the backend sees it: the control channel (a
    double) and its state mirror (a dict the test moves on)."""
    from app.services import agent_chat_input, chat_reset_audit

    transport = _FakeTransport()
    state: dict = {"version": 1, "driver": "omp", "sessionId": OLD, "busy": False,
                   "commands": [], "configOptions": [], "resets": []}
    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent: transport)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: dict(state))
    monkeypatch.setattr(chat_reset_audit, "read_acp_chat_state", lambda agent: dict(state))
    return SimpleNamespace(transport=transport, state=state)


def _row(outcome="done", via="new_session", *, previous=OLD, current=None, request_id=None,
         seconds_ago=0.0, row_id=None, **extra) -> dict:
    """One ``resets`` row as the daemon writes it."""
    row = {"id": row_id or uuid.uuid4().hex[:16], "at": _iso(seconds_ago), "via": via,
           "outcome": outcome, "previousSessionId": previous, "sessionId": current,
           "requestId": request_id, **extra}
    return {k: v for k, v in row.items() if v is not None}


async def _acp_agent(make_agent, slug="reset-probe"):
    return await make_agent(name="Reset Probe", slug=slug,
                            agent_runtime="cli-bridge", harness="omp")


async def _events(agent_id) -> list:
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "chat.session_reset")
            .order_by(ActivityEvent.created_at)
        )).all()
    return [r for r in rows if r.agent_id == agent_id]


async def _audit(agent, state) -> set:
    from app.services import chat_reset_audit

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await chat_reset_audit.reconcile_agent(s, agent, state)


# ── 1. chat input: who sent it, at once ──────────────────────────────────────


@pytest.mark.parametrize("typed,command", [("/new", "/new"), ("  /CLEAR \n", "/clear")])
async def test_a_reset_through_chat_input_names_who_how_and_the_replaced_session(
    auth_client, make_agent, daemon, typed, command
):
    agent = await _acp_agent(make_agent)

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": typed})
    assert resp.status_code == 204, resp.text
    (request_id,) = daemon.transport.request_ids()
    assert daemon.transport.calls == [("new_session", "queue", request_id)]
    assert len(request_id) == 32

    (event,) = await _events(agent.id)
    assert event.detail == {
        "actor": "operator", "actorName": "Test Admin", "userId": ADMIN_ID,
        "auth": "bearer_jwt",
        "via": "chat_input", "command": command, "handled_by": "chat_daemon",
        "outcome": "accepted", "queued": False,
        "request_id": request_id, "queue_id": None,
        "previous_session_id": OLD, "new_session_id": None,
    }
    assert event.severity == "info"
    assert command in event.title and "Test Admin" in event.title


async def test_the_reset_is_in_the_agents_activity_feed(auth_client, make_agent, daemon):
    agent = await _acp_agent(make_agent)
    await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})

    resp = await auth_client.get("/api/v1/activity", params={"agent_id": str(agent.id)})
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["event_type"] == "chat.session_reset"]
    assert len(rows) == 1
    assert rows[0]["detail"]["actorName"] == "Test Admin"
    assert rows[0]["detail"]["via"] == "chat_input"


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

    (event,) = await _events(agent.id)
    assert {k: event.detail[k] for k in ("actor", "actorName", "actingAs", "userId", "auth")} == {
        "actor": "token", "actorName": "LOCAL_AUTH_TOKEN", "actingAs": "First Admin",
        "userId": str(admin_id), "auth": "local_auth_token",
    }
    assert "LOCAL_AUTH_TOKEN (as First Admin)" in event.title
    assert LOCAL_TOKEN not in str(event.detail) and LOCAL_TOKEN not in event.title


async def test_a_reset_queued_behind_a_reply_is_recorded_at_once(auth_client, make_agent, daemon):
    """The daemon holds the job until the running reply ends — the event is
    written when the operator asked; the title asks, it does not claim."""
    daemon.transport.answers["new_session"] = {"ok": True, "queued": True, "queueId": "q1",
                                               "position": 1}
    agent = await _acp_agent(make_agent)

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    assert resp.status_code == 204

    (event,) = await _events(agent.id)
    assert (event.detail["outcome"], event.detail["queued"], event.detail["queue_id"]) \
        == ("queued", True, "q1")
    assert event.detail["previous_session_id"] == OLD  # from the state mirror
    assert "requested by Test Admin" in event.title and "runs when" in event.title


@pytest.mark.parametrize("typed", ["hello", "/clear the build cache first", "/usage"])
async def test_a_message_that_is_no_reset_writes_no_event(auth_client, make_agent, daemon, typed):
    agent = await _acp_agent(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": typed})
    assert resp.status_code == 204
    assert [c[0] for c in daemon.transport.calls] == ["prompt"]
    assert await _events(agent.id) == []


async def test_a_refused_reset_writes_no_event(auth_client, make_agent, daemon):
    daemon.transport.answers["new_session"] = {"ok": False, "error": "not_started"}
    agent = await _acp_agent(make_agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    assert resp.status_code == 409
    assert await _events(agent.id) == []


async def test_an_agent_with_its_own_new_command_gets_it_and_the_sender_is_on_record(
    auth_client, make_agent, daemon
):
    """The harness advertises ``/new`` itself: MC passes the text on (its
    command, not MC's) — and still records who sent it."""
    daemon.state["commands"] = [{"name": "new", "description": "harness new"}]
    agent = await _acp_agent(make_agent)

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    assert resp.status_code == 204
    assert daemon.transport.calls == [("prompt", "/new", "queue")]
    (event,) = await _events(agent.id)
    assert (event.detail["handled_by"], event.detail["outcome"]) == ("harness", "sent")
    assert event.detail["request_id"] is None
    assert "agent's own /new command" in event.title


async def test_a_clear_typed_into_a_terminal_agent_is_on_record(auth_client, make_agent,
                                                               monkeypatch):
    """Critic finding: a TUI agent's ``/clear`` reset its session, and
    nothing recorded who sent it."""
    from app.services import agent_chat_input

    typed: list = []

    async def _exec(argv):
        typed.append(argv)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _exec)
    monkeypatch.setattr(agent_chat_input, "_touch_recycler_marker", _noop)
    monkeypatch.setattr(agent_chat_input, "_wait_for_send_readiness", _noop)
    agent = await make_agent(name="Tui Probe", slug="tui-probe", agent_runtime="cli-bridge",
                             harness="claude")

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/clear"})
    assert resp.status_code == 204, resp.text
    assert typed, "the keys reached the terminal"
    (event,) = await _events(agent.id)
    assert {k: event.detail[k] for k in ("via", "command", "handled_by", "outcome", "actorName",
                                         "previous_session_id")} == {
        "via": "chat_input", "command": "/clear", "handled_by": "terminal", "outcome": "sent",
        "actorName": "Test Admin", "previous_session_id": None,
    }
    assert "agent's terminal" in event.title

    await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hello"})
    assert len(await _events(agent.id)) == 1


async def test_a_new_sent_to_the_boss_terminal_is_on_record(auth_client, make_agent, monkeypatch):
    from app.services import agent_chat_input

    sent: list = []

    async def _keys(keys):
        sent.append(keys)

    monkeypatch.setattr(agent_chat_input, "_send_boss_keys", _keys)
    agent = await make_agent(name="Boss", slug="boss", agent_runtime="host", harness="claude")

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    assert resp.status_code == 204, resp.text
    assert sent
    (event,) = await _events(agent.id)
    assert (event.detail["handled_by"], event.detail["command"]) == ("terminal", "/new")


async def test_a_session_id_of_the_wrong_shape_from_the_daemon_is_never_stored(
    auth_client, make_agent, daemon
):
    """Critic finding: the container's answer went into the event as is
    (200 000 characters)."""
    daemon.transport.answers["new_session"] = {"ok": True, "previousSessionId": "x" * 200_000}
    daemon.state["sessionId"] = "y" * 200_000
    agent = await _acp_agent(make_agent)
    await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    (event,) = await _events(agent.id)
    assert event.detail["previous_session_id"] is None
    assert len(str(event.detail)) < 2_000


async def test_other_chat_actions_share_the_actor_shape():
    """One actor shape for every chat.* event: the revert/commit/PR events
    name a token as the token too."""
    from app.routers.agent_chat import _actor

    user = SimpleNamespace(id=uuid.UUID(ADMIN_ID), name="First Admin")
    as_token = SimpleNamespace(state=SimpleNamespace(mc_auth_method="local_auth_token"))
    as_user = SimpleNamespace(state=SimpleNamespace(mc_auth_method="bearer_jwt"))
    assert _actor(user, as_token) == {"actor": "token", "actorName": "LOCAL_AUTH_TOKEN",
                                      "actingAs": "First Admin", "userId": ADMIN_ID,
                                      "auth": "local_auth_token"}
    assert _actor(user, as_user) == {"actor": "operator", "actorName": "First Admin",
                                     "userId": ADMIN_ID, "auth": "bearer_jwt"}
    # Without the request: the shape the existing events had.
    assert _actor(user)["actor"] == "operator" and _actor(user)["userId"] == ADMIN_ID


# ── 2. the daemon's record completes the request's event ─────────────────────


async def _post_reset(auth_client, agent, daemon, *, queued=False, queue_id="q1"):
    if queued:
        daemon.transport.answers["new_session"] = {"ok": True, "queued": True,
                                                   "queueId": queue_id, "position": 1}
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "/new"})
    assert resp.status_code == 204
    return daemon.transport.request_ids()[-1]


async def test_the_daemons_record_adds_the_new_session(auth_client, make_agent, daemon):
    agent = await _acp_agent(make_agent)
    rid = await _post_reset(auth_client, agent, daemon)
    daemon.state["resets"] = [_row(current=S1, request_id=rid, kept=True)]

    await _audit(agent, daemon.state)

    (event,) = await _events(agent.id)
    assert (event.detail["outcome"], event.detail["previous_session_id"],
            event.detail["new_session_id"]) == ("done", OLD, S1)
    assert event.detail["reset_id"] == daemon.state["resets"][0]["id"]
    assert event.detail["actorName"] == "Test Admin"
    assert event.title == "Chat reset /new by Test Admin — new conversation, the previous one is kept"


async def test_two_queued_resets_each_name_the_session_they_replaced(auth_client, make_agent,
                                                                    daemon):
    """Critic finding: two ``/new`` behind a running reply both read
    OLD → S1. The daemon's record names each job's own sessions."""
    agent = await _acp_agent(make_agent)
    first = await _post_reset(auth_client, agent, daemon, queued=True, queue_id="q1")
    second = await _post_reset(auth_client, agent, daemon, queued=True, queue_id="q2")
    daemon.state["resets"] = [
        _row(previous=OLD, current=S1, request_id=first),
        _row(previous=S1, current=S2, request_id=second),
    ]

    await _audit(agent, daemon.state)

    pairs = {e.detail["request_id"]: (e.detail["previous_session_id"], e.detail["new_session_id"],
                                      e.detail["outcome"]) for e in await _events(agent.id)}
    assert pairs == {first: (OLD, S1, "done"), second: (S1, S2, "done")}


@pytest.mark.parametrize("outcome,words", [
    ("withdrawn", "taken back before it ran"),
    ("lost", "dropped by a chat restart before it ran"),
    ("failed", "failed — the current conversation goes on"),
])
async def test_a_reset_that_never_ran_says_so(auth_client, make_agent, daemon, outcome, words):
    """Critic finding: a queued reset taken back or dropped kept saying
    "runs when the current reply ends" for ever."""
    agent = await _acp_agent(make_agent)
    rid = await _post_reset(auth_client, agent, daemon, queued=True)
    daemon.state["resets"] = [_row(outcome, request_id=rid)]

    await _audit(agent, daemon.state)

    (event,) = await _events(agent.id)
    assert event.detail["outcome"] == outcome
    assert event.detail["new_session_id"] is None
    assert words in event.title and "runs when" not in event.title


# ── 3. resets nobody in MC asked for ─────────────────────────────────────────


async def test_a_reset_from_outside_mc_is_on_record_as_such(make_agent, daemon):
    """Critic finding: ``acp_chat_ctl.py new_session`` from a shell, or the
    agent's own tools — the card was written, the feed showed nothing."""
    agent = await _acp_agent(make_agent)
    daemon.state["resets"] = [_row(current=S1, kept=True)]

    await _audit(agent, daemon.state)

    (event,) = await _events(agent.id)
    assert (event.detail["actor"], event.detail["via"], event.detail["outcome"]) \
        == ("outside_mc", "control_socket", "done")
    assert (event.detail["previous_session_id"], event.detail["new_session_id"]) == (OLD, S1)
    assert event.severity == "warning"
    assert event.title == ("Chat reset through the chat daemon's control socket — "
                           "not sent by MC")


@pytest.mark.parametrize("via,reason,way,words", [
    ("start", "load_failed", "daemon_start", "the chat restarted"),
    ("start", "model_missing", "daemon_start", "its model is not set up"),
    ("child_restart", "model_missing", "child_restart", "the agent process restarted"),
])
async def test_a_restart_that_could_not_resume_is_on_record(make_agent, daemon, via, reason,
                                                            way, words):
    """Critic finding: daemon restart, recreate and a dead omp child never
    reached the activity feed."""
    agent = await _acp_agent(make_agent)
    daemon.state["resets"] = [_row(via=via, current=S1, reason=reason, kept=True)]

    await _audit(agent, daemon.state)

    (event,) = await _events(agent.id)
    assert (event.detail["actor"], event.detail["via"], event.detail["reason"]) \
        == ("system", way, reason)
    assert event.detail["new_session_id"] == S1
    assert words in event.title
    assert event.severity == "warning"


async def test_a_request_id_mc_has_no_event_for_is_unknown_after_a_grace(make_agent, daemon):
    agent = await _acp_agent(make_agent)
    daemon.state["resets"] = [_row(current=S1, request_id="feedbeef", seconds_ago=5)]
    assert await _audit(agent, daemon.state) == set(), "the router's write may still come"
    assert await _events(agent.id) == []

    daemon.state["resets"] = [_row(current=S1, request_id="feedbeef", seconds_ago=600)]
    await _audit(agent, daemon.state)
    (event,) = await _events(agent.id)
    assert event.detail["actor"] == "unknown"
    assert event.title == ("Chat reset through the chat daemon's control socket by an "
                           "unknown sender — its request id matches no open request of MC")


async def test_a_request_id_used_twice_is_no_proof_of_mc(auth_client, make_agent, daemon):
    """The request id sits in the mirror for anyone in the container to
    read. A second session change under the same id is not the operator's."""
    agent = await _acp_agent(make_agent)
    rid = await _post_reset(auth_client, agent, daemon)
    daemon.state["resets"] = [_row(current=S1, request_id=rid),
                              _row(previous=S1, current=S2, request_id=rid)]

    await _audit(agent, daemon.state)

    events = await _events(agent.id)
    assert [(e.detail["actor"], e.detail["new_session_id"]) for e in events] \
        == [("operator", S1), ("unknown", S2)]


async def test_a_reset_that_changed_nothing_writes_no_event(make_agent, daemon):
    agent = await _acp_agent(make_agent)
    daemon.state["resets"] = [_row("withdrawn"), _row("lost"), _row("failed")]
    settled = await _audit(agent, daemon.state)
    assert len(settled) == 3
    assert await _events(agent.id) == []


async def test_the_record_is_applied_once(auth_client, make_agent, daemon):
    agent = await _acp_agent(make_agent)
    rid = await _post_reset(auth_client, agent, daemon)
    daemon.state["resets"] = [_row(current=S1, request_id=rid),
                              _row(via="start", current=S2, reason="load_failed")]
    await _audit(agent, daemon.state)
    first = [(e.id, e.title) for e in await _events(agent.id)]
    await _audit(agent, daemon.state)
    assert [(e.id, e.title) for e in await _events(agent.id)] == first
    assert len(first) == 2


async def test_rows_of_another_shape_are_dropped_not_stored(make_agent, daemon):
    """Critic finding: container-controlled ids went into the event without
    any limit. A row with one bad field is ignored as a whole."""
    agent = await _acp_agent(make_agent)
    daemon.state["resets"] = [
        _row(current="x" * 200_000),
        _row(current=S1, request_id="has spaces"),
        _row(via="evil", current=S1),
        _row(outcome="maybe", current=S1),
        "not a row",
        {"via": "new_session", "outcome": "done"},  # no id
    ]
    assert await _audit(agent, daemon.state) == set()
    assert await _events(agent.id) == []


async def test_the_sweep_reads_only_headless_agents(make_agent, daemon, monkeypatch):
    from app.services import chat_reset_audit

    acp = await _acp_agent(make_agent)
    tui = await make_agent(name="Tui", slug="tui-only", agent_runtime="cli-bridge",
                           harness="claude")
    read: list = []

    def _read(agent):
        read.append(agent.slug)
        return dict(daemon.state)

    monkeypatch.setattr(chat_reset_audit, "read_acp_chat_state", _read)
    daemon.state["resets"] = [_row(via="start", current=S1, reason="load_failed")]

    audit = chat_reset_audit.ChatResetAudit()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await audit.sweep_once(s) == 1
        assert await audit.sweep_once(s) == 0  # nothing new: no second event
    assert acp.slug in read and tui.slug not in read
    assert len(await _events(acp.id)) == 1
