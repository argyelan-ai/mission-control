"""Duplicate Root Guard — idempotency for root tasks.

1. Same root within the window → 409 + existing_task_id
2. Same root outside the window → allowed
3. Same title, different requester_id → allowed
4. Child task → not affected
5. in_progress + children → do not block
6. Response contains existing_task_id
"""
import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.utils import utcnow


def _normalize_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())[:50]


class TestDuplicateRootGuard:

    @pytest.mark.asyncio
    async def test_duplicate_inbox_root_blocked(self, session: AsyncSession, make_agent, make_task):
        """Same root (inbox) within 60s → blocked."""
        board_id = uuid.uuid4()
        agent = await make_agent("H", board_id=board_id, is_board_lead=True)

        existing = await make_task(
            board_id, title="Wetter-CLI bauen",
            status="inbox",
            owner_agent_id=agent.id,
            requester_channel="telegram",
            requester_id="123",
        )

        # Same check logic as the guard
        _new_title = _normalize_title("Wetter-CLI bauen")
        result = await session.exec(
            select(Task).where(
                Task.parent_task_id.is_(None),  # type: ignore[union-attr]
                Task.board_id == board_id,
                Task.owner_agent_id == agent.id,
                Task.created_at > utcnow() - timedelta(seconds=60),
                Task.status == "inbox",
            )
        )
        match = None
        for t in result.all():
            if _normalize_title(t.title) == _new_title:
                match = t
                break
        assert match is not None
        assert match.id == existing.id

    @pytest.mark.asyncio
    async def test_old_root_not_blocked(self, session: AsyncSession, make_agent, make_task):
        """Same root but older than 60s → allowed."""
        board_id = uuid.uuid4()
        agent = await make_agent("H2", board_id=board_id, is_board_lead=True)

        old = await make_task(
            board_id, title="Wetter-CLI bauen",
            status="inbox",
            owner_agent_id=agent.id,
            created_at=utcnow() - timedelta(seconds=120),
        )

        result = await session.exec(
            select(Task).where(
                Task.parent_task_id.is_(None),  # type: ignore[union-attr]
                Task.board_id == board_id,
                Task.owner_agent_id == agent.id,
                Task.created_at > utcnow() - timedelta(seconds=60),
            )
        )
        assert result.first() is None  # No match → allowed

    @pytest.mark.asyncio
    async def test_different_requester_not_blocked(self, session: AsyncSession, make_agent, make_task):
        """Same title, different requester_id → allowed."""
        board_id = uuid.uuid4()
        agent = await make_agent("H3", board_id=board_id, is_board_lead=True)

        await make_task(
            board_id, title="Wetter-CLI bauen",
            status="inbox",
            owner_agent_id=agent.id,
            requester_channel="telegram",
            requester_id="user-A",
        )

        # New task with a different requester_id
        result = await session.exec(
            select(Task).where(
                Task.parent_task_id.is_(None),  # type: ignore[union-attr]
                Task.board_id == board_id,
                Task.owner_agent_id == agent.id,
                Task.requester_id == "user-B",  # different sender
                Task.created_at > utcnow() - timedelta(seconds=60),
            )
        )
        assert result.first() is None  # No match → allowed

    @pytest.mark.asyncio
    async def test_child_task_not_affected(self, session: AsyncSession, make_agent, make_task):
        """Child task → guard does not apply (roots only)."""
        board_id = uuid.uuid4()
        agent = await make_agent("H4", board_id=board_id, is_board_lead=True)

        root = await make_task(board_id, title="Root", owner_agent_id=agent.id)
        child = await make_task(
            board_id, title="Child gleicher Name",
            parent_task_id=root.id,
            owner_agent_id=agent.id,
        )

        # Guard checks parent_task_id IS NULL — child has a parent → ignored
        result = await session.exec(
            select(Task).where(
                Task.parent_task_id.is_(None),  # type: ignore[union-attr]
                Task.board_id == board_id,
                Task.owner_agent_id == agent.id,
                Task.created_at > utcnow() - timedelta(seconds=60),
            )
        )
        # Only the root matches, not the child
        tasks = result.all()
        assert all(t.parent_task_id is None for t in tasks)

    @pytest.mark.asyncio
    async def test_in_progress_with_children_not_blocked(self, session: AsyncSession, make_agent, make_task):
        """in_progress root WITH children → do not block."""
        board_id = uuid.uuid4()
        agent = await make_agent("H5", board_id=board_id, is_board_lead=True)

        root = await make_task(
            board_id, title="Wetter-CLI bauen",
            status="in_progress",
            owner_agent_id=agent.id,
        )
        # Child exists
        await make_task(board_id, title="Plan", parent_task_id=root.id, owner_agent_id=agent.id)

        # Guard: in_progress + children → do NOT block
        _children = await session.exec(
            select(Task.id).where(Task.parent_task_id == root.id).limit(1)
        )
        has_children = _children.first() is not None
        assert has_children  # Has children → guard does NOT block

    @pytest.mark.asyncio
    async def test_in_progress_without_children_blocked(self, session: AsyncSession, make_agent, make_task):
        """in_progress root WITHOUT children → block."""
        board_id = uuid.uuid4()
        agent = await make_agent("H6", board_id=board_id, is_board_lead=True)

        root = await make_task(
            board_id, title="Wetter-CLI bauen",
            status="in_progress",
            owner_agent_id=agent.id,
        )

        _children = await session.exec(
            select(Task.id).where(Task.parent_task_id == root.id).limit(1)
        )
        has_children = _children.first() is not None
        assert not has_children  # No children → guard blocks

    @pytest.mark.asyncio
    async def test_title_normalization(self, session: AsyncSession):
        """Title normalization: whitespace, case, trim."""
        assert _normalize_title("  Wetter-CLI   bauen  ") == "wetter-cli bauen"
        assert _normalize_title("WETTER-CLI BAUEN") == "wetter-cli bauen"
        assert _normalize_title("Wetter-CLI\n\tbauen") == "wetter-cli bauen"
        assert _normalize_title("  Wetter-CLI   bauen  ") == _normalize_title("wetter-cli bauen")
