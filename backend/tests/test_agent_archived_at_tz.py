"""agents.archived_at must be timezone-aware ON THE MODEL (regression).

The DB column has been timestamptz since 0157 — but SQLAlchemy's asyncpg
dialect renders bind casts from the MODEL type. A naive model field emitted
``$1::TIMESTAMP WITHOUT TIME ZONE`` and asyncpg refused the tz-aware
``utcnow()`` that agent_lifecycle writes, so every
``POST /agents/{id}/archive`` returned 500 (found live 2026-08-05 by the
demo-seed cleanup; root cause pinned by adversarial review 2026-08-06).
This test guards the model metadata so the naive variant cannot silently
come back.
"""
from app.models.agent import Agent


def test_archived_at_column_is_timezone_aware() -> None:
    col = Agent.__table__.c.archived_at
    assert getattr(col.type, "timezone", False) is True, (
        "agents.archived_at must be DateTime(timezone=True) on the MODEL — "
        "the asyncpg bind cast comes from the model type, and agent_lifecycle "
        "writes tz-aware utcnow()"
    )
    assert col.index, "archived_at index must survive the sa_column move"
