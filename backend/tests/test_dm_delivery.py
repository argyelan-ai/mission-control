"""Ein Agent muss Nachrichten aus seinem DM-Thread bekommen.

W1 hat den Zustell-Scope bewusst auf Task-Threads aktiver Aufgaben begrenzt.
Damit erreicht Marks Nachricht im Allgemein-Chat den Boss nie — der Chat waere
eine Attrappe. Dieser Test pinnt die Erweiterung.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.services.messaging import ensure_dm_thread, post_message


async def _agent_with_token(async_session: AsyncSession):
    raw, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Boss-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        comm_v2=True,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    return agent, raw


async def _poll(client: AsyncClient, token: str):
    return await client.get(
        "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"}
    )


@pytest.mark.asyncio
async def test_dm_message_reaches_the_agent(client: AsyncClient, async_session):
    agent, token = await _agent_with_token(async_session)
    thread = await ensure_dm_thread(async_session, agent)
    posted = await post_message(
        async_session, thread_id=thread.id, sender_type="user",
        message_type="message", body="Kurze Frage vorab, noch keine Aufgabe.",
    )
    await async_session.commit()

    body = (await _poll(client, token)).json()
    ids = [m["id"] for m in (body.get("new_messages") or [])]
    assert str(posted.id) in ids, "DM-Nachricht wurde nicht zugestellt"


@pytest.mark.asyncio
async def test_dm_has_no_task_and_does_not_fast_forward(client: AsyncClient, async_session):
    """Ein DM-Thread haengt an keiner Aufgabe — die done/failed-Fast-Forward-Regel
    darf hier nicht greifen, sonst verschwindet die erste Nachricht."""
    agent, token = await _agent_with_token(async_session)
    thread = await ensure_dm_thread(async_session, agent)
    first = await post_message(
        async_session, thread_id=thread.id, sender_type="user",
        message_type="message", body="allererste Nachricht",
    )
    await async_session.commit()

    body = (await _poll(client, token)).json()
    assert str(first.id) in [m["id"] for m in (body.get("new_messages") or [])]


@pytest.mark.asyncio
async def test_agent_without_comm_v2_gets_nothing(client: AsyncClient, async_session):
    agent, token = await _agent_with_token(async_session)
    agent.comm_v2 = False
    async_session.add(agent)
    await async_session.commit()

    thread = await ensure_dm_thread(async_session, agent)
    await post_message(
        async_session, thread_id=thread.id, sender_type="user",
        message_type="message", body="darf nicht ankommen",
    )
    await async_session.commit()

    body = (await _poll(client, token)).json()
    assert not body.get("new_messages")
