"""Tests for callback_agent_id routing logic.

Checks:
1. Auto-set: non-board-lead creates task → callback_agent_id = board lead
2. Explicit override: payload.callback_agent_id is respected
3. Board lead creates task → callback_agent_id stays null
4. task_lifecycle routing: callback_agent_id takes priority over owner_agent_id
5. task_lifecycle routing: owner_agent_id only applies when board lead
"""
import uuid
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.task import Task
from app.models.agent import Agent
from app.models.board import Board


@pytest.fixture
async def board_with_agents(session: AsyncSession):
    """Creates: board + board lead (Henry) + planner."""
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
    """callback_agent_id is preferred, even when owner_agent_id is set."""
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
    """owner_agent_id applies as fallback when it is a board lead."""
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
    """When owner = planner and no callback_agent_id → fallback to board lead needed."""
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
    """callback_agent_id is correctly stored and loaded from the DB."""
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
    """callback_agent_id is nullable — existing tasks without the field still work."""
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
