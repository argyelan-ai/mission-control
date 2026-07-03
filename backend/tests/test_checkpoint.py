"""Bundle 1 — Checkpoint / Crash Recovery Tests.

Covers:
1. Write + read checkpoint (DB)
2. Latest checkpoint correctly selected
3. Recovery context includes checkpoint
4. Recovery without checkpoint stays intact
5. No secrets in checkpoint
"""
import uuid

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.checkpoint import TaskCheckpoint


# ── Model + DB ───────────────────────────────────────────────────────────

class TestCheckpointModel:

    @pytest.mark.asyncio
    async def test_create_and_read_checkpoint(self, session: AsyncSession, make_agent, make_task):
        """Checkpoint can be created and read."""
        board_id = uuid.uuid4()
        agent = await make_agent("TestAgent", board_id=board_id, role="developer")
        task = await make_task(board_id, title="Test Task", assigned_agent_id=agent.id)

        cp = TaskCheckpoint(
            task_id=task.id,
            agent_id=agent.id,
            checkpoint_type="manual",
            state_summary="Backend-Endpoints implementiert, Frontend fehlt",
            context_data={
                "erledigte_schritte": ["models.py", "migration", "routers/tasks.py"],
                "naechste_schritte": ["frontend types", "TaskDetailPanel"],
                "branch": "feature/intake-fields",
            },
        )
        session.add(cp)
        await session.commit()
        await session.refresh(cp)

        assert cp.id is not None
        assert cp.state_summary == "Backend-Endpoints implementiert, Frontend fehlt"
        assert cp.context_data["branch"] == "feature/intake-fields"
        assert len(cp.context_data["erledigte_schritte"]) == 3

    @pytest.mark.asyncio
    async def test_latest_checkpoint_selected(self, session: AsyncSession, make_agent, make_task):
        """With multiple checkpoints, the newest one is returned."""
        from datetime import datetime, timedelta

        board_id = uuid.uuid4()
        agent = await make_agent("TestAgent2", board_id=board_id, role="developer")
        task = await make_task(board_id, title="Multi-CP Task", assigned_agent_id=agent.id)

        now = datetime.utcnow()
        cp1 = TaskCheckpoint(
            task_id=task.id, agent_id=agent.id,
            state_summary="Schritt 1 erledigt",
            created_at=now - timedelta(minutes=10),
        )
        session.add(cp1)

        cp2 = TaskCheckpoint(
            task_id=task.id, agent_id=agent.id,
            state_summary="Schritt 1+2 erledigt, Tests gruen",
            created_at=now,
        )
        session.add(cp2)
        await session.commit()

        result = await session.exec(
            select(TaskCheckpoint)
            .where(TaskCheckpoint.task_id == task.id)
            .order_by(TaskCheckpoint.created_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
        latest = result.first()
        assert latest is not None
        assert latest.state_summary == "Schritt 1+2 erledigt, Tests gruen"

    @pytest.mark.asyncio
    async def test_checkpoint_without_context_data(self, session: AsyncSession, make_agent, make_task):
        """Checkpoint without context_data is valid."""
        board_id = uuid.uuid4()
        agent = await make_agent("MinimalAgent", board_id=board_id, role="developer")
        task = await make_task(board_id, title="Minimal CP", assigned_agent_id=agent.id)

        cp = TaskCheckpoint(
            task_id=task.id, agent_id=agent.id,
            state_summary="Nur ein kurzer Stand",
        )
        session.add(cp)
        await session.commit()
        await session.refresh(cp)

        assert cp.context_data is None
        assert cp.checkpoint_type == "manual"


# ── Recovery Integration ─────────────────────────────────────────────────

class TestCheckpointRecovery:

    # Recovery-from-TaskCheckpoint tests removed in Workstream A4 — recovery
    # now reads from TaskChecklistItem + progress comments only. The
    # TaskCheckpoint rows remain as read-only archive; migration 0082 moved
    # prior checkpoint-type comments into `progress`. Checklist-based
    # recovery is covered by test_build_agent_task_prompt_recovery.py.

    @pytest.mark.asyncio
    async def test_recovery_without_checkpoint_still_works(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Recovery without checkpoint still works (comments only)."""
        from app.services.dispatch import build_recovery_context
        from app.models.task import TaskComment

        board_id = uuid.uuid4()
        agent = await make_agent(
            "NoCPRecov", board_id=board_id, role="developer"
        )
        task = await make_task(
            board_id, title="No CP Recovery", assigned_agent_id=agent.id, status="in_progress"
        )

        comment = TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=agent.id,
            comment_type="progress", content="Habe angefangen",
        )
        session.add(comment)
        await session.commit()

        ctx = await build_recovery_context(session, task)
        assert ctx is not None
        assert "Habe angefangen" in ctx
        assert "Letzter Checkpoint" not in ctx

    @pytest.mark.asyncio
    async def test_recovery_no_comments_no_checkpoint_returns_none(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Neither comments nor checkpoint → None."""
        from app.services.dispatch import build_recovery_context

        board_id = uuid.uuid4()
        agent = await make_agent(
            "EmptyRecov", board_id=board_id, role="developer"
        )
        task = await make_task(board_id, title="Empty Recovery", assigned_agent_id=agent.id)

        ctx = await build_recovery_context(session, task)
        assert ctx is None


# ── Security ─────────────────────────────────────────────────────────────

class TestCheckpointSecurity:

    def test_secrets_blocked_in_context_data(self):
        """context_data with obvious secrets → validation error."""
        from app.routers.agent_scoped import CheckpointCreate

        with pytest.raises(Exception):
            CheckpointCreate(
                state_summary="Test",
                context_data={"api_key": "bearer sk-1234567890abcdef"},
            )

    def test_clean_context_data_allowed(self):
        """Normal context_data without secrets → OK."""
        from app.routers.agent_scoped import CheckpointCreate

        cp = CheckpointCreate(
            state_summary="Alles gut",
            context_data={
                "erledigte_schritte": ["models.py", "tests"],
                "branch": "feature/xyz",
            },
        )
        assert cp.context_data is not None

    def test_empty_summary_rejected(self):
        """Empty state_summary → validation error."""
        from app.routers.agent_scoped import CheckpointCreate

        with pytest.raises(Exception):
            CheckpointCreate(state_summary="   ")

    def test_long_summary_rejected(self):
        """state_summary > 2000 characters → validation error."""
        from app.routers.agent_scoped import CheckpointCreate

        with pytest.raises(Exception):
            CheckpointCreate(state_summary="x" * 2001)
