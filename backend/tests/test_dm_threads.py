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
