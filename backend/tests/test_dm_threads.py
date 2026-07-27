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
