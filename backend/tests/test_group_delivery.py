"""Mention-gefilterte Zustellung auf Gruppen-Threads (Gruppenchat V1, PR A).

Sturm-Schutz mechanisch (Plan §4.2): Eine Group-Message wird Mitglied X nur
zugestellt, wenn X in message.mentions steht — alles andere schiebt den
Cursor still weiter (exakt das _is_own_message-Muster: Cursor rückt vor,
Payload gefiltert). Verglichen wird fold-tolerant (chat_inbound._fold):
"@Beta-One", "beta_one" und "BetaOne" sind derselbe Agent.

Damit können sich Agenten nicht gegenseitig aufwecken: nur wer explizit das
Wort bekommt (Engine-Brief, Marks @-Mention, Agenten-@-Mention), erhält die
Nachricht. Ein unaufgeforderter Post landet im Protokoll, pusht niemanden.
"""
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.group import AgentGroup, GroupMember
from app.models.thread import AgentThreadCursor, Thread
from app.services.messaging import post_message


async def _make_member(session: AsyncSession, name: str, slug: str):
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=name,
        slug=slug,
        agent_runtime="host",
        agent_token_hash=token_hash,
        comm_v2=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


async def _make_group(session: AsyncSession, members: list[Agent]) -> tuple[AgentGroup, Thread]:
    thread = Thread(kind="group", title="Testgruppe")
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    group = AgentGroup(
        thread_id=thread.id, name="Testgruppe", goal="Frage klären",
        lead_agent_id=members[0].id,
    )
    session.add(group)
    await session.commit()
    await session.refresh(group)
    for i, agent in enumerate(members):
        session.add(GroupMember(
            group_id=group.id, agent_id=agent.id, role="lead" if i == 0 else "member",
        ))
    await session.commit()
    return group, thread


async def _poll(client: AsyncClient, token: str, acked: dict | None = None):
    params = {}
    if acked is not None:
        params["acked_seq"] = json.dumps(acked)
    return await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


async def _cursor(session: AsyncSession, agent: Agent, thread: Thread) -> AgentThreadCursor:
    cur = (
        await session.exec(
            select(AgentThreadCursor).where(
                AgentThreadCursor.agent_id == agent.id,
                AgentThreadCursor.thread_id == thread.id,
            )
        )
    ).one()
    await session.refresh(cur)
    return cur


@pytest.mark.asyncio
async def test_group_message_delivered_only_to_mentioned(
    client: AsyncClient, async_session: AsyncSession
):
    """mentions=["beta"] → Beta bekommt die Nachricht, Alpha nicht — aber
    Alphas Cursor rückt vor (sonst würde jeder Poll erneut scannen)."""
    alpha, alpha_token = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="@beta bitte prüfen",
        mentions=["beta"],
    )

    beta_resp = await _poll(client, beta_token)
    assert [m["id"] for m in beta_resp.json()["new_messages"]] == [str(msg.id)]

    alpha_resp = await _poll(client, alpha_token)
    assert alpha_resp.json()["new_messages"] == []
    alpha_cur = await _cursor(async_session, alpha, thread)
    assert alpha_cur.last_delivered_seq == msg.seq


@pytest.mark.asyncio
async def test_mention_matching_is_fold_tolerant(
    client: AsyncClient, async_session: AsyncSession
):
    """"@Beta-One" trifft den Agenten mit Slug "betaone" (Gross/Klein,
    Bindestrich/Unterstrich fallen aus dem Vergleich — chat_inbound._fold)."""
    betaone, token = await _make_member(async_session, "BetaOne", "betaone")
    other, _ = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [betaone, other])

    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="@Beta-One schau mal",
        mentions=["Beta-One"],
    )

    resp = await _poll(client, token)
    assert [m["id"] for m in resp.json()["new_messages"]] == [str(msg.id)]


@pytest.mark.asyncio
async def test_agent_reply_reaches_mentioned_not_sender(
    client: AsyncClient, async_session: AsyncSession
):
    """Beta antwortet mit @alpha: Alpha bekommt sie, Beta nicht (eigene
    Nachricht — advancet nur den Cursor, wie überall)."""
    alpha, alpha_token = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="agent",
        sender_id=beta.id,
        message_type="message",
        body="@alpha erledigt",
        mentions=["alpha"],
    )

    alpha_resp = await _poll(client, alpha_token)
    assert [m["id"] for m in alpha_resp.json()["new_messages"]] == [str(msg.id)]

    beta_resp = await _poll(client, beta_token)
    assert beta_resp.json()["new_messages"] == []


@pytest.mark.asyncio
async def test_unmentioned_group_message_pushes_nobody(
    client: AsyncClient, async_session: AsyncSession
):
    """Sturm-Probe (Plan §8): Post ohne mentions → NIEMAND bekommt ihn
    zugestellt, beide Cursor rücken vor. Ein unaufgeforderter Agenten-Post
    kann so strukturell keinen anderen Agenten aufwecken."""
    alpha, alpha_token = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="agent",
        sender_id=alpha.id,
        message_type="message",
        body="unaufgeforderter Zwischenruf",
        mentions=[],
    )

    for token in (alpha_token, beta_token):
        resp = await _poll(client, token)
        assert resp.json()["new_messages"] == []
    for agent in (alpha, beta):
        cur = await _cursor(async_session, agent, thread)
        assert cur.last_delivered_seq == msg.seq


@pytest.mark.asyncio
async def test_task_thread_delivery_stays_unfiltered(
    client: AsyncClient, async_session: AsyncSession
):
    """Regression-Schutz: Auf task/dm/chat-Threads gilt der Mention-Filter
    NICHT — dort wird weiter alles zugestellt (bestehendes Verhalten)."""
    import datetime as dt

    from app.models.board import Board
    from app.models.task import Task
    from app.services.messaging import ensure_task_thread

    agent, token = await _make_member(async_session, "Solo", "solo")
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)
    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id, assigned_agent_id=agent.id, title="T",
        status="in_progress", dispatched_at=now, ack_at=now,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    thread = await ensure_task_thread(async_session, task)

    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="ohne mentions",
        mentions=[],
    )

    resp = await _poll(client, token)
    assert [m["id"] for m in resp.json()["new_messages"]] == [str(msg.id)]


@pytest.mark.asyncio
async def test_inbox_pull_applies_same_mention_filter(
    client: AsyncClient, async_session: AsyncSession
):
    """GET /me/inbox (Pull-Pfad von `mc inbox`) filtert identisch — eine Regel,
    beide Zustellwege."""
    alpha, alpha_token = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="@beta nur für dich",
        mentions=["beta"],
    )

    beta_resp = await client.get(
        "/api/v1/agent/me/inbox", headers={"Authorization": f"Bearer {beta_token}"}
    )
    assert [m["id"] for m in beta_resp.json()["messages"]] == [str(msg.id)]

    alpha_resp = await client.get(
        "/api/v1/agent/me/inbox", headers={"Authorization": f"Bearer {alpha_token}"}
    )
    assert alpha_resp.json()["messages"] == []
