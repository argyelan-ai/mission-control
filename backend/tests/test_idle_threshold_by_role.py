"""Idle thresholds are role-based.

Workers (developer/reviewer/designer/researcher/...) get low
thresholds (15-20 min) so that stuck agents are found quickly.
Orchestrators (Boss/Planner) legitimately wait longer for callbacks
→ 45 min. Default 60 min for unknown roles.

Agent-specific override via dispatch_config['stale_progress_minutes']
takes precedence.
"""
import pytest
from app.models.agent import Agent
from app.services.task_runner import (
    _idle_threshold_for,
    STALE_PROGRESS_MINUTES,
    STALE_PROGRESS_MINUTES_BY_ROLE,
)


def test_developer_gets_15_min():
    agent = Agent(name="Cody", role="developer")
    assert _idle_threshold_for(agent) == 15


def test_reviewer_gets_15_min():
    agent = Agent(name="Rex", role="reviewer")
    assert _idle_threshold_for(agent) == 15


def test_designer_gets_15_min():
    agent = Agent(name="Davinci", role="designer")
    assert _idle_threshold_for(agent) == 15


def test_researcher_gets_20_min():
    agent = Agent(name="Researcher", role="researcher")
    assert _idle_threshold_for(agent) == 20


def test_orchestrator_gets_45_min():
    """Boss as orchestrator gets 45 min — delegates and legitimately waits."""
    agent = Agent(name="Boss", role="orchestrator")
    assert _idle_threshold_for(agent) == 45


def test_planner_gets_45_min():
    agent = Agent(name="Planner", role="planner")
    assert _idle_threshold_for(agent) == 45


def test_unknown_role_falls_back_to_default():
    agent = Agent(name="Ghost", role="unknown_role")
    assert _idle_threshold_for(agent) == STALE_PROGRESS_MINUTES


def test_no_role_falls_back_to_default():
    agent = Agent(name="NoRole", role=None)
    assert _idle_threshold_for(agent) == STALE_PROGRESS_MINUTES


def test_board_lead_without_role_gets_45():
    """Board lead without a role gets the orchestrator threshold."""
    agent = Agent(name="Lead", is_board_lead=True)
    assert _idle_threshold_for(agent) == 45


def test_agent_dispatch_config_override_wins():
    """dispatch_config[stale_progress_minutes] overrides the role."""
    agent = Agent(name="Custom", role="developer", dispatch_config={"stale_progress_minutes": 5})
    assert _idle_threshold_for(agent) == 5


def test_override_wins_over_board_lead():
    agent = Agent(name="Boss", is_board_lead=True, role="orchestrator", dispatch_config={"stale_progress_minutes": 90})
    assert _idle_threshold_for(agent) == 90


def test_all_roles_in_mapping_return_expected():
    for role, minutes in STALE_PROGRESS_MINUTES_BY_ROLE.items():
        agent = Agent(name="T", role=role)
        assert _idle_threshold_for(agent) == minutes, f"role {role} expected {minutes}"
