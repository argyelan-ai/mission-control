"""Runtime addresses are stored trimmed (round-3 user test, t4 S3).

A runtime row carried its endpoint as ``" http://<box>:5559/v1"`` — a leading
space. The add-runtime wizard's probe (``POST /runtimes/probe-endpoint``)
trims its URL, so the probe succeeded, but the row was saved untrimmed and
every later request to it failed with ``UnsupportedProtocol: Request URL is
missing an 'http://'`` (the Models tab showed the box as unreachable).

Guarded here:
  * ``RuntimeCreate`` / ``RuntimeUpdate`` trim endpoint, healthcheck path,
    control URL and model identifier — create AND patch, through the API.
  * Migration 0222 trims rows that were saved before (data only).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from app.models.runtime import Runtime
from app.routers.runtimes import RuntimeCreate, RuntimeUpdate

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0222_runtime_endpoint_trim.py"
)


def _create(**kw) -> RuntimeCreate:
    body = {
        "slug": "rt-a",
        "display_name": "rt-a",
        "runtime_type": "openai_compatible",
        "endpoint": "http://192.0.2.77:5559/v1",
    }
    body.update(kw)
    return RuntimeCreate(**body)


def test_create_trims_the_endpoint():
    assert _create(endpoint=" http://192.0.2.77:5559/v1 ").endpoint == "http://192.0.2.77:5559/v1"


def test_create_trims_tabs_and_newlines_too():
    assert _create(endpoint="\thttp://192.0.2.77:5559/v1\n").endpoint == "http://192.0.2.77:5559/v1"


def test_create_trims_the_other_address_fields():
    body = _create(
        healthcheck_path=" /v1/models ",
        control_url=" http://192.0.2.77:5555 ",
        model_identifier=" vendor/model-x ",
    )
    assert body.healthcheck_path == "/v1/models"
    assert body.control_url == "http://192.0.2.77:5555"
    assert body.model_identifier == "vendor/model-x"


def test_update_trims_the_endpoint():
    assert RuntimeUpdate(endpoint="  http://192.0.2.77:5559/v1").endpoint == "http://192.0.2.77:5559/v1"


def test_update_leaves_absent_fields_absent():
    body = RuntimeUpdate(display_name="x")
    assert body.endpoint is None
    assert "endpoint" not in body.model_fields_set


@pytest.mark.asyncio
async def test_api_create_and_patch_store_the_trimmed_endpoint(auth_client, session):
    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json={
            "slug": "rt-trim",
            "display_name": "rt-trim",
            "runtime_type": "cloud",
            "endpoint": " https://api.example.invalid/v1 ",
        },
    )
    assert resp.status_code == 200, resp.text
    row = (await session.exec(select(Runtime).where(Runtime.slug == "rt-trim"))).one()
    assert row.endpoint == "https://api.example.invalid/v1"

    resp = await auth_client.patch(
        "/api/v1/runtimes/db/rt-trim", json={"endpoint": " https://api2.example.invalid/v1"}
    )
    assert resp.status_code == 200, resp.text
    await session.refresh(row)
    assert row.endpoint == "https://api2.example.invalid/v1"


class _RealOp:
    """The two alembic ops the revision uses, bound to a real SQLite
    connection. The backend's own ``alembic/`` folder shadows the alembic
    package on the test path, so ``alembic.operations`` is not importable
    here — same approach as test_bench_start_and_order / test_migration_0204."""

    def __init__(self, conn):
        self.conn = conn

    def get_bind(self):
        return self.conn

    def execute(self, sql):
        import sqlalchemy as sa

        self.conn.execute(sa.text(sql))


def _run_migration(conn) -> None:
    import importlib.util
    import sys
    import types

    import alembic as _alembic

    placeholder = types.SimpleNamespace()
    _alembic.op = placeholder
    sys.modules["alembic.op"] = placeholder
    spec = importlib.util.spec_from_file_location("m0222", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0222_runtime_endpoint_trim"
    assert mod.down_revision == "0221_bench_entry_position"
    mod.op = _RealOp(conn)
    mod.upgrade()


def test_migration_trims_existing_rows():
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE runtimes (id INTEGER PRIMARY KEY, endpoint TEXT, "
            "control_url TEXT, healthcheck_path TEXT, model_identifier TEXT)"
        ))
        conn.execute(sa.text(
            "INSERT INTO runtimes VALUES "
            "(1, ' http://192.0.2.77:5559/v1', 'http://192.0.2.77:5555 ', "
            "' /v1/models', ' vendor/model-x' || char(10)), "
            "(2, 'http://192.0.2.1:8000/v1', NULL, '/v1/models', 'm'), "
            "(3, char(9) || 'https://api.example.invalid/v1', NULL, NULL, NULL)"
        ))
        _run_migration(conn)
        rows = conn.execute(sa.text(
            "SELECT id, endpoint, control_url, healthcheck_path, model_identifier "
            "FROM runtimes ORDER BY id"
        )).all()

    assert rows[0][1:] == (
        "http://192.0.2.77:5559/v1", "http://192.0.2.77:5555", "/v1/models", "vendor/model-x",
    )
    assert rows[1][1:] == ("http://192.0.2.1:8000/v1", None, "/v1/models", "m")
    assert rows[2][1] == "https://api.example.invalid/v1"
