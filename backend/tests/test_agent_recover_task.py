"""/agent/me/recover-task — Recovery-Endpoint für Poll-Runtime Agents.

Nach Container/Host-Restart während aktivem Task ist die tmux/claude-Session
weg, aber DB-Status bleibt `in_progress`. poll.sh kann den Prompt nicht mehr
pasten, weil /agent/me/poll nur `state=working` zurückgibt. Der Recovery-
Endpoint setzt den Task auf inbox → nächster Poll liefert ihn als `new_task`
mit frischem Prompt.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_agent_with_task(session: AsyncSession, status: str = "in_progress", runtime: str = "cli-bridge"):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.auth import generate_agent_token
    from app.utils import utcnow

    board = Board(id=uuid.uuid4(), name="Test", slug="t")
    session.add(board)
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name="Researcher",
        board_id=board.id,
        agent_runtime=runtime,
        status="idle",
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    session.add(agent)
    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Research: something",
        status=status,
        assigned_agent_id=agent.id,
        dispatched_at=utcnow(),
        ack_at=utcnow(),
        started_at=utcnow(),
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task, raw_token


@pytest.mark.asyncio
async def test_recover_resets_in_progress_task_to_inbox(client: AsyncClient):
    """Task in_progress → Recovery setzt auf inbox + clearet Dispatch-Tracking."""
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        task_id = task.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recovered"] is True
    assert data["task_id"] == str(task_id)
    assert data["previous_status"] == "in_progress"

    # DB: Task ist zurueck auf inbox, dispatch-tracking gelöscht
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task_id)
        assert refreshed.status == "inbox"
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None
        assert refreshed.started_at is None


@pytest.mark.asyncio
async def test_recover_idempotent_when_no_active_task(client: AsyncClient):
    """Kein aktiver Task → recovered=False, kein Fehler."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Setup: Agent ohne in_progress Task (task ist 'done')
        _, agent, task, token = await _setup_agent_with_task(s, status="done")

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recovered"] is False
    assert data["reason"] == "no_active_task"


@pytest.mark.asyncio
async def test_recover_leaves_system_comment(client: AsyncClient):
    """Recovery postet einen system-Kommentar fuer audit trail."""
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s)
        task_id = task.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        recovery_comments = [c for c in comments if c.comment_type == "system" and "Recovery" in c.content]
        assert len(recovery_comments) == 1
        assert "re-dispatched" in recovery_comments[0].content.lower() or "wird re-dispatched" in recovery_comments[0].content


@pytest.mark.asyncio
async def test_recover_works_for_host_runtime(client: AsyncClient):
    """Host-Runtime Agents (Boss) brauchen Recovery genauso nach launchd-Restart."""
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress", runtime="host")
        task_id = task.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recovered"] is True

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task_id)
        assert refreshed.status == "inbox"


@pytest.mark.asyncio
async def test_recover_clears_run_control_stopped(client: AsyncClient):
    """Recovery muss run_control=stopped loeschen — sonst Deadlock beim Agent.

    Szenario: Task war 'stopped' vom User, wurde spaeter manuell reassigned,
    lief wieder, Agent versuchte status=review → Backend blockte wegen
    run_control=stopped. Recovery muss run_control mit zuruecksetzen.
    """
    from app.models.task import Task
    from app.utils import utcnow

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        # Simuliere stopped Run-Control
        t = await s.get(Task, task.id)
        t.run_control = "stopped"
        s.add(t)
        await s.commit()
        task_id = t.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["recovered"] is True

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task_id)
        assert refreshed.status == "inbox"
        assert refreshed.run_control is None, "run_control muss gecleared sein"


@pytest.mark.asyncio
async def test_recover_rate_limited_after_recent_recovery(client: AsyncClient):
    """Schutz gegen poll.sh-Crash-Loop: zwei Recoveries in <60s → zweiter wird abgelehnt.

    Wenn der erste Recovery einen Task auf inbox setzt und der nächste Poll-Zyklus
    den Task wieder claimt (status=in_progress), darf ein zweiter Recovery-Call
    nicht sofort wieder auf inbox setzen — sonst entsteht ein Infinite-Loop.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")

    # Erster Recovery: erfolgreich
    r1 = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.json()["recovered"] is True

    # Simuliere: Task wurde inzwischen wieder geclaimt (inbox → in_progress)
    from app.models.task import Task
    from app.utils import utcnow
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.status = "in_progress"
        t.dispatched_at = utcnow()
        t.ack_at = utcnow()
        s.add(t)
        await s.commit()

    # Zweiter Recovery innerhalb 60s: muss rate-limited werden
    r2 = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    data = r2.json()
    assert data["recovered"] is False
    assert data["reason"] == "rate_limited"
    assert "last_recovery_at" in data

    # Task bleibt in_progress — nicht zurück auf inbox gesetzt
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.status == "in_progress"


@pytest.mark.asyncio
async def test_recover_then_poll_delivers_new_task(client: AsyncClient):
    """Kompletter Recovery-Flow: recover → poll gibt new_task mit Prompt zurück."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")

    recover = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert recover.status_code == 200
    assert recover.json()["recovered"] is True

    poll = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert poll.status_code == 200
    data = poll.json()
    assert data["state"] == "new_task"
    assert "task" in data
    assert data["task"]["id"] == str(task.id)
    assert "prompt" in data["task"] and len(data["task"]["prompt"]) > 0
