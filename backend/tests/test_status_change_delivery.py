"""Tests for status-change delivery (Issue #386, point 1) — narrowed scope.

Contract: `record_task_event()` (app/services/task_lifecycle.py) is called on
EVERY task status change. Behind a switch (default OFF), it must additionally
write a deliverable `TaskComment` (author_type="system", comment_type="system")
naming from/to status + changed_by (+ reason if given), so the assigned agent
learns about a status change it didn't trigger itself via the normal poll path
(`/agent/me/poll` → `app.routers.agents._collect_and_ack_new_comments`).

No delivery when: the switch is off; the change was made by the assigned
agent itself (no echo); the task has no assigned_agent_id; or the target
status is terminal (`failed`/`aborted` — poll doesn't deliver on those lanes
anyway, see `_collect_and_ack_new_comments`'s active-task status filter).
"""

import uuid

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.config import settings
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from app.services.task_lifecycle import record_task_event

from .conftest import test_engine


async def _poll_deliverable_comments(session: AsyncSession, agent: Agent) -> list[dict]:
    """Exercise the real `/agent/me/poll` comment-delivery path for `agent`."""
    from app.routers.agents import _collect_and_ack_new_comments
    return await _collect_and_ack_new_comments(agent, session)


async def _make_assigned_task(
    s: AsyncSession, *, name_suffix: str, assigned_agent_id: uuid.UUID | None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Board + a task, optionally assigned to `assigned_agent_id`. Returns
    (board_id, task_id)."""
    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    s.add(Board(id=board_id, name=f"SCD-{name_suffix}", slug=f"scd-{uuid.uuid4().hex[:6]}"))
    s.add(Task(
        id=task_id, board_id=board_id, title="Status-Change-Delivery Task",
        status="in_progress", assigned_agent_id=assigned_agent_id,
    ))
    await s.commit()
    return board_id, task_id


async def _make_agent(s: AsyncSession, board_id: uuid.UUID, *, name: str) -> Agent:
    agent = Agent(
        id=uuid.uuid4(), name=name, role="developer",
        board_id=board_id, agent_token_hash=generate_agent_token()[1],
        scopes=["tasks:read"],
    )
    s.add(agent)
    await s.commit()
    await s.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_flag_default_off_and_in_allowlist():
    """`status_change_delivery_enabled` defaults to False and is registered
    in the channel-settings allowlist (same pattern as slack_approvals_enabled)."""
    from app.services.channel_config import CHANNEL_SETTING_FIELDS

    assert settings.status_change_delivery_enabled is False
    assert CHANNEL_SETTING_FIELDS.get("status_change_delivery_enabled") is bool


@pytest.mark.asyncio
async def test_operator_status_change_creates_delivered_comment(monkeypatch):
    """Switch ON, operator (changed_by='user') moves a task assigned to X —
    a deliverable system comment appears, and X's poll surfaces it with
    source == 'system'."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="op", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerX")

        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "in_progress", "review",
            changed_by="user", agent_id=None, reason="manual_move",
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert len(comments) == 1
        c = comments[0]
        assert c.author_type == "system"
        assert c.author_agent_id is None
        assert c.comment_type == "system"
        assert "in_progress" in c.content
        assert "review" in c.content
        assert "user" in c.content
        assert "manual_move" in c.content

        delivered = await _poll_deliverable_comments(s, worker)
        assert len(delivered) == 1
        assert delivered[0]["task_id"] == str(task_id)
        assert delivered[0]["source"] == "system"


@pytest.mark.asyncio
async def test_flag_off_creates_no_comment(monkeypatch):
    """Switch OFF (default) — no comment is written at all."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", False, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="off", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerOff")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "in_progress", "review",
            changed_by="user", agent_id=None,
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert comments == []


@pytest.mark.asyncio
async def test_own_change_no_echo(monkeypatch):
    """Switch ON, but agent_id == task.assigned_agent_id (the agent itself
    moved its own card) — no comment (no echo loop)."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="echo", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerEcho")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "in_progress", "review",
            changed_by="agent", agent_id=worker.id,
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert comments == []


@pytest.mark.asyncio
async def test_unassigned_task_no_comment(monkeypatch):
    """Switch ON, but the task has no assigned_agent_id — nobody to deliver
    to, so no comment is written."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="unassigned", assigned_agent_id=None,
        )

        await record_task_event(
            s, task_id, "in_progress", "review",
            changed_by="user", agent_id=None,
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert comments == []


@pytest.mark.asyncio
async def test_other_agent_change_delivered(monkeypatch):
    """Switch ON, changed_by='agent' with agent_id of a DIFFERENT agent
    (e.g. the Lead) than the card's assigned_agent_id (Worker) — Worker
    still gets it delivered."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="other-agent", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerOther")
        lead = await _make_agent(s, board_id, name="LeadOther")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "in_progress", "review",
            changed_by="agent", agent_id=lead.id,
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert len(comments) == 1

        delivered = await _poll_deliverable_comments(s, worker)
        assert len(delivered) == 1
        assert delivered[0]["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_terminal_failed_no_comment(monkeypatch):
    """Switch ON, to_status='failed' (terminal) — no comment, even though
    the change comes from someone other than the assigned agent."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="terminal", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerTerminal")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "in_progress", "failed",
            changed_by="user", agent_id=None,
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert comments == []


@pytest.mark.asyncio
async def test_watchdog_change_with_affected_agent_id_is_delivered(monkeypatch):
    """Review finding A: the watchdog passes the AFFECTED agent's id as
    agent_id (task_runner stuck_no_terminal_patch). That is not the agent
    acting on its own card, so it must be delivered — the no-echo guard only
    applies to changed_by="agent"."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="wd", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerWatchdog")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "in_progress", "blocked",
            changed_by="watchdog", agent_id=worker.id,
            reason="stuck_no_terminal_patch",
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert len(comments) == 1
        assert "watchdog" in comments[0].content
        assert "stuck_no_terminal_patch" in comments[0].content


@pytest.mark.asyncio
async def test_system_internal_change_not_delivered(monkeypatch):
    """Review finding B: system transitions whose path already writes its
    own system comment or redispatches (review rejection redispatch,
    requeue, ...) are on a block list. No second comment."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="sys", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerSystem")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "review", "inbox",
            changed_by="system", agent_id=None,
            reason="review_rejection_redispatch",
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert comments == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["telegram_button_resume", "clarification_answered"])
async def test_actor_tracking_self_delivering_reasons_no_second_comment(monkeypatch, reason):
    """Pruefbericht N3/N4 follow-up (R3): the Telegram-button resume and the
    clarification-answered path both already write their own TaskComment
    right before calling record_task_event() (the "UNBLOCKED" /
    "Antwort auf deine Klaerungsfrage" comment). Both reasons are on the
    self-delivering block list, so record_task_event() must not add a
    second, generic status-change comment on top."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix=f"actor-{reason[:6]}", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name=f"Worker-{reason[:6]}")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "blocked", "in_progress",
            changed_by="user", agent_id=None,
            reason=reason, actor_label="telegram",
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert comments == []


@pytest.mark.asyncio
async def test_system_parent_reopen_is_delivered(monkeypatch):
    """Review finding B, second round: system transitions that nothing else
    announces (parent reopened for a new subtask, phase auto-advance) MUST
    be delivered — the block list is by reason, not by changed_by."""
    monkeypatch.setattr(settings, "status_change_delivery_enabled", True, raising=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, task_id = await _make_assigned_task(
            s, name_suffix="reopen", assigned_agent_id=None,
        )
        worker = await _make_agent(s, board_id, name="WorkerReopen")
        task = (await s.exec(select(Task).where(Task.id == task_id))).one()
        task.assigned_agent_id = worker.id
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task_id, "review", "in_progress",
            changed_by="system", agent_id=None,
            reason="parent_reopened_for_new_subtask",
        )
        await s.commit()

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        assert len(comments) == 1
        assert "parent_reopened_for_new_subtask" in comments[0].content
