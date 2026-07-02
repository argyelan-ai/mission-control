"""Phase 26 / Plan 26-01 -> 26-06 GREEN tests for FND-06 (HERM-10 sibling):
per-agent idle_timeout_minutes override.

Was RED in 26-01; turned GREEN by Migration 0097 + task_runner._idle_threshold_for
extension in Plan 26-06.

The core helper-level coverage lives in test_hermes_dispatch_config.py (mirror
of the ack_timeout_minutes pattern). These two tests guard the original
RED contract from 26-01 to ensure no regression.
"""
from types import SimpleNamespace

from app.services.task_runner import _idle_threshold_for


def _agent(*, dispatch_config=None, role: str = "deployer", agent_runtime: str = "host"):
    return SimpleNamespace(
        agent_runtime=agent_runtime,
        dispatch_config=dispatch_config,
        role=role,
        is_board_lead=False,
    )


def test_idle_timeout_minutes_overrides_default():
    """GREEN -- FND-06: agents.dispatch_config['idle_timeout_minutes'] overrides
    the role/runtime default in the watchdog idle check.
    """
    agent = _agent(dispatch_config={"idle_timeout_minutes": 30})
    assert _idle_threshold_for(agent) == 30


def test_deployer_long_task_not_reset():
    """GREEN -- FND-06: a Deployer agent with idle_timeout_minutes=30 has a
    threshold high enough to survive a 20-minute build/deploy chain.
    """
    deployer = _agent(dispatch_config={"idle_timeout_minutes": 30})
    threshold = _idle_threshold_for(deployer)
    assert threshold == 30
    elapsed_minutes = 20  # mid-deploy: npm install + next build + Vercel + DNS
    assert elapsed_minutes < threshold
