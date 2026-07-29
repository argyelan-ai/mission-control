"""Slack team-chat adapter — the parts the TCK does not (and should not) know.

The TCK (`test_chat_adapter_tck.py`) asserts the channel-neutral laws for every
adapter. This file asserts what is Slack's own:

  * an agent posts under ITS OWN name and face — never a text prefix,
  * where that face comes from, and that the fallback chain never runs dry,
  * `icon_emoji` carries a colon-code (Slack does not honour raw unicode),
  * a Slack thread per MC thread, the general chat in the channel itself,
  * `not_in_channel` says what to do about it, in words.

No network, ever: the transport is a fake and the token is a literal
"xoxb-TEST-…".
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from app.config import settings
from app.models.agent import Agent
from app.models.task import Task
from app.models.thread import Message, Thread
from app.services import slack_client
from app.services.chat_adapter import ChatSender, OutboundChatMessage
from app.services.chat_slack import (
    DEFAULT_FACE,
    GENERAL_ROOM,
    SlackChatAdapter,
    SlackFaces,
    agent_face,
)
from tests.chat_harnesses import _FakeSlackTransport, _StaticFaces

DAY = datetime(2026, 7, 27, 14, 0, 0)


@pytest.fixture
def transport():
    return _FakeSlackTransport([])


@pytest.fixture
def faces(async_session):
    """The REAL resolver, pointed at the test session instead of the engine."""

    @asynccontextmanager
    async def factory():
        yield async_session

    return SlackFaces(session_factory=factory)


@pytest.fixture
def adapter(monkeypatch, transport, faces):
    monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "slack_default_channel", "C-TEAM", raising=False)
    monkeypatch.setattr(settings, "chat_channels", "", raising=False)
    monkeypatch.setattr(
        slack_client, "_token_cache", (float("inf"), "xoxb-TEST-token"), raising=False
    )
    return SlackChatAdapter(transport=transport, faces=faces)


async def _thread(session, **kw) -> Thread:
    t = Thread(kind=kw.pop("kind", "task"), **kw)
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


async def _agent(session, name="Rex", **kw) -> Agent:
    a = Agent(name=name, **kw)
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return a


def _msg(thread, **kw) -> Message:
    return Message(
        thread_id=thread.id,
        seq=kw.pop("seq", 1),
        sender_type=kw.pop("sender_type", "agent"),
        sender_id=kw.pop("sender_id", None),
        message_type=kw.pop("message_type", "message"),
        body=kw.pop("body", "fertig"),
        mentions=kw.pop("mentions", []),
        question_meta=kw.pop("question_meta", None),
    )


# ── 1. Agents speak as themselves ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_agent_posts_under_its_own_name_and_face(
    async_session, adapter, transport
):
    """The reason for the whole channel: Boss, Rex and FreeCode look like three
    colleagues, not like one bot writing names into the text."""
    thread = await _thread(async_session)
    thread.slack_thread_ts = "1753699200.000100"
    async_session.add(thread)
    await async_session.commit()

    boss = await _agent(async_session, "Boss", role="orchestrator", emoji="👑")
    rex = await _agent(async_session, "Rex", role="reviewer", emoji="🦖")
    free = await _agent(async_session, "FreeCode", role="developer")

    for seq, who in enumerate((boss, rex, free), start=1):
        await adapter.mirror_message(
            async_session, _msg(thread, seq=seq, sender_id=who.id), now=DAY
        )

    posts = transport.calls
    assert [p["username"] for p in posts] == ["Boss", "Rex", "FreeCode"]
    assert [p["icon_emoji"] for p in posts] == [":crown:", ":t-rex:", ":zap:"]
    # And the very thing Telegram had to do is absent here.
    assert all(not p["text"].startswith(p["username"] + ":") for p in posts)
    assert {p["text"] for p in posts} == {"fertig"}


@pytest.mark.asyncio
async def test_two_agents_never_share_a_face_by_accident(async_session, adapter, transport):
    """Distinguishable is the point — same role, no emoji, still two faces."""
    thread = await _thread(async_session)
    thread.slack_thread_ts = "1753699200.000101"
    async_session.add(thread)
    await async_session.commit()
    a = await _agent(async_session, "Cody", slug="cody")
    b = await _agent(async_session, "Sparky", slug="sparky")

    await adapter.mirror_message(async_session, _msg(thread, seq=1, sender_id=a.id), now=DAY)
    await adapter.mirror_message(async_session, _msg(thread, seq=2, sender_id=b.id), now=DAY)

    icons = [p["icon_emoji"] for p in transport.calls]
    assert icons[0] != icons[1]


@pytest.mark.asyncio
async def test_the_app_speaks_without_borrowing_an_identity(async_session, adapter, transport):
    """`sender is None` = MC itself asks something. No username, no face."""
    await adapter.send("1753699200.000102", OutboundChatMessage(body="Welche Aufgabe?"))

    post = transport.calls[-1]
    assert post["username"] is None and post["icon_emoji"] is None
    assert post["text"] == "Welche Aufgabe?"


@pytest.mark.asyncio
async def test_system_messages_are_attributed_to_system(async_session, adapter, transport):
    thread = await _thread(async_session)
    thread.slack_thread_ts = "1753699200.000103"
    async_session.add(thread)
    await async_session.commit()

    await adapter.mirror_message(
        async_session, _msg(thread, sender_type="system", body="Watchdog"), now=DAY
    )

    assert transport.calls[-1]["username"] == "System"
    assert transport.calls[-1]["icon_emoji"] == ":gear:"


# ── 2. Where the face comes from ──────────────────────────────────────────


def test_face_prefers_the_agents_own_emoji():
    assert agent_face(emoji="🔍", role="developer", key="x") == ":mag:"


def test_face_accepts_a_colon_code_as_a_manual_override():
    """`agents.emoji` may hold ":my-custom-emoji:" — that is the per-agent
    override, and it needs no migration because the column already exists."""
    assert agent_face(emoji=":mc-boss:", role="developer", key="x") == ":mc-boss:"


def test_face_ignores_the_variation_selector():
    """"🛡️" (with U+FE0F) and "🛡" must resolve to the same Slack name."""
    assert agent_face(emoji="🛡️", role=None, key="x") == ":shield:"
    assert agent_face(emoji="🛡", role=None, key="x") == ":shield:"


@pytest.mark.parametrize(
    "role,expected",
    [
        ("reviewer", ":mag:"),
        ("developer", ":zap:"),
        ("orchestrator", ":dart:"),
        ("lead", ":crown:"),
        ("planner", ":clipboard:"),
        ("deployer", ":rocket:"),
    ],
)
def test_face_falls_back_to_the_role(role, expected):
    assert agent_face(emoji=None, role=role, key="whatever") == expected


def test_the_three_faces_the_operator_approved_stay_put():
    """Checked live against the real workspace on 2026-07-29 and confirmed by
    the operator in his Slack client. Pinned so a later edit to the role table
    cannot quietly change what he already signed off on."""
    assert agent_face(emoji=None, role="orchestrator", key="boss") == ":dart:"
    assert agent_face(emoji=None, role="developer", key="freecode") == ":zap:"
    assert agent_face(emoji=None, role="reviewer", key="rex") == ":mag:"


def test_face_falls_back_to_a_stable_hash_for_an_unknown_role():
    first = agent_face(emoji=None, role="astronaut", key="newbie")
    assert first.startswith(":") and first.endswith(":")
    # Deterministic: the same agent keeps its face across restarts.
    assert agent_face(emoji=None, role="astronaut", key="newbie") == first
    assert agent_face(emoji=None, role=None, key="someone-else") != first


def test_face_never_comes_back_empty():
    """The last line of defence: no input combination may leave an agent
    faceless (an unnamed, faceless post is exactly what Slack must not show)."""
    for emoji, role, key in [
        (None, None, ""), ("", "", ""), ("🦄", None, ""), ("not-an-emoji", "nope", ""),
    ]:
        face = agent_face(emoji=emoji, role=role, key=key)
        assert face == DEFAULT_FACE


def test_every_face_is_a_colon_code_not_raw_unicode():
    """Slack's `icon_emoji` documents `:chart_with_upwards_trend:` — a colon
    code. Raw unicode is not honoured, so no resolution path may emit one."""
    import re

    from app.services import chat_slack

    candidates = [
        agent_face(emoji=e, role=r, key=k)
        for e in [None, "🤖", "🦖", ":custom:", "🦄"]
        for r in [None, "reviewer", "astronaut"]
        for k in ["", "abc", "zzz"]
    ]
    candidates += [f":{n}:" for n in chat_slack._UNICODE_TO_NAME.values()]
    candidates += [f":{n}:" for n in chat_slack._ROLE_TO_NAME.values()]
    candidates += [f":{n}:" for n in chat_slack._FACE_PALETTE]
    for face in candidates:
        assert re.fullmatch(r":[a-z0-9][a-z0-9_+\-']*:", face), face


@pytest.mark.asyncio
async def test_face_resolver_survives_a_deleted_agent(async_session, faces):
    """A sender id with no row must still get a name and a face."""
    face = await faces.face_for(
        ChatSender(kind="agent", display_name="Ghost", agent_id=uuid.uuid4())
    )
    assert face.startswith(":")


@pytest.mark.asyncio
async def test_face_resolver_degrades_when_the_database_is_gone(async_session):
    @asynccontextmanager
    async def broken():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    resolver = SlackFaces(session_factory=broken)

    assert await resolver.face_for(
        ChatSender(kind="agent", display_name="Rex", agent_id=uuid.uuid4())
    ) == DEFAULT_FACE


@pytest.mark.asyncio
async def test_face_resolver_caches_per_agent(async_session, faces):
    agent = await _agent(async_session, "Rex", role="reviewer")
    sender = ChatSender(kind="agent", display_name="Rex", agent_id=agent.id)

    assert await faces.face_for(sender) == ":mag:"
    # Change the row: the cached answer must win (that is what the cache is for).
    agent.role = "developer"
    async_session.add(agent)
    await async_session.commit()
    assert await faces.face_for(sender) == ":mag:"
    faces.forget()
    assert await faces.face_for(sender) == ":zap:"


# ── 3. Rooms: one Slack thread per MC thread ──────────────────────────────


@pytest.mark.asyncio
async def test_a_task_thread_opens_its_own_slack_thread(async_session, adapter, transport):
    task = Task(title="Slack anbinden", status="inbox", board_id=uuid.uuid4())
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    thread = await _thread(async_session, task_id=task.id)

    room = await adapter.ensure_room(async_session, thread)

    # The parent message goes into the CHANNEL (no thread_ts) and its ts is
    # the room; everything after it replies inside that thread.
    assert transport.calls[0]["thread_ts"] is None
    assert room and thread.slack_thread_ts == room
    headline = transport.calls[0]["text"]
    assert str(task.id)[:8] in headline and "Slack anbinden" in headline

    await adapter.send(room, OutboundChatMessage(body="läuft"))
    assert transport.calls[-1]["thread_ts"] == room

    # Idempotent, and it does not open a second thread.
    before = len(transport.calls)
    assert await adapter.ensure_room(async_session, thread) == room
    assert len(transport.calls) == before


@pytest.mark.asyncio
async def test_the_general_chat_speaks_in_the_channel_not_in_a_thread(
    async_session, adapter, transport
):
    thread = await _thread(async_session, kind="dm", agent_id=uuid.uuid4())

    room = await adapter.ensure_room(async_session, thread)

    assert room == GENERAL_ROOM
    assert transport.calls == [], "the general chat must not open a thread"

    await adapter.send(room, OutboundChatMessage(body="Moin"))
    assert transport.calls[-1]["thread_ts"] is None


@pytest.mark.asyncio
async def test_without_a_default_channel_nothing_is_sent(async_session, adapter, monkeypatch, transport):
    monkeypatch.setattr(settings, "slack_default_channel", "", raising=False)
    thread = await _thread(async_session)

    assert await adapter.ensure_room(async_session, thread) is None
    assert await adapter.send("1.1", OutboundChatMessage(body="hi")) is False
    assert transport.calls == []
    assert adapter.is_configured() is False


@pytest.mark.asyncio
async def test_a_loud_message_is_broadcast_out_of_its_thread(async_session, adapter, transport):
    """Slack has no `disable_notification`. The equivalent of "make this one
    loud" is lifting the threaded reply back into the channel."""
    room = "1753699200.000200"
    await adapter.send(room, OutboundChatMessage(body="@Mark?", silent=False))
    await adapter.send(room, OutboundChatMessage(body="läuft", silent=True))

    assert transport.calls[0]["silent"] is False
    assert transport.calls[1]["silent"] is True


# ── 4. The mistake the operator will actually make ────────────────────────


@pytest.mark.asyncio
async def test_not_in_channel_says_how_to_fix_it(async_session, adapter, transport, caplog):
    """After setup, the single most likely failure is a bot nobody invited."""
    import logging

    transport.broken = True  # answers `not_in_channel`, Slack's own shape

    with caplog.at_level(logging.WARNING):
        assert await adapter.send("1.1", OutboundChatMessage(body="hi")) is False

    assert "not_in_channel" in caplog.text


def test_the_not_in_channel_hint_names_the_invite_command():
    hint = slack_client.explain_slack_error("not_in_channel")
    assert "/invite" in hint


def test_an_unknown_slack_error_is_passed_through_not_swallowed():
    assert "wildly_unexpected" in slack_client.explain_slack_error("wildly_unexpected")


# ── 5. Switch + configuration ─────────────────────────────────────────────


def test_slack_is_off_until_the_operator_turns_it_on(monkeypatch):
    monkeypatch.setattr(settings, "slack_team_chat_enabled", False, raising=False)

    assert SlackChatAdapter(transport=object(), faces=_StaticFaces()).is_enabled() is False


def test_enabled_and_configured_are_two_different_questions(monkeypatch):
    """ADR-072: the room lifecycle runs on the switch alone, sending also needs
    credentials. Collapsing them would change one of the two behaviours."""
    monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "slack_default_channel", "", raising=False)
    a = SlackChatAdapter(transport=object(), faces=_StaticFaces())

    assert a.is_enabled() is True
    assert a.is_configured() is False


def test_a_missing_bot_token_makes_the_channel_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "slack_default_channel", "C-TEAM", raising=False)
    monkeypatch.setattr(slack_client, "_token_cache", (float("inf"), None), raising=False)

    assert SlackChatAdapter(transport=object(), faces=_StaticFaces()).is_configured() is False


def test_slack_declares_that_it_can_carry_identity():
    from app.services import chat_adapter as reg

    slack = reg.get_chat_adapter("slack")

    assert slack is not None
    assert slack.capabilities.sender_identity is True, (
        "the whole point of the channel — without this the pipeline would let "
        "it degrade identity into a text prefix"
    )


@pytest.mark.asyncio
async def test_telegram_and_slack_can_run_side_by_side(async_session, monkeypatch):
    """Adding Slack must not silence Telegram — the operator keeps both while
    he moves over."""
    from app.services import chat_adapter as reg

    monkeypatch.setattr(settings, "chat_channels", "telegram,slack", raising=False)
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "t", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)
    monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "slack_default_channel", "C-TEAM", raising=False)
    monkeypatch.setattr(
        slack_client, "_token_cache", (float("inf"), "xoxb-TEST-token"), raising=False
    )

    assert {a.key for a in reg.sendable_chat_adapters()} == {"telegram", "slack"}


# ── 6. The token cache (no network, no session) ───────────────────────────


@pytest.mark.asyncio
async def test_token_cache_reads_once_and_then_answers_from_memory(async_session, monkeypatch):
    slack_client.invalidate_bot_token_cache()
    calls: list[str] = []

    async def fake_lookup(session, key):
        calls.append(key)
        return "xoxb-TEST-token"

    monkeypatch.setattr(slack_client, "get_secret_plaintext_by_key", fake_lookup)

    assert await slack_client.get_bot_token(async_session) == "xoxb-TEST-token"
    assert await slack_client.get_bot_token(async_session) == "xoxb-TEST-token"
    assert calls == ["slack_bot_token"], "the second message must not hit the DB"

    slack_client.invalidate_bot_token_cache()
    assert await slack_client.get_bot_token(async_session) == "xoxb-TEST-token"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_never_looked_up_token_does_not_block_the_first_send(monkeypatch):
    """Unknown counts as present — otherwise the channel would report itself
    unconfigured forever and never make the lookup that settles it."""
    slack_client.invalidate_bot_token_cache()

    assert slack_client.bot_token_looks_present() is True

    monkeypatch.setattr(slack_client, "_token_cache", (float("inf"), None), raising=False)
    assert slack_client.bot_token_looks_present() is False


@pytest.mark.asyncio
async def test_a_database_failure_degrades_to_no_token(monkeypatch):
    slack_client.invalidate_bot_token_cache()

    async def boom(session, key):
        raise RuntimeError("db down")

    monkeypatch.setattr(slack_client, "get_secret_plaintext_by_key", boom)

    assert await slack_client.get_bot_token(object()) is None


@pytest.fixture(autouse=True)
def _clean_token_cache():
    yield
    slack_client.invalidate_bot_token_cache()
