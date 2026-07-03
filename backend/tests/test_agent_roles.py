"""Tests for AgentRole enum, get_default_scopes(), Agent.role validator, find_agent_by_role()."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.scopes import AgentRole, DEFAULT_SCOPES, get_default_scopes, ALL_SCOPES, WORKER_ROLES, NON_WORKER_ROLES


# ── AgentRole Enum Tests ──────────────────────────────────────────────


def test_agent_role_values():
    """All 10 roles are defined."""
    assert set(AgentRole) == {
        AgentRole.LEAD, AgentRole.DEVELOPER, AgentRole.REVIEWER,
        AgentRole.TESTER, AgentRole.PLANNER, AgentRole.RESEARCHER,
        AgentRole.DEPLOYER, AgentRole.WRITER, AgentRole.ORCHESTRATOR,
        AgentRole.RELAY,
    }


def test_agent_role_matches_default_scopes_keys():
    """Every AgentRole has an entry in DEFAULT_SCOPES."""
    for role in AgentRole:
        assert role in DEFAULT_SCOPES, f"AgentRole.{role.name} fehlt in DEFAULT_SCOPES"


def test_worker_roles():
    assert WORKER_ROLES == frozenset({AgentRole.DEVELOPER, AgentRole.DEPLOYER})


def test_non_worker_roles():
    assert NON_WORKER_ROLES == frozenset({AgentRole.PLANNER, AgentRole.RESEARCHER, AgentRole.WRITER, AgentRole.ORCHESTRATOR})


# ── get_default_scopes() Tests ────────────────────────────────────────


def test_get_default_scopes_with_enum():
    """Lookup with AgentRole enum works."""
    scopes = get_default_scopes(AgentRole.DEVELOPER)
    assert "tasks:read" in scopes
    assert "tasks:write" in scopes


def test_get_default_scopes_with_string():
    """Legacy: lookup with string still works."""
    scopes = get_default_scopes("developer")
    assert "tasks:read" in scopes


def test_get_default_scopes_with_uppercase_string():
    """Legacy: case-insensitive string lookup."""
    scopes = get_default_scopes("REVIEWER")
    assert "tasks:read" in scopes


def test_get_default_scopes_unknown_returns_all():
    """Unknown template name → ALL_SCOPES."""
    scopes = get_default_scopes("unknown_role")
    assert scopes == list(ALL_SCOPES)


def test_get_default_scopes_lead_has_all():
    """Lead has all scopes."""
    scopes = get_default_scopes(AgentRole.LEAD)
    assert scopes == list(ALL_SCOPES)


# ── Agent.role Validator Tests ────────────────────────────────────────


def test_agent_role_validator_valid():
    from app.models.agent import Agent
    agent = Agent(name="TestAgent", role="developer")
    assert agent.role == "developer"


def test_agent_role_validator_none():
    from app.models.agent import Agent
    agent = Agent(name="TestAgent", role=None)
    assert agent.role is None


def test_agent_role_validator_invalid():
    from app.models.agent import Agent
    # SQLModel table=True uses model_validate for Pydantic validation
    with pytest.raises(Exception):
        Agent.model_validate({"name": "TestAgent", "role": "hacker"})


def test_agent_role_validator_all_roles():
    from app.models.agent import Agent
    for role in AgentRole:
        agent = Agent(name="Test", role=role.value)
        assert agent.role == role.value


# ── find_agent_by_role() Tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_agent_by_role_finds_correct_role(session, make_board, make_agent):
    """Finds agent with matching role."""
    board = await make_board()
    reviewer = await make_agent(
        name="Rex", role="reviewer", board_id=board.id,     )
    developer = await make_agent(
        name="Cody", role="developer", board_id=board.id,     )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is not None
    assert result.id == reviewer.id


@pytest.mark.asyncio
async def test_find_agent_by_role_fallback_to_lead(session, make_board, make_agent):
    """Falls back to board lead when no role is found."""
    board = await make_board()
    lead = await make_agent(
        name="Henry", role="lead", board_id=board.id,
        is_board_lead=True,     )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is not None
    assert result.id == lead.id


@pytest.mark.asyncio
async def test_find_agent_by_role_exclude(session, make_board, make_agent):
    """Exclude filter works."""
    board = await make_board()
    reviewer = await make_agent(
        name="Rex", role="reviewer", board_id=board.id,     )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(
        session, board.id, AgentRole.REVIEWER, exclude_agent_id=reviewer.id,
    )
    # No other reviewer, no board lead → None
    assert result is None


@pytest.mark.asyncio
async def test_find_agent_by_role_least_busy(session, make_board, make_agent, make_task):
    """With multiple candidates: agent with fewer active tasks is preferred."""
    board = await make_board()
    dev1 = await make_agent(
        name="Dev1", role="developer", board_id=board.id,     )
    dev2 = await make_agent(
        name="Dev2", role="developer", board_id=board.id,     )

    # dev1 has 2 active tasks, dev2 has 0
    await make_task(board_id=board.id, status="in_progress", assigned_agent_id=dev1.id)
    await make_task(board_id=board.id, status="in_progress", assigned_agent_id=dev1.id)

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.DEVELOPER)
    assert result is not None
    assert result.id == dev2.id


# ── _find_reviewer() Role-Based Tests ────────────────────────────────


@pytest.mark.asyncio
async def test_find_reviewer_by_role(session, make_board, make_agent):
    """Reviewer is found by role='reviewer', not by name."""
    board = await make_board()
    reviewer = await make_agent(
        name="Agent007", role="reviewer", board_id=board.id,     )

    from app.routers.agent_scoped import _find_reviewer
    result = await _find_reviewer(session, board.id)
    assert result is not None
    assert result.id == reviewer.id


@pytest.mark.asyncio
async def test_find_reviewer_legacy_fallback(session, make_board, make_agent):
    """Legacy: agent with 'rex' in its name is found when role=None."""
    board = await make_board()
    rex = await make_agent(
        name="Rex", role=None, board_id=board.id,     )

    from app.routers.agent_scoped import _find_reviewer
    result = await _find_reviewer(session, board.id)
    assert result is not None
    assert result.id == rex.id
