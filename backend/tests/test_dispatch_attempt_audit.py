"""Tests for app.services.dispatch_attempt_audit.

Verifies the helper that owns every write to tasks.dispatch_attempt_id:
- set: writes attempt_id + inserts audit row
- clear: clears attempt_id + inserts audit row (skips if already None)
- set(only_if_null=True): race-frei first-writer-wins, loser sees False
- audit row contains caller + reason + old/new transition
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.board import Board
from app.models.task import Task
from app.models.task_attempt_audit import TaskAttemptAudit
from app.services.dispatch_attempt_audit import (
    clear_dispatch_attempt_id,
    set_dispatch_attempt_id,
)


@pytest.fixture
async def board(async_session) -> Board:
    b = Board(name="test", slug="t", group_id=None)
    async_session.add(b)
    await async_session.commit()
    await async_session.refresh(b)
    return b


@pytest.fixture
async def task(async_session, board: Board) -> Task:
    t = Task(title="audit-test", status="inbox", board_id=board.id)
    async_session.add(t)
    await async_session.commit()
    await async_session.refresh(t)
    return t


async def _audit_rows(session, task_id: uuid.UUID) -> list[TaskAttemptAudit]:
    result = await session.exec(
        select(TaskAttemptAudit)
        .where(TaskAttemptAudit.task_id == task_id)
        .order_by(TaskAttemptAudit.created_at)
    )
    return list(result.scalars().all())


# ── set ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_writes_attempt_id_and_audit_row(async_session, task: Task):
    new_id = str(uuid.uuid4())
    ok = await set_dispatch_attempt_id(
        async_session, task, new_id, caller="test", reason="initial",
    )
    assert ok is True
    assert task.dispatch_attempt_id == new_id

    audits = await _audit_rows(async_session, task.id)
    assert len(audits) == 1
    assert audits[0].old_attempt is None
    assert str(audits[0].new_attempt) == new_id
    assert audits[0].caller == "test"
    assert audits[0].reason == "initial"


@pytest.mark.asyncio
async def test_set_replaces_existing_id_when_unconditional(async_session, task: Task):
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    await set_dispatch_attempt_id(async_session, task, first, caller="test", reason="first")
    await set_dispatch_attempt_id(async_session, task, second, caller="test", reason="rotate")
    assert task.dispatch_attempt_id == second

    audits = await _audit_rows(async_session, task.id)
    assert len(audits) == 2
    assert str(audits[1].old_attempt) == first
    assert str(audits[1].new_attempt) == second


# ── set(only_if_null) — Race-Safe ────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_only_if_null_skips_when_already_set(async_session, task: Task):
    existing = str(uuid.uuid4())
    await set_dispatch_attempt_id(async_session, task, existing, caller="test", reason="seed")

    loser = str(uuid.uuid4())
    ok = await set_dispatch_attempt_id(
        async_session, task, loser,
        caller="test", reason="race_loser", only_if_null=True,
    )
    assert ok is False
    # ORM was refreshed to canonical (existing) value
    assert task.dispatch_attempt_id == existing

    audits = await _audit_rows(async_session, task.id)
    # Only the original seed row — no audit for the lost race
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_set_only_if_null_writes_when_initially_null(async_session, task: Task):
    new_id = str(uuid.uuid4())
    ok = await set_dispatch_attempt_id(
        async_session, task, new_id,
        caller="test", reason="initial_race_winner", only_if_null=True,
    )
    assert ok is True
    assert task.dispatch_attempt_id == new_id

    audits = await _audit_rows(async_session, task.id)
    assert len(audits) == 1
    assert audits[0].caller == "test"
    assert audits[0].reason == "initial_race_winner"


# ── clear ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_writes_audit_row_on_transition(async_session, task: Task):
    existing = str(uuid.uuid4())
    await set_dispatch_attempt_id(async_session, task, existing, caller="test", reason="seed")

    await clear_dispatch_attempt_id(
        async_session, task, caller="test", reason="user_stop",
    )
    assert task.dispatch_attempt_id is None

    audits = await _audit_rows(async_session, task.id)
    assert len(audits) == 2
    assert str(audits[1].old_attempt) == existing
    assert audits[1].new_attempt is None
    assert audits[1].caller == "test"
    assert audits[1].reason == "user_stop"


@pytest.mark.asyncio
async def test_clear_is_noop_when_already_null(async_session, task: Task):
    # Task starts with dispatch_attempt_id=None
    assert task.dispatch_attempt_id is None
    await clear_dispatch_attempt_id(
        async_session, task, caller="test", reason="no_transition",
    )
    audits = await _audit_rows(async_session, task.id)
    assert audits == []
