"""Subtask → blocked muss einen System-Comment auf den Parent posten.

Lücke vor Fix (2026-04-23): Wenn ein Worker (z.B. Tester) einen Subtask auf
`blocked` setzt, gab es zwar:
  1. Eine Approval fuer den Operator
  2. (Bei vorhandenem Gateway) eine RPC-Nachricht an den Lead
  3. Ein Activity-Event

Aber KEINEN sichtbaren Comment auf dem PARENT-Task. Der Parent-Owner (Boss
oder ein orchestrierender Agent) sah im /poll keinen Hinweis und konnte nicht
reagieren — der Parent blieb stuck bis ein menschlicher Eingriff erfolgte.

Dieser Test deckt: nach blocked auf einem Subtask wird ein TaskComment vom
Type 'blocker' auf dem Parent erstellt, der via /poll an den Parent-Owner
ausgeliefert wird (siehe _DELIVER_SYSTEM_COMMENT_TYPES in agents.py).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_parent_subtask_setup(*, task_status: str = "in_progress"):
    """Board + Worker (Tester) + Boss (Parent-Owner) + Parent-Task + Subtask.

    Parent ist Boss zugewiesen, Subtask dem Worker — exakt das Setup aus dem
    Live-Bug (Boss orchestriert, Tester arbeitet auf Subtask).
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    boss_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    subtask_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Bug2 Board", slug="bug2")
        s.add(board)

        worker_token_raw, worker_token_hash = generate_agent_token()
        worker = Agent(
            id=worker_id,
            name="Tester",
            role="developer",
            board_id=board_id,
            agent_token_hash=worker_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
            # Kein gateway_agent_id → cli-bridge-Agent ohne Gateway-RPC.
            # Replicates production: Boss + Worker laufen via cli-bridge,
            # die RPC-Notification-Strecke greift nicht.
        )
        s.add(worker)

        boss_token_raw, boss_token_hash = generate_agent_token()
        boss = Agent(
            id=boss_id,
            name="Boss",
            role="lead",
            board_id=board_id,
            agent_token_hash=boss_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:manage"],
        )
        s.add(boss)

        parent = Task(
            id=parent_id,
            board_id=board_id,
            title="Parent Orchestrator Task",
            status="in_progress",
            assigned_agent_id=boss_id,
        )
        s.add(parent)

        subtask = Task(
            id=subtask_id,
            board_id=board_id,
            parent_task_id=parent_id,
            title="Implement Bullet-Test",
            status=task_status,
            assigned_agent_id=worker_id,
        )
        s.add(subtask)
        await s.commit()
        for obj in [board, worker, boss, parent, subtask]:
            await s.refresh(obj)

    return {
        "board": board,
        "worker": worker,
        "boss": boss,
        "parent": parent,
        "subtask": subtask,
        "worker_token": worker_token_raw,
        "boss_token": boss_token_raw,
    }


@pytest.mark.asyncio
async def test_subtask_blocked_posts_system_comment_on_parent(client, fake_redis):
    """Subtask → blocked erzeugt einen 'blocker'-Comment auf dem Parent-Task."""
    data = await _create_parent_subtask_setup(task_status="in_progress")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False
            mock_rpc.chat_send = AsyncMock()

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['subtask'].id}",
                json={
                    "status": "blocked",
                    "blocker_type": "technical_problem",
                    "blocker_question": "Sidecar antwortet nicht — wie weiter?",
                    "blocker_description": "Curl auf /healthz timeout",
                },
                headers={"Authorization": f"Bearer {data['worker_token']}"},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "blocked"

    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == data["parent"].id,
                TaskComment.comment_type == "blocker",
            )
        )
        comments = list(result.all())

    assert len(comments) == 1, (
        f"Erwartet: 1 blocker-Comment auf Parent. Gefunden: {len(comments)}"
    )
    cmt = comments[0]
    assert cmt.author_type == "agent"
    assert cmt.author_agent_id == data["worker"].id
    assert "Subtask blocked" in cmt.content or "blocked" in cmt.content.lower()
    # Subtask-Titel sollte erwaehnt sein
    assert "Bullet-Test" in cmt.content
    # Blocker-Frage sollte erwaehnt sein damit Boss kontextualisiert reagieren kann
    assert "Sidecar" in cmt.content


@pytest.mark.asyncio
async def test_subtask_blocked_no_comment_when_blocked_on_subtask(client, fake_redis):
    """Wenn der Subtask selbst auf einen weiteren Sub-Subtask wartet
    (blocked_by_task_id gesetzt), ist das interne Orchestration —
    kein Comment auf dem Parent (waere Laerm)."""
    data = await _create_parent_subtask_setup(task_status="in_progress")

    # Sub-Subtask anlegen auf den der subtask wartet
    from app.models.task import Task
    sub_subtask_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub_sub = Task(
            id=sub_subtask_id,
            board_id=data["board"].id,
            parent_task_id=data["subtask"].id,
            title="Inner delegated work",
            status="in_progress",
            assigned_agent_id=data["worker"].id,
        )
        s.add(sub_sub)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['subtask'].id}",
                json={
                    "status": "blocked",
                    "blocked_by_task_id": str(sub_subtask_id),
                },
                headers={"Authorization": f"Bearer {data['worker_token']}"},
            )

    assert resp.status_code == 200, resp.text

    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskComment).where(TaskComment.task_id == data["parent"].id)
        )
        parent_comments = list(result.all())

    assert parent_comments == [], (
        f"Bei callback-wait darf KEIN Comment auf Parent gepostet werden. "
        f"Gefunden: {parent_comments}"
    )


@pytest.mark.asyncio
async def test_root_task_blocked_no_parent_comment(client, fake_redis):
    """Root-Task (parent_task_id is None) → kein Parent-Comment-Versuch (no-op)."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Root", slug="root")
        s.add(board)
        worker_token_raw, worker_token_hash = generate_agent_token()
        worker = Agent(
            id=worker_id, name="Solo", role="developer", board_id=board_id,
            agent_token_hash=worker_token_hash, is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(worker)
        task = Task(
            id=uuid.uuid4(), board_id=board_id, title="Root Task",
            status="in_progress", assigned_agent_id=worker_id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False

            resp = await client.patch(
                f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
                json={
                    "status": "blocked",
                    "blocker_type": "decision_needed",
                    "blocker_question": "Operator, was tun?",
                },
                headers={"Authorization": f"Bearer {worker_token_raw}"},
            )

    # Root-Task blocked-Pfad muss weiterhin sauber durchlaufen
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "blocked"


@pytest.mark.asyncio
async def test_subtask_blocked_no_self_echo(client, fake_redis):
    """Wenn der Worker zufaellig SELBST der Parent-Owner ist (z.B. Boss
    delegiert an sich selbst — selten aber moeglich), darf kein Echo-Comment
    entstehen."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    boss_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Self", slug="self")
        s.add(board)
        boss_token_raw, boss_token_hash = generate_agent_token()
        boss = Agent(
            id=boss_id, name="Boss", role="lead", board_id=board_id,
            agent_token_hash=boss_token_hash, is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(boss)
        parent = Task(
            id=uuid.uuid4(), board_id=board_id, title="Parent",
            status="in_progress", assigned_agent_id=boss_id,
        )
        s.add(parent)
        subtask = Task(
            id=uuid.uuid4(), board_id=board_id, parent_task_id=parent.id,
            title="Self Sub", status="in_progress", assigned_agent_id=boss_id,
        )
        s.add(subtask)
        await s.commit()
        for o in [parent, subtask]:
            await s.refresh(o)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False

            resp = await client.patch(
                f"/api/v1/agent/boards/{board_id}/tasks/{subtask.id}",
                json={
                    "status": "blocked",
                    "blocker_type": "decision_needed",
                    "blocker_question": "?",
                },
                headers={"Authorization": f"Bearer {boss_token_raw}"},
            )

    assert resp.status_code == 200

    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == parent.id,
                TaskComment.comment_type == "blocker",
            )
        )
        parent_blocker_comments = list(result.all())

    assert parent_blocker_comments == [], (
        "Bei Self-Delegation darf kein Echo-Comment auf Parent entstehen"
    )
