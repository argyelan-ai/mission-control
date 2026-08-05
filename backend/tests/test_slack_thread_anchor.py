"""Channel-root messages become anchored conversations (thread-anchor fix).

The bug this file pins (live, 2026-08-04, the Spider-Man order): the operator
writes a channel-root message in Slack, it lands in the ONE Boss DM thread,
and every reply comes back as a NEW channel-root message — Slack never shows
a threaded answer under the operator's message, because the inbound path
throws the message's ``ts`` away and the outbound path hard-wires DM threads
to the channel root.

The fix: a channel-root message opens its own MC conversation (``kind="chat"``)
anchored to the Slack message's ``ts`` (``slack_thread_ts``). Replies then
mirror as real Slack thread replies, and the operator's follow-ups inside that
Slack thread continue the same MC conversation.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.config import settings
from app.models.agent import Agent
from app.models.thread import Message, Thread
from app.services.chat_slack import SlackChatAdapter
from app.services.slack_inbound import ingest_slack_event
from tests.chat_harnesses import _FakeSlackTransport, _StaticFaces

CHANNEL = "C-TEAM"
ROOT_TS = "1753699200.000100"


@pytest.fixture
def transport():
    return _FakeSlackTransport([])


@pytest.fixture
def adapter(monkeypatch, transport):
    monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "slack_default_channel", CHANNEL, raising=False)
    return SlackChatAdapter(transport=transport, faces=_StaticFaces())


def root_event(text="Hey Boss, bitte Film X holen", ts=ROOT_TS, **kw) -> dict:
    event = {
        "type": "message",
        "channel": CHANNEL,
        "user": "U-OPERATOR",
        "text": text,
        "ts": ts,
    }
    event.update(kw)
    return event


def thread_reply_event(thread_ts, text="und bitte auf Deutsch", ts="1753699200.000900"):
    return root_event(text=text, ts=ts, thread_ts=thread_ts)


async def _agent(session, name, slug=None, **kw) -> Agent:
    agent = Agent(name=name, slug=slug or name.lower(), **kw)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _chat_threads(session) -> list[Thread]:
    return list(
        (await session.exec(select(Thread).where(Thread.kind == "chat"))).all()
    )


# ── 1. Ingest: the root message opens an anchored conversation ────────────


@pytest.mark.asyncio
async def test_channel_root_message_opens_an_anchored_chat_thread(
    async_session, adapter
):
    boss = await _agent(async_session, "Boss")
    await ingest_slack_event(root_event(), adapter=adapter, session=async_session)

    threads = await _chat_threads(async_session)
    assert len(threads) == 1
    thread = threads[0]
    assert thread.slack_thread_ts == ROOT_TS
    assert thread.agent_id == boss.id

    messages = list(
        (
            await async_session.exec(
                select(Message).where(Message.thread_id == thread.id)
            )
        ).all()
    )
    assert len(messages) == 1
    assert "Film X" in messages[0].body
    assert messages[0].sender_type == "user"


@pytest.mark.asyncio
async def test_redelivered_root_event_reuses_the_same_conversation(
    async_session, adapter
):
    """Slack redelivers events; the anchor makes the ingest idempotent per ts."""
    await _agent(async_session, "Boss")
    await ingest_slack_event(root_event(), adapter=adapter, session=async_session)
    await ingest_slack_event(root_event(), adapter=adapter, session=async_session)

    assert len(await _chat_threads(async_session)) == 1


@pytest.mark.asyncio
async def test_a_second_root_message_opens_a_second_conversation(
    async_session, adapter
):
    """One request = one Slack thread. Two requests must not share an anchor —
    sharing is exactly the recency bug the per-conversation design avoids."""
    await _agent(async_session, "Boss")
    await ingest_slack_event(root_event(), adapter=adapter, session=async_session)
    await ingest_slack_event(
        root_event(text="Noch was: Backup pruefen", ts="1753699300.000200"),
        adapter=adapter,
        session=async_session,
    )

    threads = await _chat_threads(async_session)
    assert {t.slack_thread_ts for t in threads} == {ROOT_TS, "1753699300.000200"}


@pytest.mark.asyncio
async def test_addressed_agent_gets_the_anchored_conversation(
    async_session, adapter
):
    """`@rex mach mal` in the channel: the conversation belongs to Rex, and it
    is still anchored — Rex's answer must thread too."""
    await _agent(async_session, "Boss")
    rex = await _agent(async_session, "Rex")
    await ingest_slack_event(
        root_event(text="@rex bitte Review starten"),
        adapter=adapter,
        session=async_session,
    )

    threads = await _chat_threads(async_session)
    assert len(threads) == 1
    assert threads[0].agent_id == rex.id
    assert threads[0].slack_thread_ts == ROOT_TS


@pytest.mark.asyncio
async def test_operator_reply_in_the_slack_thread_continues_the_conversation(
    async_session, adapter
):
    boss = await _agent(async_session, "Boss")
    await ingest_slack_event(root_event(), adapter=adapter, session=async_session)
    await ingest_slack_event(
        thread_reply_event(ROOT_TS), adapter=adapter, session=async_session
    )

    threads = await _chat_threads(async_session)
    assert len(threads) == 1
    messages = list(
        (
            await async_session.exec(
                select(Message).where(Message.thread_id == threads[0].id)
            )
        ).all()
    )
    assert len(messages) == 2
    assert boss.id == threads[0].agent_id


@pytest.mark.asyncio
async def test_reply_in_a_foreign_slack_thread_anchors_a_new_conversation(
    async_session, adapter, transport
):
    """The operator answers under a message MC posted top-level (e.g. a report
    in the reports channel). 2026-07-31 this dropped a real order. Now the
    parent ts becomes the anchor of a fresh conversation with Boss — the
    answer arrives in exactly that Slack thread, no ask-back."""
    await _agent(async_session, "Boss")
    await ingest_slack_event(
        thread_reply_event("1753000000.000500", text="ladet bitte supergirl herunter"),
        adapter=adapter,
        session=async_session,
    )

    threads = await _chat_threads(async_session)
    assert len(threads) == 1
    assert threads[0].slack_thread_ts == "1753000000.000500"
    messages = list(
        (
            await async_session.exec(
                select(Message).where(Message.thread_id == threads[0].id)
            )
        ).all()
    )
    assert len(messages) == 1
    assert "supergirl" in messages[0].body


# ── 2. Outbound: replies mirror into the Slack thread ─────────────────────


@pytest.mark.asyncio
async def test_agent_reply_on_a_chat_thread_mirrors_as_slack_thread_reply(
    async_session, adapter, transport
):
    boss = await _agent(async_session, "Boss")
    await ingest_slack_event(root_event(), adapter=adapter, session=async_session)
    thread = (await _chat_threads(async_session))[0]

    from app.services.chat_outbound import mirror_message
    from app.services.messaging import post_message

    message = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="agent",
        sender_id=boss.id,
        body="Erledigt — Film liegt auf dem NAS.",
        mirror_to_telegram=False,
    )
    assert await mirror_message(async_session, message, adapter) is True

    posted = [c for c in transport.calls if c.get("thread_ts") == ROOT_TS]
    assert posted, f"kein Thread-Reply an {ROOT_TS}: {transport.calls}"


@pytest.mark.asyncio
async def test_dm_threads_still_speak_in_the_channel_root(async_session, adapter):
    """True DM threads (``/msg``, agent fallback) keep today's behaviour."""
    boss = await _agent(async_session, "Boss")
    from app.services.messaging import ensure_dm_thread

    dm = await ensure_dm_thread(async_session, boss)
    from app.services.chat_slack import GENERAL_ROOM

    assert await adapter.ensure_room(async_session, dm) == GENERAL_ROOM


# ── 3. Scope: the conversation reaches its agent (Hören = Sprechen) ───────


@pytest.mark.asyncio
async def test_chat_thread_is_part_of_its_agents_message_scope(
    async_session, adapter
):
    boss = await _agent(async_session, "Boss")
    other = await _agent(async_session, "Rex")
    await ingest_slack_event(root_event(), adapter=adapter, session=async_session)
    thread = (await _chat_threads(async_session))[0]

    from app.services.thread_scope import (
        message_threads_for_agent,
        thread_agent_may_write_to,
    )

    boss_threads = [t.id for t, _ in await message_threads_for_agent(boss, async_session)]
    assert thread.id in boss_threads
    assert await thread_agent_may_write_to(async_session, boss, thread.id) is not None
    assert await thread_agent_may_write_to(async_session, other, thread.id) is None
