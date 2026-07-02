"""Tests fuer Comment-Type Single Source of Truth (REL-01).

Verifiziert dass die zwei consumer-konstanten in agents.py und
agent_scoped.py aus comment_types.py abgeleitet sind und nicht
manuell drifteten (Live-Bug-Pattern 2026-04-23 / 2026-04-24).
"""
import pytest


def test_deliverable_subset_invariant():
    """DELIVERABLE_SYSTEM_TYPES \\ {'system'} MUSS Subset von ALL_COMMENT_TYPES sein."""
    from app.comment_types import ALL_COMMENT_TYPES, DELIVERABLE_SYSTEM_TYPES
    drift = DELIVERABLE_SYSTEM_TYPES - ALL_COMMENT_TYPES - {"system"}
    assert not drift, f"Drift: {drift} in DELIVERABLE aber nicht in ALL"


def test_agents_deliver_uses_sot():
    """agents._DELIVER_SYSTEM_COMMENT_TYPES MUSS aus DELIVERABLE_SYSTEM_TYPES kommen."""
    from app.comment_types import DELIVERABLE_SYSTEM_TYPES
    from app.routers.agents import _DELIVER_SYSTEM_COMMENT_TYPES
    assert set(_DELIVER_SYSTEM_COMMENT_TYPES) == set(DELIVERABLE_SYSTEM_TYPES)


def test_agent_scoped_valid_uses_sot():
    """agent_scoped.VALID_COMMENT_TYPES MUSS aus ALL_COMMENT_TYPES kommen."""
    from app.comment_types import ALL_COMMENT_TYPES
    from app.routers.agent_scoped import VALID_COMMENT_TYPES
    assert set(VALID_COMMENT_TYPES) == set(ALL_COMMENT_TYPES)


@pytest.mark.parametrize("comment_type", [
    "subtask_completed", "resolution", "blocker", "feedback",
    "install_completed", "install_failed",
])
def test_known_deliverable_types_present(comment_type):
    """Live-Bug-Regression: jeder dieser Type muss als deliverable bleiben."""
    from app.comment_types import DELIVERABLE_SYSTEM_TYPES
    assert comment_type in DELIVERABLE_SYSTEM_TYPES
