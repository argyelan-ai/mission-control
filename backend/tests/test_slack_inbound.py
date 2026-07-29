"""Inbound: a Slack message finds exactly one MC conversation.

Three things decide whether this channel is usable at all, and each gets its
own section below:

  1. **Loop protection.** MC posts into the channel it reads. If its own
     message came back in, MC would answer itself, forever.
  2. **Who is meant.** Boss for the channel, the responsible agent inside a
     task thread, `@name` when the operator says so — and *nobody else*. A
     "hallo" that reaches ten agents produces ten answers.
  3. **Threads.** A reply inside a Slack thread belongs to that conversation;
     an unknown thread is asked about, never guessed.

No network: the adapter's transport is the fake from `chat_harnesses`, and the
channel gate is answered from `SLACK_DEFAULT_CHANNEL` without a Slack call.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.config import settings
from app.models.agent import Agent
from app.models.thread import Message, Thread
from app.services.chat_slack import SlackChatAdapter
from app.services.slack_inbound import (
    ingest_slack_event,
    is_own_message,
    normalise_slack_text,
    room_for,
)
from tests.chat_harnesses import _FakeSlackTransport, _StaticFaces

CHANNEL = "C-TEAM"


@pytest.fixture
def transport():
    return _FakeSlackTransport([])


@pytest.fixture
def adapter(monkeypatch, transport):
    monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "slack_default_channel", CHANNEL, raising=False)
    return SlackChatAdapter(transport=transport, faces=_StaticFaces())


def message_event(text="hallo", **kw) -> dict:
    event = {
        "type": "message",
        "channel": CHANNEL,
        "user": "U-MARK",
        "text": text,
        "ts": "1753699200.000100",
    }
    event.update(kw)
    return event


async def _agent(session, name, slug=None, **kw) -> Agent:
    agent = Agent(name=name, slug=slug or name.lower(), **kw)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _messages(session, thread_id) -> list[Message]:
    return list(
        (await session.exec(select(Message).where(Message.thread_id == thread_id))).all()
    )


async def _all_messages(session) -> list[Message]:
    return list((await session.exec(select(Message))).all())


async def _ingest(session, adapter, event) -> None:
    await ingest_slack_event(event, adapter=adapter, session=session)


# ── 1. Loop protection ────────────────────────────────────────────────────


def test_a_message_posted_by_mc_is_recognised_as_its_own():
    """This is exactly the shape MC's own posts come back in: chat.postMessage
    with username + icon_emoji (verified live 2026-07-29)."""
    assert is_own_message(
        {
            "type": "message",
            "subtype": "bot_message",
            "bot_id": "B0123",
            "username": "Rex",
            "text": "fertig",
            "bot_profile": {"name": "Mission Control"},
        }
    ) is True


@pytest.mark.parametrize(
    "marker",
    [
        {"bot_id": "B0123"},
        {"subtype": "bot_message"},
        {"bot_profile": {"name": "x"}},
        {"app_id": "A0123"},
    ],
)
def test_any_single_bot_marker_is_enough(marker):
    """Which markers Slack sets depends on how the message was posted, so no
    single field may be the whole check. Each event here carries a plausible
    `user` too, so ONLY the marker under test can be what trips the filter —
    otherwise the missing-author rule below would make all four pass for free.
    """
    event = {"type": "message", "text": "x", "user": "U-BOT"}
    event.update(marker)
    assert is_own_message(event) is True


def test_a_message_with_no_author_at_all_is_not_from_a_human():
    assert is_own_message({"type": "message", "text": "x"}) is True


def test_a_human_message_is_not_mistaken_for_a_bot():
    assert is_own_message(message_event()) is False


@pytest.mark.asyncio
async def test_mcs_own_message_never_becomes_an_inbound_message(
    async_session, adapter, transport
):
    """The regression this whole section exists for: without the filter MC
    stores its own post, wakes an agent, which posts again — a loop.

    The event deliberately carries NO subtype and a plausible `user`, so the
    bot marker is the only thing that can stop it. With the subtype present as
    well, the later "any subtype is not a plain message" rule would catch it
    and this test would still pass with the loop protection deleted (verified
    by sabotage: it did).
    """
    await _agent(async_session, "Boss", "boss")

    await _ingest(
        async_session,
        adapter,
        message_event(text="Ich bin fertig", bot_id="B1"),
    )

    assert await _all_messages(async_session) == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_an_inbound_message_is_stored_with_the_mirror_suppressed(
    async_session, adapter, transport
):
    """Belt and braces: even if the filter above ever let one through, the
    write itself must not be mirrored back into the channel."""
    await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event(text="wie weit seid ihr?"))

    stored = await _all_messages(async_session)
    assert len(stored) == 1
    assert stored[0].sender_type == "user"
    assert transport.calls == [], "the inbound message was echoed back to Slack"


# ── 2. Who is meant ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_channel_belongs_to_boss(async_session, adapter):
    boss = await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event(text="hallo"))

    thread = (
        await async_session.exec(
            select(Thread).where(Thread.kind == "dm", Thread.agent_id == boss.id)
        )
    ).first()
    assert thread is not None
    assert [m.body for m in await _messages(async_session, thread.id)] == ["hallo"]


@pytest.mark.asyncio
async def test_nobody_else_hears_a_general_message(async_session, adapter):
    """The rule that keeps ten agents from answering one 'hallo'."""
    await _agent(async_session, "Boss", "boss")
    await _agent(async_session, "Rex", "rex")
    await _agent(async_session, "FreeCode", "freecode")

    await _ingest(async_session, adapter, message_event(text="hallo"))

    threads_with_messages = {m.thread_id for m in await _all_messages(async_session)}
    assert len(threads_with_messages) == 1


@pytest.mark.asyncio
async def test_an_at_name_addresses_that_agent_and_only_that_agent(
    async_session, adapter
):
    await _agent(async_session, "Boss", "boss")
    rex = await _agent(async_session, "Rex", "rex")

    await _ingest(async_session, adapter, message_event(text="@rex schau dir das an"))

    rex_thread = (
        await async_session.exec(
            select(Thread).where(Thread.kind == "dm", Thread.agent_id == rex.id)
        )
    ).first()
    assert rex_thread is not None
    stored = await _all_messages(async_session)
    assert len(stored) == 1
    assert stored[0].thread_id == rex_thread.id


@pytest.mark.parametrize(
    "text",
    [
        "@rex bitte pruefen",
        "@Rex bitte pruefen",
        "@REX bitte pruefen",
        "rex bitte pruefen",
        "Rex: bitte pruefen",
        "Rex, bitte pruefen",
        "kannst du das ansehen @rex",
    ],
)
@pytest.mark.asyncio
async def test_addressing_is_tolerant_about_how_it_is_typed(
    async_session, adapter, text
):
    """There is no autocomplete for these names — the operator types them by
    hand, so every plausible spelling must land."""
    await _agent(async_session, "Boss", "boss")
    rex = await _agent(async_session, "Rex", "rex")

    await _ingest(async_session, adapter, message_event(text=text))

    stored = await _all_messages(async_session)
    thread = (await async_session.exec(select(Thread).where(Thread.id == stored[0].thread_id))).one()
    assert thread.agent_id == rex.id, f"{text!r} did not reach Rex"


@pytest.mark.parametrize("handle", ["@free-code", "@Free_Code", "@freecode", "@FREECODE"])
@pytest.mark.asyncio
async def test_hyphen_and_underscore_are_the_same_name(async_session, adapter, handle):
    await _agent(async_session, "Boss", "boss")
    free = await _agent(async_session, "FreeCode", "freecode")

    await _ingest(async_session, adapter, message_event(text=f"{handle} bau das"))

    stored = await _all_messages(async_session)
    thread = (await async_session.exec(select(Thread).where(Thread.id == stored[0].thread_id))).one()
    assert thread.agent_id == free.id


@pytest.mark.asyncio
async def test_a_name_in_passing_does_not_re_route(async_session, adapter):
    """"ich habe rex gefragt" is about Rex, not to Rex. Only a leading name or
    an explicit @ addresses somebody."""
    boss = await _agent(async_session, "Boss", "boss")
    await _agent(async_session, "Rex", "rex")

    await _ingest(async_session, adapter, message_event(text="ich habe rex gestern gefragt"))

    stored = await _all_messages(async_session)
    thread = (await async_session.exec(select(Thread).where(Thread.id == stored[0].thread_id))).one()
    assert thread.agent_id == boss.id


@pytest.mark.asyncio
async def test_an_unknown_name_falls_back_to_boss(async_session, adapter):
    boss = await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event(text="@nobody hallo"))

    stored = await _all_messages(async_session)
    thread = (await async_session.exec(select(Thread).where(Thread.id == stored[0].thread_id))).one()
    assert thread.agent_id == boss.id


@pytest.mark.asyncio
async def test_the_recognised_handle_is_recorded_on_the_message(async_session, adapter):
    await _agent(async_session, "Boss", "boss")
    await _agent(async_session, "Rex", "rex")

    await _ingest(async_session, adapter, message_event(text="@rex kurz bitte"))

    stored = await _all_messages(async_session)
    assert stored[0].mentions == ["rex"]


@pytest.mark.asyncio
async def test_without_a_boss_it_says_so_instead_of_dropping_the_message(
    async_session, adapter, transport
):
    await _ingest(async_session, adapter, message_event(text="hallo"))

    assert await _all_messages(async_session) == []
    assert len(transport.calls) == 1
    assert "Boss" in transport.calls[0]["text"]


# ── 3. Threads ────────────────────────────────────────────────────────────


def test_a_channel_message_has_no_room():
    assert room_for(message_event()) is None


def test_the_first_message_of_a_thread_is_still_a_channel_message():
    """Slack sets thread_ts == ts on the parent. That one is not yet a reply."""
    assert room_for(message_event(thread_ts="1753699200.000100")) is None


def test_a_reply_carries_its_thread():
    assert room_for(message_event(ts="1.2", thread_ts="1753699200.000100")) == (
        "1753699200.000100"
    )


@pytest.mark.asyncio
async def test_a_reply_in_a_thread_lands_in_that_conversation(async_session, adapter):
    await _agent(async_session, "Boss", "boss")
    thread = Thread(kind="task", slack_thread_ts="1753699200.000900")
    other = Thread(kind="task", slack_thread_ts="1753699200.000901")
    async_session.add(thread)
    async_session.add(other)
    await async_session.commit()
    await async_session.refresh(thread)
    await async_session.refresh(other)

    await _ingest(
        async_session,
        adapter,
        message_event(text="und der Test?", ts="9.9", thread_ts="1753699200.000900"),
    )

    assert [m.body for m in await _messages(async_session, thread.id)] == ["und der Test?"]
    assert await _messages(async_session, other.id) == []


@pytest.mark.asyncio
async def test_an_unknown_thread_is_asked_about_not_guessed(
    async_session, adapter, transport
):
    """Two decoy threads: an implementation that takes 'the nearest one' fails
    here. Without them the test would be trivially green on an empty database."""
    known = Thread(kind="task", slack_thread_ts="1753699200.000900")
    lonely = Thread(kind="task")
    async_session.add(known)
    async_session.add(lonely)
    await async_session.commit()

    await _ingest(
        async_session,
        adapter,
        message_event(text="hier?", ts="9.9", thread_ts="1753699200.00099999"),
    )

    assert await _all_messages(async_session) == []
    assert len(transport.calls) == 1
    assert transport.calls[0]["thread_ts"] == "1753699200.00099999"


@pytest.mark.asyncio
async def test_an_at_name_inside_a_thread_does_not_move_the_conversation(
    async_session, adapter
):
    """In a task thread the responsible agent is meant. Jumping to somebody's
    DM because a name appears would tear the conversation in half."""
    await _agent(async_session, "Boss", "boss")
    await _agent(async_session, "Rex", "rex")
    thread = Thread(kind="task", slack_thread_ts="1753699200.000900")
    async_session.add(thread)
    await async_session.commit()
    await async_session.refresh(thread)

    await _ingest(
        async_session,
        adapter,
        message_event(text="@rex was meinst du?", ts="9.9", thread_ts="1753699200.000900"),
    )

    stored = await _all_messages(async_session)
    assert len(stored) == 1
    assert stored[0].thread_id == thread.id
    assert stored[0].mentions == ["rex"]


# ── 4. The channel gate ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_message_from_another_channel_is_ignored(async_session, adapter):
    """Slack's twin of Telegram's chat_id gate: never answer strangers."""
    await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event(channel="C-SOMEWHERE-ELSE"))

    assert await _all_messages(async_session) == []


@pytest.mark.asyncio
async def test_nothing_is_processed_without_a_configured_channel(
    async_session, adapter, monkeypatch
):
    monkeypatch.setattr(settings, "slack_default_channel", "", raising=False)
    await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event())

    assert await _all_messages(async_session) == []


@pytest.mark.asyncio
async def test_a_channel_configured_by_name_is_resolved_once(
    async_session, adapter, monkeypatch
):
    """`SLACK_DEFAULT_CHANNEL=#a-name` must gate as tightly as an id."""
    from app.services import slack_client

    monkeypatch.setattr(settings, "slack_default_channel", "#team-chat", raising=False)
    calls = {"n": 0}

    async def fake_resolve(reference):
        calls["n"] += 1
        return CHANNEL if reference == "#team-chat" else None

    monkeypatch.setattr(slack_client, "resolve_channel_id", fake_resolve)
    await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event(text="hallo"))
    await _ingest(async_session, adapter, message_event(channel="C-OTHER"))

    assert len(await _all_messages(async_session)) == 1
    assert calls["n"] == 2


# ── 5. Slack's payload ────────────────────────────────────────────────────


def test_slack_markup_is_unwrapped():
    assert normalise_slack_text("<@U123> schau <https://x.test|hier>") == "@U123 schau hier"
    assert normalise_slack_text("in <#C1|team-chat>") == "in #team-chat"
    assert normalise_slack_text("<https://x.test/a>") == "https://x.test/a"


def test_plain_text_survives_untouched():
    assert normalise_slack_text("a < b und 2 > 1") == "a < b und 2 > 1"


@pytest.mark.asyncio
async def test_an_edit_is_not_a_new_message(async_session, adapter):
    await _agent(async_session, "Boss", "boss")

    await _ingest(
        async_session,
        adapter,
        message_event(subtype="message_changed", text="korrigiert"),
    )

    assert await _all_messages(async_session) == []


@pytest.mark.asyncio
async def test_a_message_without_text_is_ignored(async_session, adapter, transport):
    await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event(text="   "))

    assert await _all_messages(async_session) == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_a_non_message_event_is_ignored(async_session, adapter):
    await _agent(async_session, "Boss", "boss")

    await _ingest(async_session, adapter, message_event(type="app_mention"))

    assert await _all_messages(async_session) == []
