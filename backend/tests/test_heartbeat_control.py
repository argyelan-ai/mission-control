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


@pytest.mark.asyncio
async def test_heartbeat_with_payload_task_id_sees_stopped_run(client: AsyncClient):
    """Fix 3b case (a): stop AFTER dispatch. agent.current_task_id was
    cleared by stop_task_run and the task is `blocked`+run_control=stopped,
    so the in_progress lookup finds nothing — but the bridge reports its
    live turn context (payload.task_id). The next heartbeat MUST deliver
    control.interrupt=hard — resolved via the payload task_id."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        # Simulate stop_task_run: status blocked + run_control=stopped,
        # agent lock released (operations.py:stop_task_run).
        task.status = "blocked"
        task.run_control = "stopped"
        task.blocked_at = dt.datetime.now(tz=dt.timezone.utc)
        s.add(task)
        agent.current_task_id = None
        s.add(agent)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "task_id": str(task.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["control"]["interrupt"] == "hard"
    assert "reason" in body["control"]


@pytest.mark.asyncio
async def test_heartbeat_without_task_id_keeps_legacy_behavior(client: AsyncClient):
    """Fix 3b case (b): an OLD bridge sends no task_id — the response must
    stay byte-identical to the pre-Fix-3b shape (no control for a stopped
    run whose pointer was cleared: the legacy fall-through)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        task.status = "blocked"
        task.run_control = "stopped"
        task.blocked_at = dt.datetime.now(tz=dt.timezone.utc)
        s.add(task)
        agent.current_task_id = None
        s.add(agent)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working"},  # NO task_id — legacy bridge
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Legacy: pre-heal pointer lookup finds nothing (pointer cleared) ->
    # no control. AND the Bug-18 self-heal coerced status to idle.
    assert "control" not in body


@pytest.mark.asyncio
async def test_heartbeat_foreign_task_id_is_ignored(client: AsyncClient):
    """Fix 3b case (c): a task_id of ANOTHER agent's task must NEVER steer
    this agent's control channel (sabotage guard) — response has no control
    and the agent does not flip on the foreign task."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, _own_task, token = await _agent_with_task(s)
        # Foreign agent + its stopped task.
        raw2, hash2 = generate_agent_token()
        other = Agent(
            name=f"Other-{uuid.uuid4().hex[:6]}",
            agent_runtime="cli-bridge",
            agent_token_hash=hash2,
            board_id=agent.board_id,
            scopes=["heartbeat"],
        )
        s.add(other)
        await s.commit()
        now = dt.datetime.now(tz=dt.timezone.utc)
        foreign_task = Task(
            board_id=agent.board_id,
            assigned_agent_id=other.id,
            title="Foreign stopped run",
            status="blocked",
            run_control="stopped",
            blocked_at=now,
        )
        s.add(foreign_task)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "task_id": str(foreign_task.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "control" not in body


@pytest.mark.asyncio
async def test_heartbeat_blocked_turn_signal_keeps_working_status(client: AsyncClient):
    """Fix 3b / Guard 3 (live incident 08.09.2026 10:15): the lead sets the
    RUNNING task to blocked; without a payload task_id the Bug-18 self-heal
    coerced the agent to idle and the next dispatch pasted into the live
    turn. WITH the bridge's task_id the agent stays working (Guard 3 keeps
    queueing) AND the control channel reports hard."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        task.status = "blocked"  # lead stopped the running task
        task.run_control = "stopped"
        task.blocked_at = dt.datetime.now(tz=dt.timezone.utc)
        s.add(task)
        agent.current_task_id = None
        s.add(agent)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "task_id": str(task.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["control"]["interrupt"] == "hard"
    # Agent stays working -> Guard 3 (status == "working") still queues.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from sqlmodel import select as _select
        fresh = (await s.exec(
            _select(Agent).where(Agent.id == agent.id)
        )).one()
        assert fresh.status == "working"
        assert fresh.run_state == "running"


# ── Withdrawn-task guard (09.09.2026) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_heartbeat_hard_when_running_task_requeued_to_inbox(client: AsyncClient):
    """Lead moves the card back to inbox while the bridge still reports the
    turn (payload.task_id) → hard interrupt with a 'nicht weiterarbeiten'
    reason. Before the fix nothing fired and the turn ran on for 48 min."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        task.status = "inbox"
        s.add(task)
        agent.current_task_id = None
        s.add(agent)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "task_id": str(task.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["control"]["interrupt"] == "hard"
    assert "entzogen" in body["control"]["reason"]


@pytest.mark.asyncio
async def test_heartbeat_hard_when_running_task_reassigned_to_other_agent(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        _, other_hash = generate_agent_token()
        other = Agent(
            name=f"Beta-{uuid.uuid4().hex[:6]}", agent_runtime="cli-bridge",
            agent_token_hash=other_hash, board_id=agent.board_id, scopes=["heartbeat"],
        )
        s.add(other)
        await s.commit()
        task.assigned_agent_id = other.id
        s.add(task)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "task_id": str(task.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["control"]["interrupt"] == "hard"


@pytest.mark.asyncio
async def test_heartbeat_own_finish_to_review_is_not_withdrawn(client: AsyncClient):
    """The agent's own `mc finish` sets review while its turn context still
    reports the task — that is NOT a withdrawal, no interrupt."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_task(s)
        task.status = "review"
        s.add(task)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "task_id": str(task.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert "control" not in resp.json()
