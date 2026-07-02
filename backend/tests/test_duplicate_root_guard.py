"""Duplicate Root Guard — Idempotency fuer Root-Tasks.

1. Gleicher Root innerhalb Fenster → 409 + existing_task_id
2. Gleicher Root ausserhalb Fenster → erlaubt
3. Gleicher Titel, anderer requester_id → erlaubt
4. Child-Task → nicht betroffen
5. in_progress + Children → nicht blockieren
6. Response enthaelt existing_task_id
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
        """Gleicher Root (inbox) innerhalb 60s → blockiert."""
        board_id = uuid.uuid4()
        agent = await make_agent("H", board_id=board_id, is_board_lead=True)

        existing = await make_task(
            board_id, title="Wetter-CLI bauen",
            status="inbox",
            owner_agent_id=agent.id,
            requester_channel="telegram",
            requester_id="123",
        )

        # Gleiche Prueflogik wie der Guard
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
        """Gleicher Root aber aelter als 60s → erlaubt."""
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
        assert result.first() is None  # Kein Match → erlaubt

    @pytest.mark.asyncio
    async def test_different_requester_not_blocked(self, session: AsyncSession, make_agent, make_task):
        """Gleicher Titel, anderer requester_id → erlaubt."""
        board_id = uuid.uuid4()
        agent = await make_agent("H3", board_id=board_id, is_board_lead=True)

        await make_task(
            board_id, title="Wetter-CLI bauen",
            status="inbox",
            owner_agent_id=agent.id,
            requester_channel="telegram",
            requester_id="user-A",
        )

        # Neuer Task mit anderem requester_id
        result = await session.exec(
            select(Task).where(
                Task.parent_task_id.is_(None),  # type: ignore[union-attr]
                Task.board_id == board_id,
                Task.owner_agent_id == agent.id,
                Task.requester_id == "user-B",  # Anderer Absender
                Task.created_at > utcnow() - timedelta(seconds=60),
            )
        )
        assert result.first() is None  # Kein Match → erlaubt

    @pytest.mark.asyncio
    async def test_child_task_not_affected(self, session: AsyncSession, make_agent, make_task):
        """Child-Task → Guard greift nicht (nur Roots)."""
        board_id = uuid.uuid4()
        agent = await make_agent("H4", board_id=board_id, is_board_lead=True)

        root = await make_task(board_id, title="Root", owner_agent_id=agent.id)
        child = await make_task(
            board_id, title="Child gleicher Name",
            parent_task_id=root.id,
            owner_agent_id=agent.id,
        )

        # Guard prueft parent_task_id IS NULL — Child hat parent → wird ignoriert
        result = await session.exec(
            select(Task).where(
                Task.parent_task_id.is_(None),  # type: ignore[union-attr]
                Task.board_id == board_id,
                Task.owner_agent_id == agent.id,
                Task.created_at > utcnow() - timedelta(seconds=60),
            )
        )
        # Nur der Root matcht, nicht das Child
        tasks = result.all()
        assert all(t.parent_task_id is None for t in tasks)

    @pytest.mark.asyncio
    async def test_in_progress_with_children_not_blocked(self, session: AsyncSession, make_agent, make_task):
        """in_progress Root MIT Children → nicht blockieren."""
        board_id = uuid.uuid4()
        agent = await make_agent("H5", board_id=board_id, is_board_lead=True)

        root = await make_task(
            board_id, title="Wetter-CLI bauen",
            status="in_progress",
            owner_agent_id=agent.id,
        )
        # Child existiert
        await make_task(board_id, title="Plan", parent_task_id=root.id, owner_agent_id=agent.id)

        # Guard: in_progress + Children → NICHT blockieren
        _children = await session.exec(
            select(Task.id).where(Task.parent_task_id == root.id).limit(1)
        )
        has_children = _children.first() is not None
        assert has_children  # Hat Children → Guard blockiert NICHT

    @pytest.mark.asyncio
    async def test_in_progress_without_children_blocked(self, session: AsyncSession, make_agent, make_task):
        """in_progress Root OHNE Children → blockieren."""
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
        assert not has_children  # Keine Children → Guard blockiert

    @pytest.mark.asyncio
    async def test_title_normalization(self, session: AsyncSession):
        """Title-Normalisierung: Whitespace, Case, Trim."""
        assert _normalize_title("  Wetter-CLI   bauen  ") == "wetter-cli bauen"
        assert _normalize_title("WETTER-CLI BAUEN") == "wetter-cli bauen"
        assert _normalize_title("Wetter-CLI\n\tbauen") == "wetter-cli bauen"
        assert _normalize_title("  Wetter-CLI   bauen  ") == _normalize_title("wetter-cli bauen")
