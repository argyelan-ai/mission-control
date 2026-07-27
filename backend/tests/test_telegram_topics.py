"""Telegram forum topic <-> MC thread mapping + lifecycle service.

Ein Telegram-Thema (message_thread_id) bildet 1:1 auf einen MC-Thread ab. Diese
Tests fahren ohne Netz: der Telegram-Client wird injiziert und gefaelscht. Die
Live-Fakten (27.07., am Produktiv-Bot gemessen) sind hier festgeschrieben:
createForumTopic/editForumTopic/deleteForumTopic funktionieren im Privatchat,
closeForumTopic NICHT, und `not a forum` muss sauber degradieren statt zu werfen.
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.models.thread import Thread


async def _task_thread(async_session: AsyncSession, title: str = "Recherche") -> Thread:
    """Ein Task-Thread wie ihn ensure_task_thread anlegt (Task + kind='task')."""
    task = Task(board_id=uuid.uuid4(), title=title)
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    thread = Thread(kind="task", task_id=task.id)
    async_session.add(thread)
    await async_session.commit()
    await async_session.refresh(thread)
    return thread


# ── DB-Fundament: die 1:1-Zuordnung ist unique ────────────────────────────
#
# Der Constraint steht im SQLModel-Modell UND in der Migration. Steht er nur in
# der Migration, baut der Test seine Tabelle aus dem Modell ohne ihn und beweist
# nichts (genau dieser Fehler passierte in PR #171). Darum: der Test muss den
# Constraint an der modell-gebauten Tabelle treffen.

@pytest.mark.asyncio
async def test_double_topic_assignment_is_rejected_by_the_database(async_session: AsyncSession):
    a = await _task_thread(async_session, "A")
    b = await _task_thread(async_session, "B")

    a.telegram_topic_id = 4242
    async_session.add(a)
    await async_session.commit()

    b.telegram_topic_id = 4242  # dasselbe Thema an zwei Threads -> verboten
    async_session.add(b)
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


@pytest.mark.asyncio
async def test_many_threads_may_have_no_topic(async_session: AsyncSession):
    """NULL ist kein Wert im Sinne von UNIQUE — beliebig viele Threads ohne Thema."""
    await _task_thread(async_session, "A")
    await _task_thread(async_session, "B")
    await _task_thread(async_session, "C")

    rows = (await async_session.exec(select(Thread))).all()
    assert sum(1 for t in rows if t.telegram_topic_id is None) == 3


# ── Gefaelschter Telegram-Client (kein Netz) ──────────────────────────────

from app.services.telegram_topics import (  # noqa: E402
    GENERAL_TOPIC_ID,
    TELEGRAM_TOPIC_NAME_MAX,
    TelegramNotAForumError,
    TelegramRateLimitError,
    TelegramTopicError,
    ensure_topic_for_thread,
    mark_topic_done,
    purge_old_topics,
)


class FakeForumClient:
    """Zeichnet Aufrufe auf und kann gezielt Fehler werfen — ersetzt das Netz."""

    def __init__(self, *, next_id: int = 100, create_raises: Exception | None = None,
                 edit_raises: Exception | None = None, delete_raises: Exception | None = None):
        self.created: list[str] = []
        self.edited: list[tuple[int, str]] = []
        self.deleted: list[int] = []
        self._next_id = next_id
        self._create_raises = create_raises
        self._edit_raises = edit_raises
        self._delete_raises = delete_raises

    async def create_forum_topic(self, name: str) -> int:
        self.created.append(name)
        if self._create_raises is not None:
            raise self._create_raises
        tid = self._next_id
        self._next_id += 1
        return tid

    async def edit_forum_topic(self, message_thread_id: int, name: str) -> None:
        if self._edit_raises is not None:
            raise self._edit_raises
        self.edited.append((message_thread_id, name))

    async def delete_forum_topic(self, message_thread_id: int) -> None:
        if self._delete_raises is not None:
            raise self._delete_raises
        self.deleted.append(message_thread_id)


async def _dm_thread(async_session: AsyncSession) -> Thread:
    thread = Thread(kind="dm", agent_id=uuid.uuid4(), title="DM Boss")
    async_session.add(thread)
    await async_session.commit()
    await async_session.refresh(thread)
    return thread


# ── ensure_topic_for_thread ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_topic_creates_and_persists(async_session: AsyncSession):
    thread = await _task_thread(async_session, "Landing-Page")
    client = FakeForumClient(next_id=555)

    topic_id = await ensure_topic_for_thread(async_session, thread, client)

    assert topic_id == 555
    assert len(client.created) == 1
    await async_session.refresh(thread)
    assert thread.telegram_topic_id == 555


@pytest.mark.asyncio
async def test_ensure_topic_handles_unique_collision(async_session: AsyncSession):
    """Notnagel (P2.2-Review): scheitert unser Commit am Unique-Constraint
    (uq_threads_telegram_topic_id), weil ein anderer Thread dieses Thema schon
    haelt, degradiert ensure sauber statt den IntegrityError durchzureichen.

    In der Praxis vergibt Telegram global eindeutige IDs — hier wird die
    Kollision erzwungen, indem der gefaelschte Client zweimal dieselbe ID
    ausgibt: A belegt 500, B bekommt 500 -> Kollision an der DB."""
    a = await _task_thread(async_session, "A")
    b = await _task_thread(async_session, "B")

    assert await ensure_topic_for_thread(async_session, a, FakeForumClient(next_id=500)) == 500

    # B bekommt vom (gefaelschten) Telegram dieselbe ID 500.
    colliding = FakeForumClient(next_id=500)
    result = await ensure_topic_for_thread(async_session, b, colliding)

    assert result is None, "kein Crash, B bleibt ungemappt"
    await async_session.refresh(b)
    assert b.telegram_topic_id is None


@pytest.mark.asyncio
async def test_ensure_topic_is_idempotent(async_session: AsyncSession):
    thread = await _task_thread(async_session, "Recherche")
    client = FakeForumClient()

    first = await ensure_topic_for_thread(async_session, thread, client)
    second = await ensure_topic_for_thread(async_session, thread, client)

    assert first == second
    assert len(client.created) == 1, "zweiter Aufruf darf kein zweites Thema anlegen"


@pytest.mark.asyncio
async def test_ensure_topic_degrades_when_chat_not_a_forum(async_session: AsyncSession):
    """Der Chat wird erst zum Forum, wenn der Nutzer das erste Thema anlegt.
    Vorher liefert Telegram `not a forum` — das muss None ergeben, nicht werfen."""
    thread = await _task_thread(async_session, "Recherche")
    client = FakeForumClient(create_raises=TelegramNotAForumError("the chat is not a forum"))

    topic_id = await ensure_topic_for_thread(async_session, thread, client)

    assert topic_id is None
    await async_session.refresh(thread)
    assert thread.telegram_topic_id is None, "kein halber Zustand bei Degradation"


@pytest.mark.asyncio
async def test_ensure_topic_survives_rate_limit(async_session: AsyncSession):
    """HTTP 429 darf keinen Crash ausloesen — der Thread bleibt einfach ungemappt."""
    thread = await _task_thread(async_session, "Recherche")
    client = FakeForumClient(create_raises=TelegramRateLimitError("Too Many Requests"))

    topic_id = await ensure_topic_for_thread(async_session, thread, client)

    assert topic_id is None
    await async_session.refresh(thread)
    assert thread.telegram_topic_id is None


@pytest.mark.asyncio
async def test_ensure_topic_survives_generic_error(async_session: AsyncSession):
    thread = await _task_thread(async_session, "Recherche")
    client = FakeForumClient(create_raises=TelegramTopicError("boom"))

    assert await ensure_topic_for_thread(async_session, thread, client) is None


@pytest.mark.asyncio
async def test_general_topic_is_never_created(async_session: AsyncSession):
    """Das Allgemein-Thema (DM-Thread) hat keine eigene ID (reserviert: 0) und
    wird nie via API angelegt — seine Nachrichten gehen ohne message_thread_id."""
    thread = await _dm_thread(async_session)
    client = FakeForumClient()

    topic_id = await ensure_topic_for_thread(async_session, thread, client)

    assert topic_id == GENERAL_TOPIC_ID == 0
    assert client.created == [], "fuer das Allgemein-Thema darf kein createForumTopic laufen"
    await async_session.refresh(thread)
    assert thread.telegram_topic_id is None, "0 wird nicht persistiert — der Thread hat kein eigenes Thema"


@pytest.mark.asyncio
async def test_topic_title_is_truncated_to_telegram_limit(async_session: AsyncSession):
    long_title = "L" * 300
    thread = await _task_thread(async_session, long_title)
    client = FakeForumClient()

    await ensure_topic_for_thread(async_session, thread, client)

    assert len(client.created) == 1
    assert len(client.created[0]) <= TELEGRAM_TOPIC_NAME_MAX


# ── mark_topic_done ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_done_prefixes_check_once(async_session: AsyncSession):
    thread = await _task_thread(async_session, "Recherche")
    client = FakeForumClient()
    await ensure_topic_for_thread(async_session, thread, client)

    await mark_topic_done(async_session, thread, client)
    await mark_topic_done(async_session, thread, client)

    assert len(client.edited) == 2
    for _tid, name in client.edited:
        assert name.startswith("✓ ")
        assert not name.startswith("✓ ✓"), "das Haekchen darf nie doppelt gesetzt werden"


@pytest.mark.asyncio
async def test_mark_done_does_not_double_prefix_an_already_checked_title(async_session: AsyncSession):
    """Trifft die Strip-Zeile in telegram_topics.py (`base = title[len(PREFIX):]
    ...`) tatsaechlich: traegt der Titel-Ursprung schon ein `✓ ` (z.B. ein
    operator-gesetzter Thread-Titel oder ein erneuter Lauf), darf mark_done kein
    zweites Haekchen davorsetzen.

    Der bestehende Idempotenz-Test verfehlt diese Zeile, weil er den Titel frisch
    aus dem Task rechnet (`#<id> Recherche` beginnt nie mit `✓ `) — der
    Reviewer konnte die Zeile sabotieren, ohne dass ein Test rot wurde. Dieser
    Thread hat keinen Task/kein Projekt, sein Titel IST bereits `✓ …`."""
    thread = Thread(kind="task", title="✓ Erledigt", telegram_topic_id=777)
    async_session.add(thread)
    await async_session.commit()
    await async_session.refresh(thread)
    client = FakeForumClient()

    await mark_topic_done(async_session, thread, client)

    assert len(client.edited) == 1
    _tid, name = client.edited[0]
    assert name == "✓ Erledigt", "genau ein Haekchen — die Strip-Zeile muss greifen"


@pytest.mark.asyncio
async def test_mark_done_skips_threads_without_own_topic(async_session: AsyncSession):
    """Still gelaufene Aufgaben (kein Thema) und das Allgemein-Thema haben nichts
    umzubenennen."""
    silent = await _task_thread(async_session, "Still")
    general = await _dm_thread(async_session)
    client = FakeForumClient()

    await mark_topic_done(async_session, silent, client)   # telegram_topic_id is None
    await mark_topic_done(async_session, general, client)  # DM = Allgemein

    assert client.edited == []


# ── purge_old_topics ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_deletes_old_closed_topics_and_nulls_the_id(async_session: AsyncSession):
    from datetime import datetime, timedelta

    thread = await _task_thread(async_session, "Alt")
    client = FakeForumClient()
    topic_id = await ensure_topic_for_thread(async_session, thread, client)
    thread.closed_at = datetime.utcnow() - timedelta(days=45)
    async_session.add(thread)
    await async_session.commit()

    purged = await purge_old_topics(async_session, client, older_than_days=30)

    assert purged == 1
    assert client.deleted == [topic_id]
    await async_session.refresh(thread)
    assert thread.telegram_topic_id is None


@pytest.mark.asyncio
async def test_purge_leaves_recent_and_open_topics_alone(async_session: AsyncSession):
    from datetime import datetime, timedelta

    recent = await _task_thread(async_session, "Neu")
    still_open = await _task_thread(async_session, "Offen")
    client = FakeForumClient()
    await ensure_topic_for_thread(async_session, recent, client)
    await ensure_topic_for_thread(async_session, still_open, client)
    recent.closed_at = datetime.utcnow() - timedelta(days=5)  # geschlossen, aber jung
    async_session.add(recent)
    await async_session.commit()

    purged = await purge_old_topics(async_session, client, older_than_days=30)

    assert purged == 0
    assert client.deleted == []
