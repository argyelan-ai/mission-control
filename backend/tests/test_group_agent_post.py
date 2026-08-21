"""Agenten-Antworten im Gruppen-Thread (Gruppenchat V1, PR A).

POST /api/v1/agent/threads/{id}/messages ist der Rückweg (`mc msg --thread`).
Auf Gruppen-Threads gilt zusätzlich:
- @-Mentions im Body werden gegen die MITGLIEDER aufgelöst und auf der
  Message gespeichert — nur so erreicht ein Agent einen anderen (Zustellung
  ist mention-gefiltert).
- KEIN Mention-Default auf den Lead: ein unaufgeforderter Agenten-Post weckt
  strukturell niemanden (Sturm-Schutz) — er liegt im Protokoll, die Engine
  (PR B) sammelt ihn ins nächste Brief-Delta.
- Jeder Gruppen-Post wird als SSE-Event auf den Gruppen-Kanal publiziert,
  damit Marks UI live mitliest.
"""
import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.group import AgentGroup, GroupMember
from app.models.thread import Message, Thread


async def _make_member(session: AsyncSession, name: str, slug: str):
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=name,
        slug=slug,
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        comm_v2=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


async def _make_group(session: AsyncSession, members: list[Agent]):
    thread = Thread(kind="group", title="G")
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    group = AgentGroup(
        thread_id=thread.id, name="G", goal="Ziel", lead_agent_id=members[0].id
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


@pytest.fixture
def sse_calls(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def _fake_broadcast(channel: str, event_type: str, data: dict) -> None:
        calls.append((channel, event_type, data))

    monkeypatch.setattr("app.services.sse.broadcast", _fake_broadcast)
    return calls


@pytest.mark.asyncio
async def test_agent_post_resolves_mentions_and_broadcasts(
    client: AsyncClient, async_session: AsyncSession, sse_calls
):
    alpha, _ = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    group, thread = await _make_group(async_session, [alpha, beta])

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        headers={"Authorization": f"Bearer {beta_token}"},
        json={"body": "@alpha erledigt, Quelle: https://example.org"},
    )
    assert resp.status_code == 201, resp.text

    msg = (
        await async_session.exec(
            select(Message).where(Message.thread_id == thread.id)
        )
    ).one()
    assert msg.mentions == ["alpha"]

    group_events = [c for c in sse_calls if c[0] == f"mc:events:group:{group.id}"]
    assert len(group_events) == 1
    assert group_events[0][1] == "group.message_posted"
    assert group_events[0][2]["message"]["body"].startswith("@alpha erledigt")


@pytest.mark.asyncio
async def test_agent_post_without_mentions_wakes_nobody(
    client: AsyncClient, async_session: AsyncSession, sse_calls
):
    """Kein Lead-Default für Agenten-Posts — mentions bleibt leer (der
    Zustell-Filter liefert ihn niemandem; die Engine sammelt ihn später)."""
    alpha, _ = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        headers={"Authorization": f"Bearer {beta_token}"},
        json={"body": "unaufgeforderter Zwischenstand"},
    )
    assert resp.status_code == 201

    msg = (
        await async_session.exec(
            select(Message).where(Message.thread_id == thread.id)
        )
    ).one()
    assert msg.mentions == []


@pytest.mark.asyncio
async def test_group_post_never_mirrors_to_chat_channels(
    client: AsyncClient, async_session: AsyncSession, sse_calls, monkeypatch
):
    """Plan §4.5: Gruppen spiegeln in V1 NICHT nach Slack/Telegram — eine
    autonome Runde würde die Kanäle fluten. Gilt für Agenten-Posts (der
    Operator-Pfad setzt mirror_to_telegram=False bereits im Service)."""
    mirror_calls = []

    async def _fake_mirror(session, message, attachment=None):
        mirror_calls.append(message)

    monkeypatch.setattr(
        "app.services.messaging._maybe_mirror_to_chat", _fake_mirror
    )

    alpha, _ = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        headers={"Authorization": f"Bearer {beta_token}"},
        json={"body": "@alpha Zwischenstand"},
    )
    assert resp.status_code == 201
    assert mirror_calls == []


@pytest.mark.asyncio
async def test_task_thread_post_does_not_hit_group_channel(
    client: AsyncClient, async_session: AsyncSession, sse_calls
):
    """Regression: Posts auf Task-Threads publizieren NICHT auf Gruppen-Kanäle
    und bekommen keine Mention-Auflösung übergestülpt."""
    import datetime as dt

    from app.models.board import Board
    from app.models.task import Task
    from app.services.messaging import ensure_task_thread

    agent, token = await _make_member(async_session, "Solo", "solo")
    board = Board(name="B", slug="b-sse")
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

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"body": "@alpha status"},
    )
    assert resp.status_code == 201
    assert [c for c in sse_calls if c[0].startswith("mc:events:group:")] == []
