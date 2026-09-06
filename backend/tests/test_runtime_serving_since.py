"""Laufzeit-Anzeige der Bühne — ``runtimes.serving_since`` (W3, 06.09.2026).

Was hier abgesichert wird:

  * Migration 0195 (Quelltext-Ebene): eine nullable Spalte, Downgrade nimmt
    sie zurück, keine Datenzeilen angefasst.
  * ``runtime_watcher._probe_one`` setzt ``serving_since`` NUR beim Übergang
    "nicht erreichbar/unbekannt → erreichbar" (leer → jetzt), überschreibt
    einen bereits gesetzten Wert bei einer ruhigen, weiter erfolgreichen Probe
    NICHT, und löscht ihn bei derselben Schwelle, die auch
    ``runtime.unreachable`` auslöst (drei aufeinanderfolgende Fehlproben).
  * ``slot_runtimes.write_slot_state`` — der bestätigte Rezept-Start —
    schreibt ``serving_since`` neu, weil die Slot-Zeile während der
    Schalt-Gnadenfrist keine Fehlproben zählt und sonst die Uptime des
    VORHERIGEN Modells zeigen würde.
  * ``GET /runtimes/live-status`` liefert ``serving_since`` als ISO-String
    (oder ``null``) je Zeile.

Testdaten heissen box-a / watch-rt / cockpit-rt. Kein Netz: Health-Probe wird
ersetzt.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import inspect as sa_inspect

import app.services.sse as sse_mod
from app.models.host import Host
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services import slot_runtimes
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_watcher import UNREACHABLE_EVENT_THRESHOLD, RuntimeWatcher
from tests.conftest import test_engine

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0195_runtime_serving_since.py"
)


def _load_migration():
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0195 not present at {REVISION_PATH}")

    calls: dict[str, list] = {"add_column": [], "drop_column": []}
    op_shim = types.SimpleNamespace(
        add_column=lambda *a, **k: calls["add_column"].append((a, k)),
        drop_column=lambda *a, **k: calls["drop_column"].append((a, k)),
    )
    import alembic as _alembic

    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0195", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis

    return _get


async def _mk_rt(session, *, slug="watch-rt", model="old-model", **kw):
    fields = dict(
        slug=slug, display_name=slug, runtime_type="vllm_docker",
        endpoint="http://spark:8000/v1", model_identifier=model, enabled=True,
    )
    fields.update(kw)
    rt = Runtime(**fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


# ── 1. Migration 0195 ─────────────────────────────────────────────────────────


def test_migration_metadata():
    module, _ = _load_migration()
    assert module.revision == "0195_runtime_serving_since"
    assert module.down_revision == "0194_runtime_is_slot"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_migration_upgrade_adds_nullable_column():
    module, calls = _load_migration()
    module.upgrade()

    assert len(calls["add_column"]) == 1
    (table_name, column), _ = calls["add_column"][0]
    assert table_name == "runtimes"
    assert column.name == "serving_since"
    assert column.nullable is True
    # Additiv, kein Server-Default nötig — NULL ist der harmlose Startzustand.
    assert column.server_default is None


def test_migration_downgrade_drops_only_the_column():
    module, calls = _load_migration()
    module.upgrade()
    module.downgrade()

    assert calls["drop_column"] == [(("runtimes", "serving_since"), {})]


async def test_runtime_model_has_serving_since_field():
    """SQLModel exposes serving_since; the live schema (SQLite test DB) too."""
    async with test_engine.connect() as conn:
        cols_list = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_columns("runtimes")
        )
    cols = {c["name"]: c for c in cols_list}
    assert "serving_since" in cols
    assert cols["serving_since"]["nullable"] is True

    rt = Runtime(slug="test-rt", display_name="Test", runtime_type="cloud", endpoint="x")
    assert rt.serving_since is None


# ── 2. Wächter: Übergänge ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watcher_sets_serving_since_on_first_reachable_probe(
    async_session, fake_redis
):
    """Leer → erreichbar setzt die Uhr auf jetzt."""
    rt = await _mk_rt(async_session, slug="up-rt")
    assert rt.serving_since is None
    watcher = RuntimeWatcher(interval=90)

    before = datetime.now(timezone.utc)
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("old-model", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)):
        await watcher.tick(session=async_session)
    after = datetime.now(timezone.utc)

    await async_session.refresh(rt)
    assert rt.serving_since is not None
    serving_since = rt.serving_since
    if serving_since.tzinfo is None:
        serving_since = serving_since.replace(tzinfo=timezone.utc)
    assert before <= serving_since <= after


@pytest.mark.asyncio
async def test_watcher_does_not_overwrite_existing_serving_since(
    async_session, fake_redis
):
    """Eine weitere ruhige, erfolgreiche Probe rührt einen gesetzten Wert nicht an."""
    fixed = datetime.now(timezone.utc) - timedelta(hours=2, minutes=41)
    rt = await _mk_rt(async_session, slug="steady-rt", serving_since=fixed)
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("old-model", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    got = rt.serving_since
    if got.tzinfo is None:
        got = got.replace(tzinfo=timezone.utc)
    assert got == fixed


@pytest.mark.asyncio
async def test_watcher_clears_serving_since_at_unreachable_threshold(
    async_session, fake_redis
):
    """Erreichbar → nicht erreichbar löscht die Uhr — bei derselben Schwelle
    wie ``runtime.unreachable`` (drei Fehlproben), nicht schon bei der ersten."""
    fixed = datetime.now(timezone.utc) - timedelta(minutes=10)
    rt = await _mk_rt(async_session, slug="down-rt", serving_since=fixed)
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel(None, None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)):
        for i in range(UNREACHABLE_EVENT_THRESHOLD - 1):
            await watcher.tick(session=async_session)
            await async_session.refresh(rt)
            # Noch nicht an der Schwelle — der alte Wert bleibt stehen.
            assert rt.serving_since is not None

        await watcher.tick(session=async_session)  # dritte Fehlprobe: Schwelle

    await async_session.refresh(rt)
    assert rt.serving_since is None


# ── 3. Slot-Zeile: bestätigter Rezept-Start ──────────────────────────────────


async def _host_row(session, slug="box-a") -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind="ssh", ssh_host="192.0.2.10")
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


@pytest.mark.asyncio
async def test_write_slot_state_sets_serving_since_on_confirmed_start(async_session):
    """Der Umschalter setzt serving_since neu, wenn er das Ziel-Modell sofort
    in die Slot-Zeile schreibt — unabhängig vom vorherigen Wert.

    Sabotage-Probe: ohne diese Zeile bliebe der ALTE Wert stehen, weil die
    Schalt-Gnadenfrist die Fehler-Schwelle des Wächters unterdrückt — die
    Bühne zeigte dann die Uptime des Modells, das die Box gerade VERLÄSST.
    """
    host = await _host_row(async_session)
    stale = datetime.now(timezone.utc) - timedelta(hours=5)
    slot = Runtime(
        slug="box-a-slot", host_id=host.id, display_name="BOX-A :8000",
        runtime_type="openai_compatible", endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/old-recipe", is_slot=True, exclusive_memory=False,
        enabled=True, serving_since=stale,
    )
    async_session.add(slot)
    await async_session.commit()
    await async_session.refresh(slot)

    before = datetime.now(timezone.utc)
    updated = await slot_runtimes.write_slot_state(
        async_session, host.id, model="org/new-recipe", context_len=131072
    )
    after = datetime.now(timezone.utc)

    assert updated is not None
    assert updated.model_identifier == "org/new-recipe"
    got = updated.serving_since
    if got.tzinfo is None:
        got = got.replace(tzinfo=timezone.utc)
    assert got != stale
    assert before <= got <= after


@pytest.mark.asyncio
async def test_write_slot_state_leaves_serving_since_when_no_slot(async_session):
    """Keine Slot-Zeile auf der Box → keine Zeile, kein Absturz."""
    host = await _host_row(async_session, slug="box-b")
    result = await slot_runtimes.write_slot_state(
        async_session, host.id, model="org/whatever"
    )
    assert result is None


# ── 4. Serialisierung ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_status_serializes_serving_since_iso(async_session, auth_client):
    fixed = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    rt = await _mk_rt(async_session, slug="cockpit-rt", serving_since=fixed)
    redis = await get_redis()
    import json

    await redis.setex(
        RedisKeys.runtime_live(rt.slug), 300,
        json.dumps({
            "reachable": True, "served_model": "old-model",
            "latency_ms": 12, "last_probe_at": "2026-09-06T12:00:00+00:00",
            "consecutive_failures": 0,
        }),
    )

    body = (await auth_client.get("/api/v1/runtimes/live-status")).json()

    # SQLite (the test DB) stores/returns a naive datetime — the value still
    # round-trips to the same instant, just without the offset suffix.
    got = body["live"][rt.slug]["serving_since"]
    assert got == fixed.isoformat() or got == fixed.replace(tzinfo=None).isoformat()


@pytest.mark.asyncio
async def test_live_status_serving_since_null_when_unset(async_session, auth_client):
    rt = await _mk_rt(async_session, slug="fresh-rt")
    redis = await get_redis()
    import json

    await redis.setex(
        RedisKeys.runtime_live(rt.slug), 300,
        json.dumps({
            "reachable": False, "served_model": None,
            "latency_ms": None, "last_probe_at": "2026-09-06T12:00:00+00:00",
            "consecutive_failures": 1,
        }),
    )

    body = (await auth_client.get("/api/v1/runtimes/live-status")).json()

    assert body["live"][rt.slug]["serving_since"] is None
