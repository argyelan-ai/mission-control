"""build_agent_task_prompt liefert Recovery-Context bei re-dispatch.

Wenn ein Task bereits Kommentare (checkpoint/progress) oder Checklist-Items hat,
muss der Prompt den Recovery-Block enthalten damit der Agent fortsetzt statt
neu anzufangen. Bei frischem Task: kein Recovery-Block.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.task import Task, TaskComment
from app.utils import utcnow


@pytest.mark.asyncio
async def test_fresh_task_has_no_recovery_section(async_session, board_with_agents):
    """Frischer Task ohne History → kein Recovery-Header."""
    from app.services.dispatch import build_agent_task_prompt

    board = board_with_agents["board"]
    developer = board_with_agents["developer"]

    task = Task(
        board_id=board.id, title="Frische Aufgabe", status="in_progress",
        assigned_agent_id=developer.id, description="Build feature X",
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    with patch("app.services.dispatch.emit_event", new_callable=AsyncMock):
        prompt = await build_agent_task_prompt(task, developer, async_session)

    assert "## Recovery — Du hast hier aufgehoert" not in prompt


@pytest.mark.asyncio
async def test_task_with_progress_comment_includes_recovery(async_session, board_with_agents):
    """Task mit progress-comment → Recovery-Header + fortsetzen-Mandat im Prompt."""
    from app.services.dispatch import build_agent_task_prompt

    board = board_with_agents["board"]
    developer = board_with_agents["developer"]

    task = Task(
        board_id=board.id, title="Laufender Task", status="in_progress",
        assigned_agent_id=developer.id,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    # Simuliere vorherigen Progress
    c = TaskComment(
        task_id=task.id,
        author_type="agent",
        author_agent_id=developer.id,
        content="**Update** — Schritt 1+2 erledigt, Schritt 3 laeuft",
        comment_type="progress",
    )
    async_session.add(c)
    await async_session.commit()

    with patch("app.services.dispatch.emit_event", new_callable=AsyncMock):
        prompt = await build_agent_task_prompt(task, developer, async_session)

    assert "## Recovery — Du hast hier aufgehoert" in prompt
    assert "NICHT neu an" in prompt
    assert "Schritt 1+2 erledigt" in prompt  # vorheriger progress-content


@pytest.mark.asyncio
async def test_task_with_only_checklist_includes_recovery(async_session, board_with_agents):
    """Task ohne Comments aber mit Checklist-Items → Recovery-Header (Agent soll
    Checkliste nicht neu erstellen)."""
    from app.services.dispatch import build_agent_task_prompt
    from app.models.checklist import TaskChecklistItem

    board = board_with_agents["board"]
    developer = board_with_agents["developer"]

    task = Task(
        board_id=board.id, title="Task mit Checkliste", status="in_progress",
        assigned_agent_id=developer.id,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    items = [
        TaskChecklistItem(task_id=task.id, title="Schritt 1", status="done", sort_order=0),
        TaskChecklistItem(task_id=task.id, title="Schritt 2", status="pending", sort_order=1),
    ]
    for i in items:
        async_session.add(i)
    await async_session.commit()

    with patch("app.services.dispatch.emit_event", new_callable=AsyncMock):
        prompt = await build_agent_task_prompt(task, developer, async_session)

    assert "## Recovery — Du hast hier aufgehoert" in prompt
    assert "← **HIER WEITERMACHEN**" in prompt
    assert "Schritt 2" in prompt


@pytest.mark.asyncio
async def test_recovery_context_shows_progress_comments(async_session, board_with_agents):
    """Progress-comments erscheinen unter 'Letzter Fortschritt' (Workstream A4:
    checkpoint-comments wurden zu progress migriert via Migration 0082)."""
    from app.services.dispatch import build_recovery_context

    board = board_with_agents["board"]
    developer = board_with_agents["developer"]

    task = Task(
        board_id=board.id, title="Task", status="in_progress",
        assigned_agent_id=developer.id,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    c = TaskComment(
        task_id=task.id,
        author_type="agent",
        author_agent_id=developer.id,
        content="Schritt 1 fertig, Schritt 2 in Arbeit",
        comment_type="progress",
    )
    async_session.add(c)
    await async_session.commit()

    ctx = await build_recovery_context(async_session, task)
    assert ctx is not None
    assert "Schritt 1 fertig" in ctx
    assert "[progress" in ctx
