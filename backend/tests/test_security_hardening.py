"""Tests for security hardening: ownership, board check, comment type, dispatch lock, rejection counter."""
import uuid
from unittest.mock import patch

import pytest

from app.services.task_queue import (
    acquire_dispatch_lock,
    release_dispatch_lock,
    increment_rejection_count,
    get_rejection_count,
    MAX_REJECTIONS,
)
from app.routers.agent_scoped import VALID_COMMENT_TYPES, AgentCommentCreate


# ── Comment-Type Validation Tests ───────────────────────────────────


def test_valid_comment_types():
    """All expected types are defined (incl. reflection + waiting_on_callback since 2026-04-11,
    phase approval workflow types since 2026-04-13, install callback types since 2026-04-19)."""
    assert VALID_COMMENT_TYPES == {
        "message", "handoff", "blocker", "progress", "resolution", "feedback", "checkpoint",
        "report_back", "reflection", "waiting_on_callback",
        # Phase Approval Workflow (2026-04-13)
        "subtask_completed", "phase_approved", "phase_rewrite_request",
        # Install-Approval Callback (2026-04-19)
        "install_completed", "install_failed",
        # Lead-first Blocker-Triage (2026-07-04, Fix A)
        "escalate_to_operator",
    }


def test_comment_create_valid():
    c = AgentCommentCreate(content="test", comment_type="progress")
    assert c.comment_type == "progress"


def test_comment_create_default():
    c = AgentCommentCreate(content="test")
    assert c.comment_type == "message"


def test_comment_create_invalid():
    with pytest.raises(Exception):
        AgentCommentCreate(content="test", comment_type="hack")


def test_comment_create_checkpoint():
    c = AgentCommentCreate(content="- [x] step 1", comment_type="checkpoint")
    assert c.comment_type == "checkpoint"


# ── Dispatch Lock Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_lock_acquire_release(fake_redis):
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        agent_id = str(uuid.uuid4())

        # Acquire lock
        assert await acquire_dispatch_lock(agent_id, ttl=30) is True

        # Second acquire fails
        assert await acquire_dispatch_lock(agent_id, ttl=30) is False

        # Release + acquire again
        await release_dispatch_lock(agent_id)
        assert await acquire_dispatch_lock(agent_id, ttl=30) is True


@pytest.mark.asyncio
async def test_dispatch_lock_different_agents(fake_redis):
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        agent1 = str(uuid.uuid4())
        agent2 = str(uuid.uuid4())

        # Two different agents can hold locks at the same time
        assert await acquire_dispatch_lock(agent1) is True
        assert await acquire_dispatch_lock(agent2) is True


# ── Rejection Counter Tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejection_counter_increment(fake_redis):
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        task_id = str(uuid.uuid4())

        count = await increment_rejection_count(task_id)
        assert count == 1

        count = await increment_rejection_count(task_id)
        assert count == 2

        count = await increment_rejection_count(task_id)
        assert count == 3


@pytest.mark.asyncio
async def test_rejection_counter_get(fake_redis):
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        task_id = str(uuid.uuid4())

        assert await get_rejection_count(task_id) == 0

        await increment_rejection_count(task_id)
        assert await get_rejection_count(task_id) == 1


@pytest.mark.asyncio
async def test_max_rejections_constant():
    assert MAX_REJECTIONS == 10
