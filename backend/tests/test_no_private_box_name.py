"""App code names no private box; the one legacy value is migrated as data.

The power-managed Windows box (Wake-on-LAN + a Flask PowerShell control
server, ADR-042) used to be a runtime_type named after one operator's machine.
The DB columns were already generic since migration 0130; what was left were
the type value, a seed entry, variable names and labels. Since 2026-10 the
type is ``flask_wol`` — the same word as the host kind it pairs with
(``hosts.kind = "flask_wol"``, ADR-048).

Existing installs keep their data: migration 0217 rewrites only the
``runtime_type`` value; slug, display name, host row and every other field
stay as the operator named them.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO = Path(__file__).resolve().parents[2]
SCANNED = [REPO / "backend" / "app", REPO / "frontend-v2" / "src"]
# The private box name. Only this test (outside the scanned trees) and the
# historical migrations may spell it.
NEEDLE = "porsche"
LEGACY_TYPE = "unsloth_porsche"

MIGRATION = REPO / "backend" / "alembic" / "versions" / "0217_runtime_type_flask_wol.py"


def test_app_code_names_no_private_box():
    hits = []
    for root in SCANNED:
        for path in root.rglob("*"):
            if not path.is_file() or "node_modules" in path.parts or path.suffix in {".png", ".jpg", ".webp", ".ico", ".woff2"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if NEEDLE in line.lower():
                    hits.append(f"{path.relative_to(REPO)}:{no}")
    assert hits == [], f"{len(hits)} lines still name the private box: {hits[:20]}"


def test_seed_file_names_no_private_box():
    seed = (REPO / "backend" / "config" / "runtimes.json").read_text().lower()
    assert NEEDLE not in seed


# ── migration 0217: the one data update ───────────────────────────────────


def _load_migration(op_obj):
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = op_obj
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake_alembic
    try:
        spec = importlib.util.spec_from_file_location("mig0217_under_test", MIGRATION)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return mod


def _db():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE runtimes (slug TEXT PRIMARY KEY, display_name TEXT, "
            "runtime_type TEXT, endpoint TEXT, control_url TEXT, host_id TEXT)"
        ))
        conn.execute(sa.text(
            "INSERT INTO runtimes VALUES "
            "('unsloth-porsche', 'Unsloth (PORSCHE)', :legacy, 'http://192.0.2.20:8000/v1', "
            " 'http://192.0.2.20:5555', 'host-1'),"
            "('qwen-general', 'Qwen', 'vllm_docker', 'http://192.0.2.10:8000/v1', NULL, NULL),"
            "('studio', 'Unsloth Studio', 'unsloth', 'http://192.0.2.10:8888', NULL, NULL)"
        ), {"legacy": LEGACY_TYPE})
    return engine


def _rows(engine):
    with engine.connect() as conn:
        return {r[0]: tuple(r[1:]) for r in conn.execute(sa.text(
            "SELECT slug, display_name, runtime_type, endpoint, control_url, host_id "
            "FROM runtimes ORDER BY slug"
        ))}


def _run(engine, fn_name):
    with engine.begin() as conn:
        op_obj = types.SimpleNamespace(get_bind=lambda: conn, execute=lambda sql: conn.execute(
            sa.text(sql) if isinstance(sql, str) else sql))
        mod = _load_migration(op_obj)
        getattr(mod, fn_name)()
    return mod


def test_migration_metadata():
    mod = _load_migration(types.SimpleNamespace())
    assert mod.revision == "0217_runtime_type_flask_wol"
    assert mod.down_revision == "0216_host_ssh_host_key"


def test_upgrade_rewrites_only_the_type_value_and_keeps_the_operators_names():
    engine = _db()
    before = _rows(engine)
    _run(engine, "upgrade")
    after = _rows(engine)

    assert after["unsloth-porsche"][1] == "flask_wol"
    # slug + display name + every other field stay as the operator named them
    assert after["unsloth-porsche"][0] == before["unsloth-porsche"][0] == "Unsloth (PORSCHE)"
    assert after["unsloth-porsche"][2:] == before["unsloth-porsche"][2:]
    # other rows untouched — including the plain "unsloth" type
    assert after["qwen-general"] == before["qwen-general"]
    assert after["studio"] == before["studio"]


def test_upgrade_is_idempotent_and_downgrade_restores_the_old_value():
    engine = _db()
    _run(engine, "upgrade")
    once = _rows(engine)
    _run(engine, "upgrade")
    assert _rows(engine) == once
    _run(engine, "downgrade")
    assert _rows(engine)["unsloth-porsche"][1] == LEGACY_TYPE
    assert _rows(engine)["studio"][1] == "unsloth"

