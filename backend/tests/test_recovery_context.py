"""
Tests for build_recovery_context() — rich recovery context from task comments.

TDD: tests first, implementation after.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment


# ── Helper function: create comment ─────────────────────────────────


async def _create_comment(
    session: AsyncSession,
    task_id: uuid.UUID,
    comment_type: str = "progress",
    content: str = "Test comment",
    created_at: datetime | None = None,
    author_type: str = "agent",
) -> TaskComment:
    """Create a TaskComment in the DB and return it."""
    comment = TaskComment(
        id=uuid.uuid4(),
        task_id=task_id,
        author_type=author_type,
        comment_type=comment_type,
        content=content,
        created_at=created_at or datetime.utcnow(),
    )
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return comment


# ── Helper function: board + task setup ──────────────────────────────────


async def _setup_board_and_task(
    session: AsyncSession,
    assigned_agent_id: uuid.UUID | None = None,
) -> Task:
    """Create board + task and return the task."""
    board = Board(id=uuid.uuid4(), name="Test Board", slug=f"test-{uuid.uuid4().hex[:8]}")
    session.add(board)
    await session.commit()

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Recovery Test Task",
        assigned_agent_id=assigned_agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_context_returns_none_without_comments(session: AsyncSession):
    """Without comments → return None."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    result = await build_recovery_context(session, task)
    assert result is None


@pytest.mark.asyncio
async def test_recovery_context_includes_progress_comments(session: AsyncSession):
    """Progress comments appear under 'Latest Progress'.

    Workstream A4: `checkpoint` comments no longer exist — migration 0082
    moved them into `progress`, and new code posts `progress` via
    `mc comment progress`.
    """
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    await _create_comment(session, task.id, "progress", "Schritt 1 erledigt", now - timedelta(minutes=30))
    await _create_comment(session, task.id, "progress", "Models erstellt", now - timedelta(minutes=20))
    await _create_comment(session, task.id, "progress", "Tests geschrieben", now - timedelta(minutes=10))

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Recovery" in result
    assert "Schritt 1 erledigt" in result
    assert "Models erstellt" in result
    assert "Tests geschrieben" in result
    assert "progress" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_blocker(session: AsyncSession):
    """Blocker comment is shown with the BLOCKER label."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "blocker", "Warte auf API-Key")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "BLOCKER" in result
    assert "Warte auf API-Key" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_feedback(session: AsyncSession):
    """Reviewer feedback is shown with the REVIEWER-FEEDBACK label."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "feedback", "Tests fehlen fuer Edge-Cases")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "REVIEWER-FEEDBACK" in result
    assert "Tests fehlen fuer Edge-Cases" in result


@pytest.mark.asyncio
async def test_recovery_context_limits_to_5_comments(session: AsyncSession):
    """10 comments → only the newest 5 appear (Workstream A4 cap)."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    for i in range(10):
        prefix = "OLD" if i < 5 else "NEW"
        await _create_comment(
            session,
            task.id,
            "progress",
            f"Fortschritt {prefix}-{i:02d}",
            now - timedelta(minutes=10 - i),
        )

    result = await build_recovery_context(session, task)

    assert result is not None
    # The oldest 5 (OLD-00 through OLD-04) should NOT be included
    for i in range(5):
        assert f"Fortschritt OLD-{i:02d}" not in result
    # The newest 5 (NEW-05 through NEW-09) should be included
    for i in range(5, 10):
        assert f"Fortschritt NEW-{i:02d}" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_workspace_info(session: AsyncSession):
    """Agent with workspace_path → path in the result."""
    from app.services.dispatch import build_recovery_context

    agent = Agent(
        id=uuid.uuid4(),
        name="Cody",
        workspace_path="/home/henry/.openclaw/workspace-cody",
    )
    session.add(agent)
    await session.commit()

    task = await _setup_board_and_task(session, assigned_agent_id=agent.id)
    await _create_comment(session, task.id, "progress", "Arbeite am Feature")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "/home/henry/.openclaw/workspace-cody" in result
    assert "Workspace" in result


@pytest.mark.asyncio
async def test_recovery_context_ignores_message_type(session: AsyncSession):
    """Comments of type 'message' are NOT included."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "message", "Hallo, wie geht's?")

    result = await build_recovery_context(session, task)

    # Only message comments → no recovery context
    assert result is None


@pytest.mark.asyncio
async def test_recovery_context_includes_sixth_operator_comment(session: AsyncSession):
    """W0.3: 6 Operator-Kommentare vor einem Requeue -> der 6. (neueste) steht im Prompt.

    Vorher rot gegen die alte Funktion verifiziert (relevant_types kannte weder
    `message` noch `handoff` -> alle 6 fielen komplett raus, `result is None`).
    Prueft nebenbei auch die Postfach-Hinweiszeile (DoD-Punkt 1): von 6
    Operator-Kommentaren werden nur die letzten 3 ungekuerzt gezeigt, die
    Hinweiszeile muss die echte Zahl der uebrigen 3 nennen.
    """
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    for i in range(1, 7):
        await _create_comment(
            session,
            task.id,
            "message",
            f"Anweisung {i}: mach X statt Y",
            now - timedelta(minutes=60 - i),
            author_type="user",
        )

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Anweisung 6: mach X statt Y" in result
    # Postfach-Hinweis: 6 Operator-Kommentare insgesamt, nur 3 gezeigt -> 3 fehlen.
    assert "3" in result
    assert "mc task-get" in result
    assert str(task.id) in result


@pytest.mark.asyncio
async def test_recovery_context_operator_comment_full_multiline_content(session: AsyncSession):
    """Ein mehrzeiliger Operator-Kommentar kommt vollstaendig an, nicht nur Zeile 1."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    multiline = (
        "Bitte zuerst die Migration pruefen.\n"
        "Danach den Endpunkt gegen den neuen Vertrag testen.\n"
        "Erst wenn beides gruen ist: PR aufmachen."
    )
    await _create_comment(session, task.id, "message", multiline, author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert multiline in result


@pytest.mark.asyncio
async def test_recovery_context_operator_block_cap_drops_oldest(session: AsyncSession):
    """Operator-Block > 1500 Zeichen -> die aelteste Anweisung fliegt raus, mit Hinweis."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    oldest = "A" * 600
    middle = "B" * 600
    newest = "C" * 600
    await _create_comment(session, task.id, "message", oldest, now - timedelta(minutes=30), author_type="user")
    await _create_comment(session, task.id, "handoff", middle, now - timedelta(minutes=20), author_type="agent")
    await _create_comment(session, task.id, "message", newest, now - timedelta(minutes=10), author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert oldest not in result
    assert middle in result
    assert newest in result
    # Hinweis, dass wegen des Caps etwas weggelassen wurde.
    assert "weggelassen" in result or "Cap" in result


@pytest.mark.asyncio
async def test_recovery_context_excludes_system_generated_message_and_handoff(session: AsyncSession):
    """Automatisch erzeugte system-Kommentare (author_type='system') sind keine
    Anweisungen und duerfen nicht im Operator-/Lead-Block auftauchen — selbst
    wenn ihr comment_type zufaellig 'message' oder 'handoff' ist (z.B. der
    System-Handoff beim Human-Review-Uebergang, oder der System-Message-
    Callback bei Subtask-Abschluss)."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    await _create_comment(
        session, task.id, "handoff",
        "Human-Review angefordert fuer 'X' — wartet auf Mark (kein Agent-Reviewer dispatcht).",
        author_type="system",
    )
    await _create_comment(
        session, task.id, "message",
        "Callback: Root-Task abgeschlossen (done).",
        author_type="system",
    )
    # Ein echter Operator-Kommentar muss trotzdem durchkommen.
    await _create_comment(session, task.id, "message", "Echte Anweisung vom Operator", author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Human-Review angefordert" not in result
    assert "Callback: Root-Task abgeschlossen" not in result
    assert "Echte Anweisung vom Operator" in result


@pytest.mark.asyncio
async def test_agent_dispatch_config_defaults(session: AsyncSession):
    """dispatch_config defaults to empty dict."""
    agent = Agent(name="TestAgent")
    session.add(agent)
    await session.flush()

    loaded = await session.get(Agent, agent.id)
    assert loaded.dispatch_config == {}


# ── Tests for _get_agent_timeout ─────────────────────────────────────


def test_get_agent_timeout_returns_default():
    """Without dispatch_config, the global default is returned."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="TestAgent")
    assert _get_agent_timeout(agent, "stale_progress_minutes", 30) == 30


def test_get_agent_timeout_returns_agent_value():
    """With dispatch_config, the agent value is returned."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="Cody", dispatch_config={"stale_progress_minutes": 45})
    assert _get_agent_timeout(agent, "stale_progress_minutes", 30) == 45


def test_get_agent_timeout_falls_back_on_missing_key():
    """Missing key in dispatch_config falls back to the default."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="Cody", dispatch_config={"stale_progress_minutes": 45})
    assert _get_agent_timeout(agent, "ack_timeout_minutes", 10) == 10
