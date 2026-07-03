"""Subtask → blocked must post a system comment on the parent.

Gap before fix (2026-04-23): when a worker (e.g. Tester) set a subtask to
`blocked`, there was:
  1. An approval for the operator
  2. (If a gateway was present) an RPC message to the lead
  3. An activity event

But NO visible comment on the PARENT task. The parent owner (Boss or an
orchestrating agent) saw no hint in /poll and couldn't react — the parent
stayed stuck until a human intervened.

This test covers: after blocked on a subtask, a TaskComment of type
'blocker' is created on the parent, which gets delivered to the parent
owner via /poll (see _DELIVER_SYSTEM_COMMENT_TYPES in agents.py).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_parent_subtask_setup(*, task_status: str = "in_progress"):
    """Board + Worker (Tester) + Boss (parent owner) + parent task + subtask.

    Parent is assigned to Boss, subtask to the worker — exactly the setup
    from the live bug (Boss orchestrates, Tester works on the subtask).
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
            # No gateway_agent_id → cli-bridge agent without gateway RPC.
            # Replicates production: Boss + Worker run via cli-bridge,
            # the RPC notification path doesn't apply.
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
    """Subtask → blocked creates a 'blocker' comment on the parent task."""
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
    # Subtask title should be mentioned
    assert "Bullet-Test" in cmt.content
    # Blocker question should be mentioned so Boss can react with context
    assert "Sidecar" in cmt.content


@pytest.mark.asyncio
async def test_subtask_blocked_no_comment_when_blocked_on_subtask(client, fake_redis):
    """If the subtask itself is waiting on another sub-subtask
    (blocked_by_task_id set), that's internal orchestration —
    no comment on the parent (would be noise)."""
    data = await _create_parent_subtask_setup(task_status="in_progress")

    # Create the sub-subtask the subtask is waiting on
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
    """Root task (parent_task_id is None) → no parent comment attempt (no-op)."""
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

    # Root task blocked path must still complete cleanly
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "blocked"


@pytest.mark.asyncio
async def test_subtask_blocked_no_self_echo(client, fake_redis):
    """If the worker happens to be the parent owner ITSELF (e.g. Boss
    delegates to itself — rare but possible), no echo comment must be
    created."""
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
