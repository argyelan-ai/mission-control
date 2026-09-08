"""Fix 3 (omp stop-signal) — heartbeat control channel tests.

agent_heartbeat gains a `control` object: {"interrupt": "hard"|"soft",
"reason": str}. The omp-bridge heartbeater reads it while a native-TUI turn
is running and interrupts the run (hard) or nudges+continues (soft).

Triggers (hard):
  - run_control == "stopped"  (operator Stop-Knopf)
  - task status "blocked" by a FOREIGN actor (user/lead), i.e. the newest
    blocker/handoff comment of the blocked episode is NOT authored by the
    agent itself.
Trigger (soft):
  - unread blocker/handoff comments beyond the agent's comment cursor.
Non-triggers:
  - the agent's OWN block (self `mc blocked`) must NOT interrupt.
  - a plain `message` comment must NOT interrupt.
No-control: a heartbeat on an agent with no active task carries NO `control`
key (legacy response shape).
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


async def _agent_with_task(
    session: AsyncSession,
    *,
    task_status: str = "in_progress",
    run_control: str | None = None,
):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Alpha-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        board_id=board.id,
        scopes=["heartbeat", "tasks:read"],
    )
    session.add(agent)
    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Interrupt probe",
        status=task_status,
        run_control=run_control,
        dispatched_at=now,
        ack_at=now,
        blocked_at=now if task_status == "blocked" else None,
    )
    session.add(task)
    await session.commit()
    # The working agent holds the active-task lock (claim semantics) — the
    # heartbeat's control read uses it to resolve blocked/stopped runs.
    agent.current_task_id = task.id
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    await session.refresh(task)
    return agent, task, raw_token


async def _add_comment(
    session: AsyncSession,
    task: Task,
    *,
    author_type: str = "user",
    author_agent_id: uuid.UUID | None = None,
    comment_type: str = "message",
    content: str = "note",
):
    c = TaskComment(
        task_id=task.id,
        author_type=author_type,
        author_agent_id=author_agent_id,
        comment_type=comment_type,
        content=content,
    )
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


async def _heartbeat(client: AsyncClient, token: str):
    return await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working"},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_heartbeat_hard_on_run_control_stopped(client: AsyncClient):
    """Trigger 1: run_control=stopped (Stop-Knopf) -> hard interrupt."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s, run_control="stopped")

    resp = await _heartbeat(client, token)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["control"]["interrupt"] == "hard"
    assert "reason" in body["control"]


@pytest.mark.asyncio
async def test_heartbeat_hard_on_foreign_block(client: AsyncClient):
    """Trigger 2: task blocked by a foreign actor (user blocker comment on
    the blocked episode) -> hard interrupt."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s, task_status="blocked")
        await _add_comment(
            s, task, author_type="user", comment_type="blocker",
            content="STOPP — Umleitung auf Fix 4",
        )

    resp = await _heartbeat(client, token)
    body = resp.json()
    assert body["control"]["interrupt"] == "hard"


@pytest.mark.asyncio
async def test_heartbeat_soft_on_unread_blocker_comment(client: AsyncClient):
    """Trigger 3: unread blocker comment beyond the agent's cursor on an
    in_progress task -> soft (nudge + continue, same session)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        seen = await _add_comment(
            s, task, author_type="agent", author_agent_id=agent.id,
            comment_type="progress", content="checkpoint",
        )
        # Advance the agent's comment cursor past its own progress comment.
        from app.models.agent_task_comment_cursor import AgentTaskCommentCursor
        s.add(AgentTaskCommentCursor(
            agent_id=agent.id, task_id=task.id,
            last_seen_comment_id=seen.id,
        ))
        await s.commit()
        # THEN the lead posts a blocker comment -> unread -> soft (posted in
        # the second session below).

    # Second session: create the foreign (lead) author + the unread blocker
    # comment.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead_raw, lead_hash = generate_agent_token()
        lead = Agent(
            name=f"Lead-{uuid.uuid4().hex[:6]}",
            agent_runtime="host",
            agent_token_hash=lead_hash,
            board_id=task.board_id,
        )
        s.add(lead)
        await s.commit()
        await s.refresh(lead)
        await _add_comment(
            s, task, author_type="agent", author_agent_id=lead.id,
            comment_type="blocker", content="STOPP — bitte License pruefen",
        )

    resp = await _heartbeat(client, token)
    body = resp.json()
    assert body["control"]["interrupt"] == "soft"


@pytest.mark.asyncio
async def test_heartbeat_own_block_does_not_interrupt(client: AsyncClient):
    """The agent's OWN `mc blocked` must NOT interrupt its own run: the
    newest blocker comment of the episode is self-authored -> no control."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s, task_status="blocked")
        await _add_comment(
            s, task, author_type="agent", author_agent_id=agent.id,
            comment_type="blocker", content="Warte auf Approval",
        )
        # The agent has seen its own comment (poll auto-acks delivered
        # comments) — advance the cursor so soft doesn't fire either.
        from sqlmodel import select as _select
        from app.models.agent_task_comment_cursor import AgentTaskCommentCursor
        comments = list((await s.exec(
            _select(TaskComment).where(TaskComment.task_id == task.id)
        )).all())
        s.add(AgentTaskCommentCursor(
            agent_id=agent.id, task_id=task.id,
            last_seen_comment_id=comments[-1].id,
        ))
        await s.commit()

    resp = await _heartbeat(client, token)
    body = resp.json()
    assert "control" not in body


@pytest.mark.asyncio
async def test_heartbeat_message_comment_does_not_interrupt(client: AsyncClient):
    """A plain `message` comment (routine note / audit) is NOT a wake signal
    — no control, no soft, no hard."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        await _add_comment(
            s, task, author_type="user", comment_type="message",
            content="BTW: schones Wetter heute",
        )

    resp = await _heartbeat(client, token)
    body = resp.json()
    assert "control" not in body


@pytest.mark.asyncio
async def test_heartbeat_hard_beats_soft(client: AsyncClient):
    """run_control=stopped AND unread blocker comment -> hard (both at once
    -> hard)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s, run_control="stopped")
        lead_raw, lead_hash = generate_agent_token()
        lead = Agent(
            name=f"Lead-{uuid.uuid4().hex[:6]}",
            agent_runtime="host",
            agent_token_hash=lead_hash,
            board_id=task.board_id,
        )
        s.add(lead)
        await s.commit()
        await s.refresh(lead)
        await _add_comment(
            s, task, author_type="agent", author_agent_id=lead.id,
            comment_type="blocker", content="STOPP",
        )

    resp = await _heartbeat(client, token)
    body = resp.json()
    assert body["control"]["interrupt"] == "hard"


@pytest.mark.asyncio
async def test_heartbeat_no_task_has_no_control(client: AsyncClient):
    """Legacy response shape: no active task -> NO `control` key (the
    sabotage probe / old poll.sh agents stay byte-compatible)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            name=f"Idle-{uuid.uuid4().hex[:6]}",
            agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            board_id=board.id,
            scopes=["heartbeat"],
        )
        s.add(agent)
        await s.commit()

    resp = await _heartbeat(client, raw_token)
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert "control" not in body
