"""Bug 17 — Stale-Check Auto-Promote darf nur auf Agent-Resolutions feuern.

`comment_type="resolution"` ist polysem in der Codebase:
- `agent_comments.py` schreibt es bei Agent-Fertig-Meldungen (`author_type="agent"`)
- `approvals.py` schreibt es bei User-Klaerungs-Antworten / Blocker-Resolves
  (`author_type="user"`)

Vor dem Fix wertete `task_runner._check_stale_in_progress` nur `comment_type`
aus und promotete deshalb auch User-Antworten faelschlich auf `review`.
Live-Bug 2026-05-13 ~22:00: Task `c9fbe9cb` (Voice-Foundation) ging auf
review nachdem eine clarification-resolve geschrieben wurde, obwohl Sparky
die Arbeit noch nicht fertig hatte.

Zwei Tests:
1. User-Resolution darf NICHT auto-promoten (Regression-Schutz).
2. Agent-Resolution muss weiterhin auto-promoten (Inverse-Schutz, damit der
   Phase-8 BUG-01 Safety-Net Path B nicht versehentlich kaputtgeht).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_test_data(session, *, auto_promote: bool = True):
    """Board + Worker-Agent + in_progress Task. Comment wird vom Test
    angelegt, damit author_type pro Test variieren kann."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    board = Board(id=board_id, name="Test Board", slug=f"test-{board_id.hex[:8]}")
    session.add(board)

    _raw, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="TestWorker",
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=False,
        role=None,
        scopes=["tasks:read", "tasks:write", "tasks:create"],
        auto_promote_on_resolution=auto_promote,
    )
    session.add(agent)

    task = Task(
        id=task_id,
        board_id=board_id,
        title="Bug 17 regression task",
        status="in_progress",
        assigned_agent_id=agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)

    return board, agent, task


@pytest.mark.asyncio
async def test_user_resolution_does_not_trigger_stale_promote(fake_redis):
    """Bug 17 regression: User-Klaerungs-Antworten (approvals.py setzt
    `author_type="user"`, `comment_type="resolution"`) duerfen den Stale-
    Check Auto-Promote NICHT triggern, auch wenn `auto_promote_on_resolution`
    aktiv ist."""
    from app.models.task import Task, TaskComment
    from app.services.task_runner import TaskRunnerService
    from app.utils import utcnow

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _board, _agent, task = await _create_test_data(s, auto_promote=True)
        # Simuliere clarification-resolve: User-Antwort als resolution-Comment.
        # Identisch zu approvals.py:355-365.
        s.add(TaskComment(
            task_id=task.id,
            author_type="user",
            content="**Antwort auf deine Klaerungsfrage** (vom Operator): mach so weiter.",
            comment_type="resolution",
            created_at=utcnow(),
        ))
        await s.commit()

    runner = TaskRunnerService()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.emit_event", new_callable=AsyncMock):
            await runner._check_stale_in_progress(s)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert updated.status == "in_progress", (
            f"Bug 17 regression: User-Resolution-Comment promoted task to "
            f"{updated.status}; expected in_progress (User-Klaerungs-"
            f"Antworten sind keine Agent-Fertig-Meldungen)."
        )


@pytest.mark.asyncio
async def test_agent_resolution_still_triggers_stale_promote(fake_redis):
    """Inverse-Schutz: Agent-Resolution-Comments triggern den Phase-8 BUG-01
    Safety-Net Path B weiterhin (Stale-Check Auto-Promote)."""
    from app.models.task import Task, TaskComment
    from app.services.task_runner import TaskRunnerService
    from app.utils import utcnow

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _board, agent, task = await _create_test_data(s, auto_promote=True)
        s.add(TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=agent.id,
            content="Fertig, alle Tests gruen.",
            comment_type="resolution",
            created_at=utcnow(),
        ))
        await s.commit()

    runner = TaskRunnerService()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.emit_event", new_callable=AsyncMock):
            await runner._check_stale_in_progress(s)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert updated.status == "review", (
            f"Inverse-Regression: Agent-Resolution-Comment hat NICHT promotet; "
            f"got {updated.status}; expected review (Phase-8 BUG-01 Safety-Net "
            f"Path B muss intakt bleiben)."
        )
