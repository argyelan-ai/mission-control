"""Migration 0206 — the news/content vertical is gone, so are its tables.

The optional news vertical (news crawler, shorts/storyboards, newsletter)
was retired. Its models lived in core only to keep the Alembic chain
identical between variants; with the vertical gone they are dead schema.

Three layers:
  - the ORM no longer registers any news table, and ``content_pipelines``
    (still core: bench drafts, X-post approvals) lost its FK column into
    ``news_sources``;
  - the revision drops exactly the news tables + that column, and its
    downgrade recreates them (empty);
  - Postgres lane: after ``alembic upgrade head`` the tables are really gone.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import text

import app.models  # noqa: F401 — registers every table on SQLModel.metadata
from sqlmodel import SQLModel

REVISION_PATH = (
    pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0206_drop_news_tables.py"
)

NEWS_TABLES = {
    "news_sources",
    "news_articles",
    "news_post_schedules",
    "newsletter_issues",
    "storyboards",
    "trend_signals",
    "viral_shorts_settings",
    "video_performance",
}


class _RecordingOp:
    """Stand-in for ``alembic.op`` that records what a revision would do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def _record(*args, **_kwargs):
            self.calls.append((name, args))

        return _record


def _load_revision(op_shim):
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0206 not present at {REVISION_PATH}")
    import alembic as _alembic

    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim
    spec = importlib.util.spec_from_file_location("mig0206", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_orm_registers_no_news_table():
    assert NEWS_TABLES.isdisjoint(SQLModel.metadata.tables)


def test_content_pipelines_has_no_fk_into_news():
    table = SQLModel.metadata.tables["content_pipelines"]
    assert "rss_source_id" not in table.columns
    for fk in table.foreign_keys:
        assert fk.column.table.name not in NEWS_TABLES


def test_revision_wiring():
    module = _load_revision(types.SimpleNamespace())
    assert module.revision == "0206_drop_news_tables"
    assert module.down_revision == "0205_model_usage_head_runs"


def test_upgrade_drops_every_news_table_and_the_fk_column():
    op = _RecordingOp()
    module = _load_revision(op)
    module.upgrade()
    sql = " ".join(str(a[0]) for name, a in op.calls if name == "execute")
    for table in NEWS_TABLES:
        assert f"DROP TABLE IF EXISTS {table}" in sql
    assert "DROP COLUMN IF EXISTS rss_source_id" in sql
    # A raw, never-migrated config table some installs carry.
    assert "DROP TABLE IF EXISTS news_config" in sql


def test_downgrade_recreates_the_schema_empty():
    op = _RecordingOp()
    module = _load_revision(op)
    module.downgrade()
    sql = " ".join(str(a[0]) for name, a in op.calls if name == "execute")
    for table in NEWS_TABLES:
        assert f"CREATE TABLE {table} (" in sql
    assert "ADD COLUMN rss_source_id" in sql
    assert "INSERT" not in sql.upper()


@pytest.mark.postgres
async def test_news_tables_absent_after_upgrade_head(session):
    rows = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {"names": sorted(NEWS_TABLES | {"news_config"})},
    )
    assert rows.scalars().all() == []
    col = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'content_pipelines' AND column_name = 'rss_source_id'"
        )
    )
    assert col.first() is None
