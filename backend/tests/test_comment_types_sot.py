"""Tests for comment-type single source of truth (REL-01).

Verifies that the two consumer constants in agents.py and
agent_scoped.py are derived from comment_types.py and haven't
drifted manually (live-bug pattern 2026-04-23 / 2026-04-24).
"""
import pytest


def test_deliverable_subset_invariant():
    """DELIVERABLE_SYSTEM_TYPES \\ {'system'} MUST be a subset of ALL_COMMENT_TYPES."""
    from app.comment_types import ALL_COMMENT_TYPES, DELIVERABLE_SYSTEM_TYPES
    drift = DELIVERABLE_SYSTEM_TYPES - ALL_COMMENT_TYPES - {"system"}
    assert not drift, f"Drift: {drift} in DELIVERABLE aber nicht in ALL"


def test_agents_deliver_uses_sot():
    """agents._DELIVER_SYSTEM_COMMENT_TYPES MUST come from DELIVERABLE_SYSTEM_TYPES."""
    from app.comment_types import DELIVERABLE_SYSTEM_TYPES
    from app.routers.agents import _DELIVER_SYSTEM_COMMENT_TYPES
    assert set(_DELIVER_SYSTEM_COMMENT_TYPES) == set(DELIVERABLE_SYSTEM_TYPES)


def test_agent_scoped_valid_uses_sot():
    """agent_scoped.VALID_COMMENT_TYPES MUST come from ALL_COMMENT_TYPES."""
    from app.comment_types import ALL_COMMENT_TYPES
    from app.routers.agent_scoped import VALID_COMMENT_TYPES
    assert set(VALID_COMMENT_TYPES) == set(ALL_COMMENT_TYPES)


@pytest.mark.parametrize("comment_type", [
    "subtask_completed", "resolution", "blocker", "feedback",
    "install_completed", "install_failed",
])
def test_known_deliverable_types_present(comment_type):
    """Live-bug regression: each of these types must remain deliverable."""
    from app.comment_types import DELIVERABLE_SYSTEM_TYPES
    assert comment_type in DELIVERABLE_SYSTEM_TYPES


# ── needs_decision (11.09.2026) ────────────────────────────────────────────


def test_needs_decision_is_an_accepted_comment_type():
    """`mc finish --needs-decision` haengt die offene Frage als DIESEN Typ an
    die Karte. Faellt er aus ALL_COMMENT_TYPES, antwortet der Comment-Endpunkt
    mit 422 und `mc finish` bricht ab, bevor die Karte schliesst."""
    from app.comment_types import ALL_COMMENT_TYPES
    assert "needs_decision" in ALL_COMMENT_TYPES


def test_needs_decision_wakes_nobody():
    """Die Karte ist fertig — der Kommentar ist ein Fundstueck an ihr, kein
    Weck-Signal. Waere er deliverable, wuerde die Frage an den Operator den
    gerade abgeschlossenen Worker per /me/poll erneut anstossen."""
    from app.comment_types import DELIVERABLE_SYSTEM_TYPES
    assert "needs_decision" not in DELIVERABLE_SYSTEM_TYPES
