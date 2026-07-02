"""Phase 25 / D-11: Hermes ACK-Timeout via dispatch_config Per-Agent-Override.

Verifies that _get_ack_timeout_minutes honors the dispatch_config['ack_timeout_minutes']
set by alembic migration 0096 -- Boss (also runtime='host') stays at 5min, only Hermes
gets 15min.
"""
from types import SimpleNamespace

from app.services.task_runner import (
    AGENT_RUNTIME_ACK_TIMEOUTS,
    STALE_PROGRESS_MINUTES,
    STALE_PROGRESS_MINUTES_BY_ROLE,
    _DEFAULT_ACK_TIMEOUT_MINUTES,
    _get_ack_timeout_minutes,
    _idle_threshold_for,
)


def _agent(*, agent_runtime: str = "host", dispatch_config=None, role: str = "developer", is_board_lead: bool = False):
    return SimpleNamespace(
        agent_runtime=agent_runtime,
        dispatch_config=dispatch_config,
        role=role,
        is_board_lead=is_board_lead,
    )


def test_baseline_host_runtime_is_5_minutes():
    """Boss (agent_runtime='host', no dispatch_config) keeps the host default."""
    assert _get_ack_timeout_minutes(_agent()) == 5
    assert AGENT_RUNTIME_ACK_TIMEOUTS["host"] == 5  # guard the contract


def test_hermes_ack_timeout_is_15_minutes():
    """Hermes (D-11): per-agent override wins over runtime default."""
    agent = _agent(
        agent_runtime="host",
        dispatch_config={"ack_timeout_minutes": 15},
    )
    assert _get_ack_timeout_minutes(agent) == 15


def test_per_agent_override_wins_over_runtime():
    """Lookup precedence: Stufe 1 (per-agent) beats Stufe 2 (runtime)."""
    agent = _agent(
        agent_runtime="host",
        dispatch_config={"ack_timeout_minutes": 99},
    )
    assert _get_ack_timeout_minutes(agent) == 99


def test_default_fallback_for_unknown_runtime():
    """Unknown runtime + no override -> hard fallback (_DEFAULT_ACK_TIMEOUT_MINUTES)."""
    agent = _agent(agent_runtime="unknown-runtime")
    assert _get_ack_timeout_minutes(agent) == _DEFAULT_ACK_TIMEOUT_MINUTES


def test_empty_dispatch_config_falls_through():
    """dispatch_config={} or None must NOT raise -- falls through to runtime default."""
    assert _get_ack_timeout_minutes(_agent(dispatch_config={})) == 5
    assert _get_ack_timeout_minutes(_agent(dispatch_config=None)) == 5


# -----------------------------------------------------------------------------
# Phase 26 / FND-06: per-agent idle_timeout_minutes override
# Mirrors the ack_timeout_minutes pattern above but for the in_progress idle window.
# Migration 0097 sets:
#   - Deployer:  30 min
#   - FreeCode:  20 min
#   - Davinci:   20 min
#   - Neo:       20 min
# -----------------------------------------------------------------------------


def test_idle_timeout_minutes_overrides_default():
    """FND-06: dispatch_config['idle_timeout_minutes'] overrides role default."""
    agent = _agent(dispatch_config={"idle_timeout_minutes": 30}, role="deployer")
    assert _idle_threshold_for(agent) == 30


def test_idle_timeout_minutes_overrides_stale_progress():
    """FND-06: priority order -- idle_timeout_minutes wins over stale_progress_minutes."""
    agent = _agent(
        dispatch_config={"idle_timeout_minutes": 30, "stale_progress_minutes": 15},
        role="deployer",
    )
    assert _idle_threshold_for(agent) == 30


def test_stale_progress_minutes_backwards_compat():
    """FND-06: agents with ONLY stale_progress_minutes still honored (backwards-compat)."""
    agent = _agent(dispatch_config={"stale_progress_minutes": 15}, role="developer")
    assert _idle_threshold_for(agent) == 15


def test_no_dispatch_config_uses_role_default():
    """FND-06: dispatch_config=None falls through to role/runtime default (no AttributeError)."""
    # developer role default = 15
    assert _idle_threshold_for(_agent(dispatch_config=None, role="developer")) == \
        STALE_PROGRESS_MINUTES_BY_ROLE["developer"]
    # unknown role + not board lead -> hard fallback
    assert _idle_threshold_for(_agent(dispatch_config=None, role="mystery")) == \
        STALE_PROGRESS_MINUTES


def test_deployer_long_task_not_reset():
    """FND-06: simulate Deployer with idle_timeout_minutes=30 -- a 20-min idle task
    is BELOW threshold, so the stale-check would not trip.

    We assert the threshold contract (>= 20) rather than spinning up the full
    watchdog loop -- that path is integration-tested elsewhere; here we lock in
    the per-agent override semantics that drive the decision.
    """
    deployer = _agent(
        agent_runtime="host",
        dispatch_config={"idle_timeout_minutes": 30},
        role="deployer",
    )
    threshold = _idle_threshold_for(deployer)
    assert threshold == 30
    # 20 minutes elapsed < 30 minute threshold -> task survives stale-check
    elapsed_minutes = 20
    assert elapsed_minutes < threshold, (
        f"Deployer 20-min task would be killed at {threshold}min threshold -- "
        "FND-06 regression"
    )
