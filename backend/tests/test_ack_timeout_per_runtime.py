"""Tests for the ACK timeout 3-step lookup (REL-05).

Expected order:
  1. agent.dispatch_config['ack_timeout_minutes']  (per-agent JSON override)
  2. AGENT_RUNTIME_ACK_TIMEOUTS[agent.agent_runtime] (runtime default)
  3. _DEFAULT_ACK_TIMEOUT_MINUTES (5)

Phase-1 Plan 03: Helper landed in app.services.task_runner.
"""
import pytest


def _import_helper():
    from app.services.task_runner import _get_ack_timeout_minutes
    return _get_ack_timeout_minutes


def test_per_agent_override_wins():
    """Per-agent dispatch_config['ack_timeout_minutes'] beats the runtime default."""
    helper = _import_helper()

    class FakeAgent:
        agent_runtime = "host"
        dispatch_config = {"ack_timeout_minutes": 30}

    assert helper(FakeAgent()) == 30


def test_per_agent_override():
    """Alias for test_per_agent_override_wins — VALIDATION.md row 1-03-02 names
    this name explicitly. Kept as a thin wrapper definition to ensure both
    test IDs can be invoked.
    """
    helper = _import_helper()

    class FakeAgent:
        agent_runtime = "openclaw"
        dispatch_config = {"ack_timeout_minutes": 42}

    assert helper(FakeAgent()) == 42


def test_runtime_default_host_is_5():
    """Runtime 'host' without an override → 5."""
    helper = _import_helper()

    class FakeAgent:
        agent_runtime = "host"
        dispatch_config = {}

    assert helper(FakeAgent()) == 5


@pytest.mark.parametrize("runtime,expected", [
    ("host", 5),
    ("cli-bridge", 15),
    ("openclaw", 15),
])
def test_runtime_defaults(runtime, expected):
    """Runtime defaults from AGENT_RUNTIME_ACK_TIMEOUTS."""
    helper = _import_helper()

    class FakeAgent:
        pass

    a = FakeAgent()
    a.agent_runtime = runtime
    a.dispatch_config = None
    assert helper(a) == expected


def test_unknown_runtime_falls_back():
    """Unknown runtime → _DEFAULT_ACK_TIMEOUT_MINUTES (5)."""
    helper = _import_helper()

    class FakeAgent:
        agent_runtime = "claude-code"  # not in AGENT_RUNTIME_ACK_TIMEOUTS
        dispatch_config = None

    assert helper(FakeAgent()) == 5
