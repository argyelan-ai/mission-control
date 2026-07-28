"""Migration 0168 — merge the two LM Studio rows into one engine row.

Verified with Mark on 2026-07-28: LM Studio serves exactly ONE chat model at a
time (plus a permanent embedding model) and the model is switched IN LM STUDIO,
not through an MC runtime switch. `nemotron-super` and `qwen-coder-lms`
therefore described the same engine behind the same endpoint — the local
counterpart to the drifted cloud labels 0167 repairs.

The safety guards get as much coverage as the happy path: this migration runs
on other people's installations, where an agent may well be bound to one of
these rows.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import pathlib
import types

import pytest
import sqlalchemy as sa

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0168_merge_lmstudio_runtime_rows.py"
)

ENDPOINT = "http://192.0.2.10:1234/v1"


@contextlib.contextmanager
def _migration_bound_to(conn):
    """`alembic.op` shim — `backend/alembic/` shadows the installed alembic
    package under pytest, so a real MigrationContext is out of reach (same
    reason test_migration_0091 shims `op`). This migration only calls
    `op.get_bind()`, so the real upgrade() SQL still runs."""
    import alembic as _alembic

    previous = getattr(_alembic, "op", None)
    _alembic.op = types.SimpleNamespace(get_bind=lambda: conn)
    try:
        spec = importlib.util.spec_from_file_location("mc_migration_0168", REVISION_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        if previous is None:
            delattr(_alembic, "op")
        else:
            _alembic.op = previous


def _make_engine(rows, agents=()):
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE runtimes ("
                " id TEXT PRIMARY KEY, slug TEXT, display_name TEXT, endpoint TEXT,"
                " runtime_type TEXT, model_identifier TEXT, role_tags TEXT,"
                " ui_order INTEGER, enabled BOOLEAN)"
            )
        )
        conn.execute(sa.text("CREATE TABLE agents (name TEXT, runtime_id TEXT)"))
        for row in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO runtimes VALUES (:id, :slug, :display_name, :endpoint,"
                    " :runtime_type, :model_identifier, :role_tags, :ui_order, :enabled)"
                ),
                row,
            )
        for name, runtime_id in agents:
            conn.execute(
                sa.text("INSERT INTO agents VALUES (:n, :r)"), {"n": name, "r": runtime_id}
            )
    return eng


def _lmstudio_pair(**overrides):
    retired = {
        "id": "rt-nemotron", "slug": "nemotron-super",
        "display_name": "Nemotron 3 Super", "endpoint": ENDPOINT,
        "runtime_type": "lmstudio", "model_identifier": None,
        "role_tags": json.dumps(["coder", "fallback"]), "ui_order": 3, "enabled": True,
    }
    survivor = {
        "id": "rt-qwen-lms", "slug": "qwen-coder-lms",
        "display_name": "Qwen3.6 35B A3B (LM Studio)", "endpoint": ENDPOINT,
        "runtime_type": "lmstudio", "model_identifier": "qwen/qwen3-coder-next",
        "role_tags": json.dumps(["coder"]), "ui_order": 4, "enabled": True,
    }
    retired.update(overrides.get("retired", {}))
    survivor.update(overrides.get("survivor", {}))
    return [retired, survivor]


def _run(engine, action="upgrade"):
    with engine.begin() as conn:
        with _migration_bound_to(conn) as module:
            getattr(module, action)()
        return {
            r["slug"]: dict(r)
            for r in conn.execute(sa.text("SELECT * FROM runtimes")).mappings()
        }


def test_revision_chains_onto_0167():
    engine = _make_engine([])
    with engine.connect() as conn:
        with _migration_bound_to(conn) as module:
            assert module.revision == "0168_merge_lmstudio_runtime_rows"
            assert module.down_revision == "0167_runtime_display_name_derived"


def test_merges_into_one_engine_row():
    engine = _make_engine(_lmstudio_pair())
    after = _run(engine)

    survivor = after["qwen-coder-lms"]
    assert survivor["display_name"] == "LM Studio (DGX — model follows the engine)"
    # The name must not promise a model — the engine decides which one runs.
    assert "Qwen" not in survivor["display_name"]
    # NULL so the probe writes back whatever LM Studio actually serves.
    assert survivor["model_identifier"] is None
    # "fallback" came from the retired row and must not silently disappear.
    assert json.loads(survivor["role_tags"]) == ["coder", "fallback"]
    assert survivor["ui_order"] == 3

    # Disabled, NOT deleted: a registry other things can point at is the wrong
    # place for destructive cleanup.
    assert after["nemotron-super"]["enabled"] in (0, False)
    assert len(after) == 2


def test_is_idempotent():
    engine = _make_engine(_lmstudio_pair())
    first = _run(engine)
    second = _run(engine)
    assert first == second


def test_skipped_when_an_agent_is_bound():
    """On someone else's installation an agent may sit on one of these rows —
    silently disabling its runtime would break that agent."""
    engine = _make_engine(_lmstudio_pair(), agents=[("Sparky", "rt-nemotron")])
    after = _run(engine)
    assert after["nemotron-super"]["enabled"] in (1, True)
    assert after["qwen-coder-lms"]["display_name"] == "Qwen3.6 35B A3B (LM Studio)"


def test_skipped_when_endpoints_differ():
    """Two LM Studio instances on two hosts are two real engines."""
    engine = _make_engine(
        _lmstudio_pair(retired={"endpoint": "http://192.0.2.99:1234/v1"})
    )
    after = _run(engine)
    assert after["nemotron-super"]["enabled"] in (1, True)
    assert after["qwen-coder-lms"]["model_identifier"] == "qwen/qwen3-coder-next"


def test_skipped_when_one_row_is_absent():
    """Fresh installs seed only the merged row — nothing to do, no crash."""
    engine = _make_engine([_lmstudio_pair()[1]])
    after = _run(engine)
    assert after["qwen-coder-lms"]["display_name"] == "Qwen3.6 35B A3B (LM Studio)"


def test_downgrade_re_enables_the_retired_row():
    engine = _make_engine(_lmstudio_pair())
    _run(engine)
    after = _run(engine, action="downgrade")
    assert after["nemotron-super"]["enabled"] in (1, True)


def test_no_slug_is_renamed_or_removed():
    engine = _make_engine(_lmstudio_pair())
    after = _run(engine)
    assert set(after) == {"nemotron-super", "qwen-coder-lms"}


@pytest.mark.parametrize("slug", ["nemotron-super", "qwen-coder-lms"])
def test_seed_file_contains_only_the_merged_row(slug):
    """A fresh install must not recreate the fiction."""
    entries = json.loads(
        (pathlib.Path(__file__).parents[1] / "config" / "runtimes.json").read_text()
    )
    lmstudio = {e["id"] for e in entries if e.get("runtime_type") == "lmstudio"}
    assert lmstudio == {"qwen-coder-lms"}
    assert (slug in lmstudio) == (slug == "qwen-coder-lms")
