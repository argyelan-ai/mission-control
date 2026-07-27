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
