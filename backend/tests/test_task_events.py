"""Tests fuer Task Event Sourcing + Status State Machine + Review Monitoring."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── TaskStatus Enum + Transitions ─────────────────────────────────────────


def test_task_status_enum_values():
    """Alle 8 Status-Werte sind definiert."""
    from app.task_status import TaskStatus

    assert len(TaskStatus) == 8
    assert set(TaskStatus) == {
        "inbox", "in_progress", "review", "user_test",
        "done", "blocked", "failed", "aborted",
    }


def test_valid_transitions_complete():
    """Jeder Status hat einen Eintrag in VALID_TRANSITIONS."""
    from app.task_status import TaskStatus, VALID_TRANSITIONS

    for status in TaskStatus:
        assert status in VALID_TRANSITIONS, f"{status} fehlt in VALID_TRANSITIONS"


def test_is_valid_transition():
    """is_valid_transition() prueft korrekt."""
    from app.task_status import is_valid_transition

    assert is_valid_transition("inbox", "in_progress") is True
    assert is_valid_transition("inbox", "done") is False
    assert is_valid_transition("aborted", "in_progress") is True
    assert is_valid_transition("aborted", "done") is False
    assert is_valid_transition("done", "in_progress") is True
    assert is_valid_transition("done", "inbox") is False


def test_aborted_in_transitions():
    """aborted hat eigene Transitions (war frueher nicht definiert)."""
    from app.task_status import VALID_TRANSITIONS

    assert "aborted" in VALID_TRANSITIONS
    assert VALID_TRANSITIONS["aborted"] == {"in_progress", "inbox"}


# ── TaskEvent Model ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_event_creation():
    """TaskEvent kann erstellt und gelesen werden."""
    from app.models.task import Task, TaskEvent
    from app.models.board import Board

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Test", slug="test")
        s.add(board)
        task = Task(id=uuid.uuid4(), board_id=board.id, title="Test Task")
        s.add(task)
        await s.commit()

        event = TaskEvent(
            task_id=task.id,
            from_status="inbox",
            to_status="in_progress",
            changed_by="agent",
            agent_id=None,
            reason="ack",
        )
        s.add(event)
        await s.commit()

        result = await s.exec(
            select(TaskEvent).where(TaskEvent.task_id == task.id)
        )
        events = result.all()
        assert len(events) == 1
        assert events[0].from_status == "inbox"
        assert events[0].to_status == "in_progress"
        assert events[0].changed_by == "agent"


# ── record_task_event() ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_task_event():
    """record_task_event() speichert Event korrekt."""
    from app.models.task import Task, TaskEvent
    from app.models.board import Board
    from app.services.task_lifecycle import record_task_event

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Test", slug="test")
        s.add(board)
        task = Task(id=uuid.uuid4(), board_id=board.id, title="Event Test")
        s.add(task)
        await s.commit()

        await record_task_event(
            s, task.id, "inbox", "in_progress",
            changed_by="user", reason="manual_update",
        )
        await s.commit()

        result = await s.exec(
            select(TaskEvent).where(TaskEvent.task_id == task.id)
        )
        events = result.all()
        assert len(events) == 1
        assert events[0].reason == "manual_update"
        assert events[0].changed_by == "user"


# ── Event Logging in Routers ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_status_change_creates_event(auth_client, make_board, make_task):
    """User-Status-Update via PATCH erzeugt ein TaskEvent."""
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
        )
    assert resp.status_code == 200

    # Event pruefen
    from app.models.task import TaskEvent
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskEvent).where(TaskEvent.task_id == task.id)
        )
        events = result.all()
        assert len(events) >= 1
        event = events[0]
        assert event.from_status == "inbox"
        assert event.to_status == "in_progress"
        assert event.changed_by == "user"


@pytest.mark.asyncio
async def test_agent_status_change_creates_event(client):
    """Agent-Status-Update via PATCH erzeugt ein TaskEvent mit agent_id."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task, TaskEvent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Test", slug="test")
        s.add(board)

        token_raw, token_hash = generate_agent_token()
        agent = Agent(
            id=agent_id, name="Cody", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
        )
        s.add(agent)

        task = Task(
            id=task_id, board_id=board_id, title="Agent Event Test",
            status="in_progress", assigned_agent_id=agent_id,
        )
        s.add(task)
        # Evidence-Guard: mind. 1 progress Kommentar vor Review
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=agent_id,
            comment_type="progress", content="Test evidence",
        ))
        # ADR-023: Reflection-Pflicht vor Closing-Transition (review/done)
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=agent_id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\nAgent-Status-Change Test\n\n"
                "## Was hat funktioniert\nPATCH erfolgreich\n\n"
                "## Was war unklar\nNichts\n\n"
                "## Lesson fuer Agent-Memory\n"
                "TaskEvent wird bei Status-Change von Agent korrekt erzeugt."
            ),
        ))
        await s.commit()

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {token_raw}"},
            json={"status": "review"},
        )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskEvent).where(TaskEvent.task_id == task_id)
        )
        events = result.all()
        assert len(events) >= 1
        event = events[0]
        assert event.from_status == "in_progress"
        assert event.to_status == "review"
        assert event.changed_by == "agent"
        assert event.agent_id == agent_id


# ── Event History Endpoint ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_events_endpoint(auth_client, make_board, make_task):
    """GET .../events liefert chronologische Event-History."""
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    # Zwei Status-Changes machen
    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
        )
        await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "review"},
        )

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/events"
    )
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 2
    # Neueste zuerst (desc)
    assert events[0]["to_status"] == "review"
    assert events[1]["to_status"] == "in_progress"


# ── State Machine — Ungueltige Transitions ───────────────────────────────


@pytest.mark.asyncio
async def test_invalid_transition_rejected(auth_client, make_board, make_task):
    """Ungueltiger Uebergang inbox→done wird mit 400 abgelehnt."""
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
        )
    assert resp.status_code == 400
    assert "Ungültiger Status-Übergang" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_aborted_to_in_progress_allowed(auth_client, make_board, make_task):
    """aborted→in_progress ist jetzt ein gueltiger Uebergang."""
    board = await make_board()
    task = await make_task(board.id, status="aborted")

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
        )
    assert resp.status_code == 200
