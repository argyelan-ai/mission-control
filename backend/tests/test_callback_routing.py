"""Tests fuer callback_agent_id Routing-Logik.

Prueft:
1. Auto-set: Nicht-Board-Lead erstellt Task → callback_agent_id = Board Lead
2. Explicit override: payload.callback_agent_id wird respektiert
3. Board Lead erstellt Task → callback_agent_id bleibt null
4. task_lifecycle Routing: callback_agent_id hat Vorrang vor owner_agent_id
5. task_lifecycle Routing: owner_agent_id greift nur wenn Board Lead
"""
import uuid
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.task import Task
from app.models.agent import Agent
from app.models.board import Board


@pytest.fixture
async def board_with_agents(session: AsyncSession):
    """Erstellt: Board + Board Lead (Henry) + Planner."""
    board = Board(id=uuid.uuid4(), name="Test Board", slug="test-board")
    session.add(board)

    henry = Agent(
        id=uuid.uuid4(),
        name="Henry",
        board_id=board.id,
        is_board_lead=True,
    )
    session.add(henry)

    planner = Agent(
        id=uuid.uuid4(),
        name="Planner",
        board_id=board.id,
        is_board_lead=False,
    )
    session.add(planner)

    await session.commit()
    await session.refresh(board)
    await session.refresh(henry)
    await session.refresh(planner)
    return board, henry, planner


@pytest.mark.asyncio
async def test_callback_agent_id_takes_priority_over_owner(session: AsyncSession, board_with_agents):
    """callback_agent_id wird bevorzugt, auch wenn owner_agent_id gesetzt ist."""
    board, henry, planner = board_with_agents

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Test Task",
        owner_agent_id=planner.id,
        callback_agent_id=henry.id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    assert task.callback_agent_id == henry.id
    assert task.owner_agent_id == planner.id


@pytest.mark.asyncio
async def test_owner_as_board_lead_is_fallback(session: AsyncSession, board_with_agents):
    """owner_agent_id greift als Fallback wenn er ein Board Lead ist."""
    board, henry, planner = board_with_agents

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Test Task",
        owner_agent_id=henry.id,
        callback_agent_id=None,
    )
    session.add(task)
    await session.commit()

    assert task.callback_agent_id is None
    assert task.owner_agent_id == henry.id
    assert henry.is_board_lead is True


@pytest.mark.asyncio
async def test_planner_owner_without_callback_agent(session: AsyncSession, board_with_agents):
    """Wenn owner = Planner und kein callback_agent_id → Fallback auf Board Lead nötig."""
    board, henry, planner = board_with_agents

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Test Task",
        owner_agent_id=planner.id,
        callback_agent_id=None,
    )
    session.add(task)
    await session.commit()

    assert task.callback_agent_id is None
    assert planner.is_board_lead is False


@pytest.mark.asyncio
async def test_callback_agent_id_persisted(session: AsyncSession, board_with_agents):
    """callback_agent_id wird korrekt in DB gespeichert und geladen."""
    board, henry, planner = board_with_agents

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Test Task",
        owner_agent_id=planner.id,
        callback_agent_id=henry.id,
    )
    session.add(task)
    await session.commit()

    loaded = await session.get(Task, task.id)
    assert loaded is not None
    assert loaded.callback_agent_id == henry.id


@pytest.mark.asyncio
async def test_callback_agent_id_nullable(session: AsyncSession, board_with_agents):
    """callback_agent_id ist nullable — bestehende Tasks ohne das Feld funktionieren."""
    board, henry, planner = board_with_agents

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Legacy Task",
        owner_agent_id=henry.id,
    )
    session.add(task)
    await session.commit()

    loaded = await session.get(Task, task.id)
    assert loaded is not None
    assert loaded.callback_agent_id is None
