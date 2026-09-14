"""Schema isolation of the SQLite lane (review #488 B1).

`setup_db` no longer rebuilds the schema per test; it compares a fingerprint
of sqlite_master before every test and rebuilds only on drift. This pins the
oracle: an ALTER TABLE left behind by a test (a migration test that aborts on
purpose) must be detected and reverted before the next test runs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from tests import conftest as cf


@pytest.mark.asyncio
async def test_added_column_is_detected_and_schema_rebuilt():
    async with cf.test_engine.begin() as conn:
        assert await cf._ensure_pristine_schema(conn) is False, "fresh test starts pristine"
        await conn.execute(text("ALTER TABLE agents ADD COLUMN gateway_id_probe TEXT"))
    async with cf.test_engine.begin() as conn:
        assert await cf._ensure_pristine_schema(conn) is True, "column drift must trigger a rebuild"
        cols = [r[1] for r in await conn.execute(text("PRAGMA table_info(agents)"))]
        assert "gateway_id_probe" not in cols
        assert await cf._ensure_pristine_schema(conn) is False


@pytest.mark.asyncio
async def test_dropped_table_is_detected_and_schema_rebuilt():
    async with cf.test_engine.begin() as conn:
        await conn.execute(text("DROP TABLE boards"))
    async with cf.test_engine.begin() as conn:
        assert await cf._ensure_pristine_schema(conn) is True
        names = [r[0] for r in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
        assert "boards" in names
