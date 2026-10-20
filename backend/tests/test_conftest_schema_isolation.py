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


async def _row_counts(conn) -> dict[str, int]:
    from sqlmodel import SQLModel
    return {
        t.name: (await conn.execute(text(f'SELECT count(*) FROM "{t.name}"'))).scalar_one()
        for t in SQLModel.metadata.sorted_tables
    }


@pytest.mark.asyncio
async def test_empty_all_tables_leaves_every_model_table_empty(make_board, make_agent):
    """Row isolation between tests: `_empty_all_tables` only DELETEs the
    tables its one probe query reports as filled. Whatever the probe misses
    would leak into the next test — so fill tables, empty, count everything."""
    board = await make_board()
    await make_agent(board_id=board.id)
    async with cf.test_engine.begin() as conn:
        before = await _row_counts(conn)
        assert before["boards"] >= 1 and before["agents"] >= 1, before
        await cf._empty_all_tables(conn)
    async with cf.test_engine.begin() as conn:
        after = await _row_counts(conn)
    assert not {name: n for name, n in after.items() if n}, after


@pytest.mark.asyncio
async def test_empty_all_tables_still_raises_for_a_missing_table():
    """setup_db's rebuild rescue relies on an OperationalError when a model
    table is gone — the single probe query must raise just like the old
    per-table DELETE did."""
    from sqlalchemy.exc import OperationalError
    async with cf.test_engine.begin() as conn:
        await conn.execute(text("DROP TABLE boards"))
    with pytest.raises(OperationalError):
        async with cf.test_engine.begin() as conn:
            await cf._empty_all_tables(conn)
    async with cf.test_engine.begin() as conn:
        assert await cf._ensure_pristine_schema(conn) is True
