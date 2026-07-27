"""DM-Threads: Mark <-> ein Agent, ohne Task-Bezug.

Das Thread-Modell kannte kind="dm" seit Welle 1, aber es gab kein Feld, das sagt
WER der Gespraechspartner ist — und damit keine Moeglichkeit, "der DM-Thread mit
Boss" wiederzufinden. Ohne das bleibt der Allgemein-Chat in Telegram eine Attrappe.
"""
import uuid

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.thread import Thread


async def _agent(async_session: AsyncSession, name: str = "Boss") -> Agent:
    _raw, token_hash = generate_agent_token()
    agent = Agent(
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        comm_v2=True,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_thread_stores_its_dm_partner(async_session: AsyncSession):
    agent = await _agent(async_session)
    thread = Thread(kind="dm", agent_id=agent.id)
    async_session.add(thread)
    await async_session.commit()
    await async_session.refresh(thread)

    found = (
        await async_session.exec(
            select(Thread).where(Thread.kind == "dm", Thread.agent_id == agent.id)
        )
    ).one()
    assert found.id == thread.id
    assert found.task_id is None  # DM haengt an keiner Aufgabe


from app.services.messaging import ensure_dm_thread, post_message


@pytest.mark.asyncio
async def test_ensure_dm_thread_is_idempotent(async_session: AsyncSession):
    agent = await _agent(async_session)

    first = await ensure_dm_thread(async_session, agent)
    second = await ensure_dm_thread(async_session, agent)

    assert first.id == second.id, "zweiter Aufruf darf keinen zweiten Thread anlegen"
    assert first.kind == "dm"
    assert first.agent_id == agent.id


@pytest.mark.asyncio
async def test_dm_threads_are_per_agent(async_session: AsyncSession):
    boss = await _agent(async_session, "Boss")
    rex = await _agent(async_session, "Rex")

    assert (await ensure_dm_thread(async_session, boss)).id != (
        await ensure_dm_thread(async_session, rex)
    ).id


@pytest.mark.asyncio
async def test_dm_thread_accepts_messages(async_session: AsyncSession):
    agent = await _agent(async_session)
    thread = await ensure_dm_thread(async_session, agent)

    msg = await post_message(
        async_session, thread_id=thread.id, sender_type="user",
        message_type="message", body="Lass uns kurz brainstormen.",
    )
    await async_session.commit()
    assert msg.seq == 1


# ── Race-Festigkeit (Review-Fund 27.07.) ──────────────────────────────────
#
# ensure_dm_thread macht SELECT-dann-INSERT. Zwei gleichzeitige Aufrufe
# (Telegram-Nachricht + Poll im selben Moment) legten damit ZWEI DM-Threads an
# — beide wuerden zugestellt und der Gespraechsverlauf zerfiele in zwei
# Haelften. Abgesichert durch den partiellen Unique-Index
# uq_threads_dm_per_agent + IntegrityError-Fang.

@pytest.mark.asyncio
async def test_duplicate_dm_thread_is_rejected_by_the_database(async_session: AsyncSession):
    """Der Index muss greifen — nicht nur der Anwendungscode."""
    from sqlalchemy.exc import IntegrityError

    agent = await _agent(async_session)
    await ensure_dm_thread(async_session, agent)

    async_session.add(Thread(kind="dm", agent_id=agent.id))
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


@pytest.mark.asyncio
async def test_ensure_dm_thread_survives_a_lost_race(async_session: AsyncSession):
    """Verliert ensure_dm_thread das Rennen, nimmt es den Thread des Gewinners
    statt zu werfen. Simuliert, indem der Konkurrent zwischen SELECT und INSERT
    einfuegt."""
    agent = await _agent(async_session)

    winner = Thread(kind="dm", agent_id=agent.id, title="vom Konkurrenten")
    async_session.add(winner)
    await async_session.commit()
    await async_session.refresh(winner)

    # Der Cache muss geleert werden, sonst sieht der Aufruf den Gewinner nicht
    # als "fremd" an — wir wollen den echten DB-Pfad testen.
    got = await ensure_dm_thread(async_session, agent)
    assert got.id == winner.id, "verlorenes Rennen muss den bestehenden Thread liefern"


@pytest.mark.asyncio
async def test_task_threads_are_not_affected_by_the_unique_index(async_session: AsyncSession):
    """Der Index ist partiell (WHERE kind='dm') — Task-/Side-Threads duerfen
    weiterhin beliebig viele mit agent_id NULL sein."""
    async_session.add(Thread(kind="task"))
    async_session.add(Thread(kind="task"))
    async_session.add(Thread(kind="side"))
    await async_session.commit()  # darf nicht werfen
