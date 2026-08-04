"""Ein Task hat genau EINEN Task-Thread — und Briefings sind keine Inbox-Post.

Live-Vorfall 2026-08-04 (Live-Beweis des Chat-Antwort-SOUL-Pakets): Der
Dispatcher hielt sein Task-Objekt seit dem Claim; der Operator postete
waehrenddessen eine Thread-Nachricht (legte den Task-Thread an und setzte
``task.thread_id``). ``ensure_task_thread`` vertraute dem stale Objekt
(``thread_id=None``) und legte einen ZWEITEN kind='task'-Thread an —
``task.thread_id`` wurde ueberschrieben, die Operator-Nachricht war fuer den
Agenten unsichtbar (Thread-Scope laeuft ueber ``task.thread_id``). `mc inbox`
lieferte stattdessen das persistierte Dispatch-Briefing als "Neue Nachricht",
und der Agent tat die gesamte Inbox als "Redelivery meines Briefings" ab.

Drei Fixes, hier festgenagelt:
1. ``ensure_task_thread`` sucht select-first per (kind, task_id) statt
   ``task.thread_id`` zu vertrauen.
2. Partieller Unique-Index ``uq_threads_task_per_task`` (Modell + Migration
   0173) laesst das echte Rennen sauber verlieren.
3. Briefing-Marker-Nachrichten erscheinen in keiner Zustellung (Poll + Inbox),
   ruecken aber Cursor/Ack-Ziele weiter wie bisher.
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.thread import Thread
from app.services.dispatch_delivery import _briefing_marker
from app.services.messaging import ensure_task_thread, post_message


async def _board_agent_task(async_session: AsyncSession, *, comm_v2: bool = True):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Probe-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        board_id=board.id,
        comm_v2=comm_v2,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Thread probe",
        status="in_progress",
        dispatched_at=now,
        ack_at=now,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    return board, agent, raw_token, task


@pytest.mark.asyncio
async def test_stale_task_object_reuses_existing_thread(async_session):
    """Der Kern des Vorfalls: ein zweiter Aufruf mit einem Task-Objekt, dessen
    ``thread_id`` noch None ist (stale), darf KEINEN zweiten Thread anlegen."""
    _board, _agent, _token, task = await _board_agent_task(async_session)

    first = await ensure_task_thread(async_session, task)

    # Staleness simulieren: das Objekt kennt den inzwischen angelegten Thread
    # nicht (so sah der Dispatcher den Task — geladen vor dem Commit des
    # Operator-Posts). Der alte Code lief damit in den Create-Pfad.
    task.thread_id = None
    second = await ensure_task_thread(async_session, task)

    assert second.id == first.id
    threads = (
        await async_session.exec(
            select(Thread).where(Thread.kind == "task", Thread.task_id == task.id)
        )
    ).all()
    assert len(threads) == 1
    assert task.thread_id == first.id  # das stale Objekt wurde repariert


@pytest.mark.asyncio
async def test_unique_index_blocks_duplicate_task_threads(async_session):
    """Belt-and-braces: selbst am Service vorbei verliert das Rennen jetzt an
    der DB (Modell und Migration 0173 muessen denselben Index tragen — Tests
    bauen die Tabellen aus dem Modell, Produktion aus der Migration)."""
    _board, _agent, _token, task = await _board_agent_task(async_session)
    await ensure_task_thread(async_session, task)

    async_session.add(Thread(kind="task", task_id=task.id))
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


@pytest.mark.asyncio
async def test_side_threads_stay_unrestricted(async_session):
    """Der Index ist partiell — mehrere side-Threads pro Task bleiben erlaubt."""
    _board, _agent, _token, task = await _board_agent_task(async_session)
    await ensure_task_thread(async_session, task)

    async_session.add(Thread(kind="side", task_id=task.id))
    async_session.add(Thread(kind="side", task_id=task.id))
    await async_session.commit()


@pytest.mark.asyncio
async def test_inbox_skips_briefing_but_keeps_operator_message(
    async_session, client: AsyncClient
):
    """`mc inbox` darf das persistierte Dispatch-Briefing nie als "Neue
    Nachricht" ausliefern — die Operator-Nachricht daneben sehr wohl. Das
    Ack-Ziel bleibt das Thread-Maximum (inkl. Briefing-Seq), damit ein Ack
    sauber ueber alles hinwegzieht."""
    _board, _agent, token, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)

    await post_message(
        async_session, thread_id=thread.id, sender_type="system",
        message_type="system",
        body=f"{_briefing_marker('attempt-1')}\n# New Task: probe",
    )
    await post_message(
        async_session, thread_id=thread.id, sender_type="user",
        message_type="message", body="Bitte im Thread Bescheid geben.",
    )

    resp = await client.get(
        "/api/v1/agent/me/inbox", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    bodies = [m["body"] for m in data["messages"]]
    assert any("Bescheid" in b for b in bodies)
    assert not any("mc:briefing" in b for b in bodies)
    # Ack-Ziel = hoechste seq im Thread (2), nicht nur die gelieferte (2 hier,
    # aber der Punkt: das Briefing zaehlt fuer den Cursor weiter mit).
    assert data["threads"][str(thread.id)] == 2


@pytest.mark.asyncio
async def test_poll_delivery_skips_briefing_too(async_session):
    """Gleiche Regel auf dem Poll-Zustellpfad (_collect_new_messages) — beide
    Pfade teilen den Filter, keiner darf das Briefing echoen."""
    from app.routers.agents import _collect_new_messages

    _board, agent, _token, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)

    await post_message(
        async_session, thread_id=thread.id, sender_type="system",
        message_type="system",
        body=f"{_briefing_marker('attempt-2')}\n# New Task: probe",
    )
    await post_message(
        async_session, thread_id=thread.id, sender_type="user",
        message_type="message", body="Noch da?",
    )

    delivered = await _collect_new_messages(async_session, agent, acked={})
    bodies = [m["body"] for m in delivered]
    assert any("Noch da?" in b for b in bodies)
    assert not any("mc:briefing" in b for b in bodies)
