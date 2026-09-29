"""Postgres lane: the timezone contract behind the sqlmodel>=0.0.45 upgrade.

sqlmodel 0.0.45 maps plain ``datetime`` fields to ``UTCDateTime``
(``DateTime(timezone=True)``): aware values are normalised to UTC on bind,
naive values raise, reads come back aware UTC. On Postgres that is only
lossless when the real columns are ``timestamp with time zone`` — a
``timestamp without time zone`` column would silently store the session-
timezone wall clock instead. The SQLite lane cannot see any of this.

Run: MC_TEST_DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test
     (schema via ``alembic upgrade head``) — see .github/workflows/ci.yml.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import StatementError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.memory import BoardMemory
from tests.conftest import test_engine

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_all_migrated_datetime_columns_are_timestamptz():
    async with test_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND data_type = 'timestamp without time zone'"
                )
            )
        ).all()
    assert rows == [], f"naive timestamp columns in the migrated schema: {rows}"


@pytest.mark.asyncio
async def test_aware_datetime_roundtrips_as_same_instant_in_utc():
    zurich_summer = timezone(timedelta(hours=2))
    stamp = datetime(2026, 9, 24, 21, 0, 0, tzinfo=zurich_summer)
    bm_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(BoardMemory(id=bm_id, memory_type="journal", content="tz", source="system",
                          archived_at=stamp))
        await s.commit()
    async with AsyncSession(test_engine) as s:
        got = (await s.get(BoardMemory, bm_id)).archived_at
    assert got == stamp
    assert got.utcoffset() == timedelta(0)
    assert got.hour == 19


@pytest.mark.asyncio
async def test_naive_datetime_bind_is_rejected():
    async with AsyncSession(test_engine) as s:
        s.add(BoardMemory(id=uuid.uuid4(), memory_type="journal", content="tz",
                          source="system", archived_at=datetime(2026, 9, 24, 19, 0)))
        with pytest.raises(StatementError, match="timezone information"):
            await s.commit()
