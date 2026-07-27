"""Ausgehende Spiegelung Thread-Nachricht -> Telegram-Thema (P2.3).

Ohne Netz: Topic-Client und Bot werden injiziert und gefaelscht. Geprueft werden
Absender-Prefix, die Ping-Regel (stumm/laut), die Nachtruhe, das Allgemein-Thema
(kein message_thread_id), die Degradation bei nicht bereitem Telegram, der
Schleifenschutz (user + Herkunfts-Skip) und die Ausfallsicherheit (nie werfen).
"""
import uuid
from datetime import datetime

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.thread import Message, Thread
from app.services.dispatch_delivery import _briefing_marker
from app.services.messaging import BACKFILL_SEED_BODY
from app.services.telegram_outbound import mirror_message_to_telegram

DAY = datetime(2026, 7, 27, 14, 0, 0)     # 14:00 — Tag
NIGHT = datetime(2026, 7, 27, 2, 0, 0)    # 02:00 — Nachtruhe


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, text, *, message_thread_id=None, disable_notification=False):
        self.sent.append(
            {
                "text": text,
                "message_thread_id": message_thread_id,
                "disable_notification": disable_notification,
            }
        )
        return 1


class FakeTopicClient:
    """Nur create wird evtl. gebraucht; Themen sind meist vorab gesetzt."""

    def __init__(self, *, next_id=100, create_raises=None):
        self.created: list[str] = []
        self._next_id = next_id
        self._create_raises = create_raises

    async def create_forum_topic(self, name: str) -> int:
        self.created.append(name)
        if self._create_raises is not None:
            raise self._create_raises
        tid = self._next_id
        self._next_id += 1
        return tid

    async def edit_forum_topic(self, message_thread_id, name):  # pragma: no cover
        ...

    async def delete_forum_topic(self, message_thread_id):  # pragma: no cover
        ...


async def _thread(session, *, kind="task", topic_id=None, title=None):
    t = Thread(kind=kind, title=title, telegram_topic_id=topic_id)
    if kind == "dm":
        t.agent_id = uuid.uuid4()
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


async def _agent(session, name="Rex"):
    a = Agent(name=name)
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return a


def _msg(thread, *, sender_type="agent", sender_id=None, body="Fertig.",
         message_type="message", mentions=None, question_meta=None):
    return Message(
        thread_id=thread.id,
        seq=1,
        sender_type=sender_type,
        sender_id=sender_id,
        message_type=message_type,
        body=body,
        mentions=mentions if mentions is not None else [],
        question_meta=question_meta,
    )


# ── Absender-Prefix ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_message_is_prefixed_with_agent_name(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session, "Rex")
    bot, tc = FakeBot(), FakeTopicClient()

    sent = await mirror_message_to_telegram(
        async_session, _msg(thread, sender_id=agent.id, body="fertig"),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert sent is True
    assert bot.sent[0]["text"] == "Rex: fertig"
    assert bot.sent[0]["message_thread_id"] == 555


@pytest.mark.asyncio
async def test_system_message_is_prefixed_with_system(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session, _msg(thread, sender_type="system", body="Watchdog"),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert bot.sent[0]["text"] == "System: Watchdog"


# ── Schleifenschutz ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_message_is_never_mirrored(async_session: AsyncSession):
    """Mark hat sie geschrieben — Schleifenschutz Sperre 1 (deckt den heutigen
    Telegram-Inbound-Pfad ab, der als sender_type='user' postet)."""
    thread = await _thread(async_session, topic_id=555)
    bot, tc = FakeBot(), FakeTopicClient()

    sent = await mirror_message_to_telegram(
        async_session, _msg(thread, sender_type="user", body="mach das"),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert sent is False
    assert bot.sent == []


@pytest.mark.asyncio
async def test_dispatch_briefing_is_not_mirrored(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    bot, tc = FakeBot(), FakeTopicClient()
    body = f"{_briefing_marker('abc')}\nRiesiges Dispatch-Briefing ..."

    sent = await mirror_message_to_telegram(
        async_session, _msg(thread, sender_type="system", body=body),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert sent is False
    assert bot.sent == []


@pytest.mark.asyncio
async def test_backfill_seed_is_not_mirrored(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    bot, tc = FakeBot(), FakeTopicClient()

    sent = await mirror_message_to_telegram(
        async_session, _msg(thread, sender_type="system", body=BACKFILL_SEED_BODY),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert sent is False


# ── Ping-Regel (Tag) ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plain_message_is_silent(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session, _msg(thread, sender_id=agent.id),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert bot.sent[0]["disable_notification"] is True


@pytest.mark.asyncio
async def test_question_pings_loud(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session,
        _msg(thread, sender_id=agent.id, message_type="question",
             question_meta={"awaiting": True, "to": "mark"}),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert bot.sent[0]["disable_notification"] is False


@pytest.mark.asyncio
async def test_mention_of_mark_pings_loud(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session, _msg(thread, sender_id=agent.id, mentions=["@Mark"]),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert bot.sent[0]["disable_notification"] is False


@pytest.mark.asyncio
async def test_approval_category_pings_loud(async_session: AsyncSession):
    """Approval/Review haengen ihre Lautstaerke an question_meta['category'] —
    der dokumentierte Slot, den die (spaetere) Approval/Review-in-Thread-Wiring
    setzt."""
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session,
        _msg(thread, sender_id=agent.id, message_type="status",
             question_meta={"category": "review"}),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert bot.sent[0]["disable_notification"] is False


# ── Nachtruhe 23–07 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_night_silences_a_question(async_session: AsyncSession):
    """Nachts ist selbst eine Frage stumm — ausser sie ist critical."""
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session,
        _msg(thread, sender_id=agent.id, message_type="question",
             question_meta={"awaiting": True, "priority": "high"}),
        topic_client=tc, bot=bot, now=NIGHT,
    )

    assert bot.sent[0]["disable_notification"] is True


@pytest.mark.asyncio
async def test_night_lets_critical_through(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session,
        _msg(thread, sender_id=agent.id, message_type="question",
             question_meta={"awaiting": True, "priority": "critical"}),
        topic_client=tc, bot=bot, now=NIGHT,
    )

    assert bot.sent[0]["disable_notification"] is False


# ── Allgemein-Thema + Degradation ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_general_topic_sends_without_thread_id(async_session: AsyncSession):
    """DM-Thread = Allgemein-Thema (Sentinel 0) -> ohne message_thread_id."""
    thread = await _thread(async_session, kind="dm")
    agent = await _agent(async_session)
    bot, tc = FakeBot(), FakeTopicClient()

    sent = await mirror_message_to_telegram(
        async_session, _msg(thread, sender_id=agent.id, body="hallo"),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert sent is True
    assert bot.sent[0]["message_thread_id"] is None


@pytest.mark.asyncio
async def test_no_send_when_telegram_not_ready(async_session: AsyncSession):
    """Kein Thema und Telegram noch kein Forum -> ensure liefert None -> kein Send."""
    from app.services.telegram_topics import TelegramNotAForumError

    thread = await _thread(async_session)  # kein topic_id
    agent = await _agent(async_session)
    bot = FakeBot()
    tc = FakeTopicClient(create_raises=TelegramNotAForumError("not a forum"))

    sent = await mirror_message_to_telegram(
        async_session, _msg(thread, sender_id=agent.id),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert sent is False
    assert bot.sent == []


# ── Ausfallsicherheit ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mirror_never_raises_on_bot_error(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    agent = await _agent(async_session)

    class ExplodingBot:
        async def send_message(self, *a, **k):
            raise RuntimeError("telegram down")

    sent = await mirror_message_to_telegram(
        async_session, _msg(thread, sender_id=agent.id),
        topic_client=FakeTopicClient(), bot=ExplodingBot(), now=DAY,
    )

    assert sent is False  # geschluckt, nicht geworfen


@pytest.mark.asyncio
async def test_unknown_agent_falls_back_to_generic_name(async_session: AsyncSession):
    thread = await _thread(async_session, topic_id=555)
    bot, tc = FakeBot(), FakeTopicClient()

    await mirror_message_to_telegram(
        async_session, _msg(thread, sender_id=uuid.uuid4(), body="x"),
        topic_client=tc, bot=bot, now=DAY,
    )

    assert bot.sent[0]["text"] == "Agent: x"


# ── Nachtruhe in der Zeitzone des Operators ───────────────────────────────
#
# Review-Fund 27.07. (MAJOR): _is_night nutzte datetime.now(). Der Kommentar
# nahm an, das sei CH-Ortszeit — der Mac Mini laeuft so. post_message laeuft
# aber im Backend-CONTAINER, und der steht auf UTC (live geprueft: Container
# 19:56 waehrend es in Zuerich 21:56 war). Folge: die Nacht-Grenze war um 1-2h
# verschoben, ein lauter Approval-Ping um 08:00 CEST waere STUMM angekommen.
#
# Warum keiner der bestehenden Tests das fing: sie injizieren alle ein naives
# `now=` und laufen damit nie durch den echten datetime.now()-Pfad. Diese Tests
# gehen ueber echte UTC-Zeitstempel — die Form, in der das Backend rechnet.

from datetime import timezone as _timezone


def _utc(hour: int, minute: int = 0):
    from datetime import datetime as _dt
    return _dt(2026, 7, 27, hour, minute, tzinfo=_timezone.utc)


def test_night_is_measured_in_operator_time_not_utc():
    """Sommer: Zuerich = UTC+2. 06:00 UTC ist 08:00 in Zuerich — Tag, nicht Nacht.
    Genau der Fall, in dem ein Approval-Ping stumm liegengeblieben waere."""
    from app.services.telegram_outbound import _is_night

    assert _is_night(_utc(6, 0)) is False, "08:00 Zuerich ist Tag — Ping muss laut sein"
    assert _is_night(_utc(5, 30)) is False, "07:30 Zuerich ist Tag"


def test_night_starts_at_local_eleven_pm():
    """22:00 UTC = 00:00 Zuerich → Nacht. 20:00 UTC = 22:00 Zuerich → noch Tag."""
    from app.services.telegram_outbound import _is_night

    assert _is_night(_utc(22, 0)) is True
    assert _is_night(_utc(20, 0)) is False
    assert _is_night(_utc(21, 30)) is True  # 23:30 Zuerich


def test_winter_offset_is_handled_too():
    """Im Winter ist Zuerich UTC+1 — ZoneInfo rechnet die Umstellung mit,
    ein fester Offset wuerde hier zweimal im Jahr falsch liegen."""
    from datetime import datetime as _dt
    from app.services.telegram_outbound import _is_night

    # 06:30 UTC im Januar = 07:30 Zuerich → Tag
    assert _is_night(_dt(2026, 1, 15, 6, 30, tzinfo=_timezone.utc)) is False
    # 22:30 UTC im Januar = 23:30 Zuerich → Nacht
    assert _is_night(_dt(2026, 1, 15, 22, 30, tzinfo=_timezone.utc)) is True


def test_naive_datetime_is_treated_as_local():
    """Bestehende Tests uebergeben naive Lokalzeit — das muss weiter gelten."""
    from datetime import datetime as _dt
    from app.services.telegram_outbound import _is_night

    assert _is_night(_dt(2026, 7, 27, 23, 30)) is True
    assert _is_night(_dt(2026, 7, 27, 12, 0)) is False
