"""agents.archived_at must be timezone-aware (regression, 2026-08-05).

agent_lifecycle stamps archived_at with the tz-aware app.utils.utcnow().
The column was created as a naive TIMESTAMP, so on Postgres asyncpg refused
the encode and every POST /agents/{id}/archive returned 500 (found live by
the demo-seed cleanup). Migration 0175 converts the column; this test pins
the model definition so the naive variant cannot silently come back.
SQLite in the test engine accepts both, hence the check targets the column
metadata, not a round-trip.
"""
from app.models.agent import Agent


def test_archived_at_column_is_timezone_aware() -> None:
    col = Agent.__table__.c.archived_at
    assert getattr(col.type, "timezone", False) is True, (
        "agents.archived_at must be DateTime(timezone=True) — "
        "agent_lifecycle writes tz-aware utcnow() into it (see 0175)"
    )
    assert col.index, "archived_at index must survive the sa_column move"
