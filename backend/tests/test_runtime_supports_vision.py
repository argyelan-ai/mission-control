"""Vision-Fähigkeit je Runtime, an omp-Agenten weitergegeben (W3, 06.09.2026).

Verifizierter Befund (live, 06.09.2026): der Motor (GLM-5.3-Flash EXL3, vLLM)
beantwortet Bilder korrekt über die API. omp im Agenten-Container bekam von MC
bislang eine ``models.yml`` OHNE ``input:``-Liste — das MC-Modell galt für omp
als text-only, und sein Auflösungspfad (``s.find(u => u.input.includes
("image"))``, im omp-Binary ``/usr/local/bin/omp`` v18.1.10 per ``grep``
nachgelesen) fiel auf ein eingebautes, nie konfiguriertes Standard-Vision-
Modell zurück — Ergebnis: ``401 Incorrect API key provided: sk-noauth`` gegen
api.openai.com statt gegen unsere eigene Box.

Was hier abgesichert wird — jede Prüfung MIT Sabotage-Probe (Gegenfall, der
ohne die neue Regel durchginge):

  * Migration 0196 (Quelltext-Ebene): zwei additive NOT-NULL-Spalten mit
    Server-Default false, Downgrade nimmt beide zurück, der Seed ist ein
    Namens-/Slug-Muster (KEINE Gerätedaten, ADR-077 Regel 7).
  * ``runtimes.supports_vision`` / ``local_recipes.supports_vision`` sind auf
    den Modellen vorhanden und defaulten auf false (der sichere Startzustand:
    eine unbekannte Runtime blockt Bilder statt sie ungefragt extern zu
    verschicken).
  * ``build_runtime_env`` (omp-Zweig) setzt ``OMP_MODEL_INPUT`` aus
    ``runtime.supports_vision`` — Gegenfall: ohne das Feld bliebe der alte,
    kaputte Zustand (kein ``input:`` in models.yml).
  * ``render-omp-config.sh`` rendert ``input: [text, image]`` bzw. ``[text]``
    in models.yml aus ``OMP_MODEL_INPUT`` — Gegenfall: „text" ohne "image"
    ergibt ``[text]``.
  * ``recipe_switcher.build_runtime_from_recipe`` kopiert das Katalogfeld in
    die Instanz; ``slot_runtimes.write_slot_state`` zieht es in die Slot-Zeile
    mit — Gegenfall: ein Aufruf ohne das Argument (``None``) lässt den
    bestehenden Wert unangetastet, überschreibt ihn NICHT mit false.
  * ``GET /runtimes`` / ``PATCH /runtimes/db/{slug}`` tragen das Feld — beide
    hängen an ``model_dump()``, keine Sonderbehandlung nötig, aber ein
    Regressionstest hält das fest.

Testdaten heissen box-a / recipe-x / rt-a. Kein Netz, kein Docker: die
Render-Skript-Prüfung läuft direkt gegen ``docker/omp-bridge/render-omp-
config.sh`` mit einem Temp-``HOME`` und ohne Bootstrap.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import types
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import slot_runtimes
from app.services.host_resolver import resolved_host_from_row
from app.services.recipe_switcher import build_runtime_from_recipe
from tests.conftest import test_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0196_runtime_supports_vision.py"
)
RENDER_SCRIPT = REPO_ROOT / "docker" / "omp-bridge" / "render-omp-config.sh"


# ── 1. Migration 0196 (Quelltext-Ebene) ──────────────────────────────────────


def _load_migration():
    if not MIGRATION_PATH.is_file():
        pytest.fail(f"Migration 0196 not present at {MIGRATION_PATH}")

    calls: dict[str, list] = {"add_column": [], "drop_column": [], "execute": []}
    op_shim = types.SimpleNamespace(
        add_column=lambda *a, **k: calls["add_column"].append((a, k)),
        drop_column=lambda *a, **k: calls["drop_column"].append((a, k)),
        execute=lambda *a, **k: calls["execute"].append((a, k)),
    )
    import alembic as _alembic

    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0196", str(MIGRATION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_migration_metadata():
    module, _ = _load_migration()
    assert module.revision == "0196_runtime_supports_vision"
    assert module.down_revision == "0195_runtime_serving_since"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_migration_upgrade_adds_both_columns_not_null_default_false():
    module, calls = _load_migration()
    module.upgrade()

    added = {(table, col.name): col for (table, col), _kw in calls["add_column"]}
    assert set(added) == {
        ("runtimes", "supports_vision"),
        ("local_recipes", "supports_vision"),
    }
    for col in added.values():
        assert col.nullable is False
        # server_default carries the "false" text clause — additive/safe for
        # every existing row.
        assert col.server_default is not None

    # Der Seed ist ein Namens-/Slug-Muster — keine Gerätedaten (ADR-077 Regel 7).
    seed_sql = "\n".join(a[0] for a, _k in calls["execute"])
    assert "runtimes" in seed_sql and "local_recipes" in seed_sql
    assert "glm53-exl3" in seed_sql
    assert "glm-5.3-flash-exl3" in seed_sql.lower()
    assert "vision" in seed_sql.lower()
    # Sabotage-Probe (Review-Fund 06.09.2026): kein bare "glm53%" mehr — das
    # hätte auch glm53-dflash-sparks (nicht live bewiesen) mitgerissen.
    assert "'glm53%'" not in seed_sql
    # Sabotage-Probe: kein Hostname/keine IP im Seed-Muster.
    assert "192." not in seed_sql
    assert "ssh" not in seed_sql.lower()
    # Die Slot-Zeile folgt der Vision-Fähigkeit ihrer Box (Review-Fund).
    assert "is_slot" in seed_sql


def test_migration_downgrade_drops_both_columns():
    module, calls = _load_migration()
    module.upgrade()
    module.downgrade()

    dropped = {a for a, _k in calls["drop_column"]}
    assert dropped == {("runtimes", "supports_vision"), ("local_recipes", "supports_vision")}


# ── 1b. Seed-Logik, funktional (Review-Fund 06.09.2026) ──────────────────────
#
# Läuft die ECHTE WHERE-Klausel aus der Migrationsdatei gegen eine echte
# SQLite-Tabelle — nicht nur eine String-Prüfung. Einzige Übersetzung:
# ``ILIKE`` → ``LIKE`` (SQLite ist für ASCII per Default schon case-insensitiv
# bei LIKE — identisches Verhalten für unsere reinen ASCII-Muster). Die
# Boolean-Logik der Klausel selbst bleibt unverändert die aus der Migration.


def _sqlite_where(pg_where: str) -> str:
    return pg_where.replace("ILIKE", "LIKE")


def _seed_test_db(rows: list[dict]) -> "sqlite3.Connection":
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE runtimes (
            id TEXT PRIMARY KEY, host_id TEXT, slug TEXT, display_name TEXT,
            model_identifier TEXT, is_slot INTEGER, supports_vision INTEGER
        )
        """
    )
    for row in rows:
        conn.execute(
            "INSERT INTO runtimes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row.get("host_id"), row["slug"], row["display_name"],
                row["model_identifier"], int(row.get("is_slot", False)),
                int(row.get("supports_vision", False)),
            ),
        )
    conn.commit()
    return conn


def _run_seed(conn, module) -> None:
    conn.execute(f"UPDATE runtimes SET supports_vision = 1 WHERE {_sqlite_where(module._SEED_WHERE)}")
    conn.execute(f"UPDATE runtimes SET supports_vision = 1 WHERE {_sqlite_where(module._SLOT_SYNC_WHERE)}")
    conn.commit()


def _vision(conn, slug: str) -> bool:
    row = conn.execute(
        "SELECT supports_vision FROM runtimes WHERE slug = ?", (slug,)
    ).fetchone()
    return bool(row[0])


def test_seed_matches_the_reported_slot_row_directly():
    """Der gemeldete Fall: die Slot-Zeile trägt den Modellnamen selbst schon
    im eigenen model_identifier (Bindestrich-Schreibweise) — die erweiterte
    Namens-Regel muss sie OHNE den Join-Schritt treffen."""
    module, _ = _load_migration()
    conn = _seed_test_db([
        dict(id="1", host_id="h1", slug="dgx-spark-slot", is_slot=True,
             display_name="DGX Spark :8000", model_identifier="org/GLM-5.3-Flash-EXL3"),
    ])
    _run_seed(conn, module)
    assert _vision(conn, "dgx-spark-slot") is True


def test_seed_leaves_dflash_variant_false():
    """Sabotage-Probe: glm53-dflash-sparks ist NICHT live bewiesen und darf
    nicht über ein zu breites 'glm53%'-Muster mitgerissen werden."""
    module, _ = _load_migration()
    conn = _seed_test_db([
        dict(id="1", slug="glm53-dflash-sparks", display_name="GLM-5.3-Flash DFlash Sparks",
             model_identifier="org/GLM-5.3-Flash-DFlash-Sparks"),
    ])
    _run_seed(conn, module)
    assert _vision(conn, "glm53-dflash-sparks") is False


def test_seed_slot_sync_inherits_vision_from_sibling_on_same_host():
    """Eine Slot-Zeile deren EIGENER Name/Slug auf kein Muster passt, muss
    trotzdem vision-fähig werden, wenn eine andere Zeile DERSELBEN Box exakt
    denselben model_identifier bereits als vision-fähig trägt — die Slot-
    Zeile serviert ja genau das, was diese Zeile beschreibt.

    Sabotage-Probe: eine gleichnamige Zeile auf einer ANDEREN Box darf NICHT
    anstecken (host_id-Bedingung)."""
    module, _ = _load_migration()
    conn = _seed_test_db([
        # Bereits vision-fähig (durch eine frühere, hier nicht nachgebildete
        # Regel) — Name/Slug matchen absichtlich KEIN Namensmuster.
        dict(id="1", host_id="box-a", slug="recipe-x-box-a",
             display_name="Recipe X (Box A)", model_identifier="org/custom-engine-9000",
             supports_vision=True),
        # Slot-Zeile derselben Box, gleicher model_identifier, eigener Name
        # trifft kein Muster — muss über den Join true werden.
        dict(id="2", host_id="box-a", slug="box-a-slot", is_slot=True,
             display_name="BOX-A :8000", model_identifier="org/custom-engine-9000"),
        # Gegenfall: gleicher model_identifier, aber ANDERE Box — bleibt false.
        dict(id="3", host_id="box-b", slug="box-b-slot", is_slot=True,
             display_name="BOX-B :8000", model_identifier="org/custom-engine-9000"),
    ])
    _run_seed(conn, module)
    assert _vision(conn, "box-a-slot") is True
    assert _vision(conn, "box-b-slot") is False


async def test_runtime_and_local_recipe_expose_supports_vision_column():
    """SQLModel exposes the field; the live schema (SQLite test DB) too."""
    async with test_engine.connect() as conn:
        rt_cols = {
            c["name"]: c
            for c in await conn.run_sync(lambda sc: sa_inspect(sc).get_columns("runtimes"))
        }
        recipe_cols = {
            c["name"]: c
            for c in await conn.run_sync(
                lambda sc: sa_inspect(sc).get_columns("local_recipes")
            )
        }
    assert rt_cols["supports_vision"]["nullable"] is False
    assert recipe_cols["supports_vision"]["nullable"] is False

    rt = Runtime(slug="fresh-rt", display_name="Fresh", runtime_type="cloud", endpoint="x")
    assert rt.supports_vision is False
    recipe = LocalRecipe(
        slug="fresh-recipe", display_name="Fresh", engine="vllm_docker",
        model_identifier="org/fresh",
    )
    assert recipe.supports_vision is False


# ── 2. build_runtime_env (omp) → OMP_MODEL_INPUT ─────────────────────────────


def _omp_runtime(**over) -> Runtime:
    defaults = dict(
        slug="omp-vision-rt",
        display_name="omp headless (vision test)",
        runtime_type="omp",
        endpoint="http://192.0.2.20:8000/v1",
        model_identifier="org/glm53-exl3",
        enabled=True,
        supports_tools=True,
    )
    defaults.update(over)
    return Runtime(**defaults)


@pytest.mark.asyncio
async def test_build_runtime_env_sets_text_image_when_vision_true(async_session):
    from app.routers.internal import build_runtime_env

    env = await build_runtime_env(_omp_runtime(supports_vision=True), async_session)
    assert env["OMP_MODEL_INPUT"] == "text,image"


@pytest.mark.asyncio
async def test_build_runtime_env_sets_text_only_when_vision_false(async_session):
    """Sabotage-Probe: der sichere Default — ohne Vision-Flag kein 'image'."""
    from app.routers.internal import build_runtime_env

    env = await build_runtime_env(_omp_runtime(supports_vision=False), async_session)
    assert env["OMP_MODEL_INPUT"] == "text"
    assert "image" not in env["OMP_MODEL_INPUT"]


# ── 3. Rezept → Instanz → Slot-Zeile ─────────────────────────────────────────


async def _host(session, slug: str = "box-a", **kw) -> Host:
    fields = dict(display_name=slug.upper(), kind="ssh", ssh_host="192.0.2.10")
    fields.update(kw)
    host = Host(slug=slug, **fields)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


TEMPLATE = (
    "docker run -d --name {container_name} --label mc.runtime.slug={slug} "
    "-p {port}:8000 img"
)


@pytest.mark.asyncio
async def test_build_runtime_from_recipe_copies_supports_vision(async_session):
    host = await _host(async_session)
    recipe = LocalRecipe(
        slug="glm53-exl3", display_name="GLM-5.3-Flash EXL3", engine="vllm_docker",
        model_identifier="org/glm53-exl3", launch_template=TEMPLATE, port=8000,
        context_len=131072, supports_vision=True,
    )
    async_session.add(recipe)
    await async_session.commit()
    await async_session.refresh(recipe)

    instance = await build_runtime_from_recipe(
        async_session, recipe, host, resolved_host_from_row(host)
    )
    assert instance.supports_vision is True


@pytest.mark.asyncio
async def test_build_runtime_from_recipe_defaults_false_for_non_vision_recipe(async_session):
    """Gegenfall: Qwen3.8 Flash Next — kein Vision-Flag im Katalog."""
    host = await _host(async_session, slug="box-b")
    recipe = LocalRecipe(
        slug="qwen38-flash-next", display_name="Qwen3.8 Flash Next", engine="vllm_docker",
        model_identifier="org/qwen38-flash-next", launch_template=TEMPLATE, port=8000,
        context_len=131072,
    )
    async_session.add(recipe)
    await async_session.commit()
    await async_session.refresh(recipe)

    instance = await build_runtime_from_recipe(
        async_session, recipe, host, resolved_host_from_row(host)
    )
    assert instance.supports_vision is False


@pytest.mark.asyncio
async def test_write_slot_state_applies_supports_vision(async_session):
    host = await _host(async_session, slug="box-slot")
    slot = Runtime(
        slug="box-slot-slot", host_id=host.id, display_name="BOX-SLOT :8000",
        runtime_type="openai_compatible", endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/old-recipe", is_slot=True, exclusive_memory=False,
        enabled=True, supports_vision=False,
    )
    async_session.add(slot)
    await async_session.commit()
    await async_session.refresh(slot)

    updated = await slot_runtimes.write_slot_state(
        async_session, host.id, model="org/glm53-exl3", supports_vision=True,
    )
    assert updated is not None
    assert updated.supports_vision is True


@pytest.mark.asyncio
async def test_write_slot_state_leaves_supports_vision_when_not_given(async_session):
    """Sabotage-Probe: ein Aufruf ohne das Argument darf einen gesetzten Wert
    NICHT stumm auf false zurücksetzen."""
    host = await _host(async_session, slug="box-slot-2")
    slot = Runtime(
        slug="box-slot-2-slot", host_id=host.id, display_name="BOX-SLOT-2 :8000",
        runtime_type="openai_compatible", endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/glm53-exl3", is_slot=True, exclusive_memory=False,
        enabled=True, supports_vision=True,
    )
    async_session.add(slot)
    await async_session.commit()
    await async_session.refresh(slot)

    updated = await slot_runtimes.write_slot_state(
        async_session, host.id, model="org/glm53-exl3-v2",
    )
    assert updated is not None
    assert updated.supports_vision is True


# ── 4. API: GET /runtimes, live-status, PATCH ────────────────────────────────


@pytest.mark.asyncio
async def test_list_runtimes_serializes_supports_vision(async_session, auth_client):
    rt = _omp_runtime(slug="list-vision-rt", supports_vision=True)
    async_session.add(rt)
    await async_session.commit()

    body = (await auth_client.get("/api/v1/runtimes")).json()
    row = next(r for r in body["runtimes"] if r["slug"] == "list-vision-rt")
    assert row["supports_vision"] is True


@pytest.mark.asyncio
async def test_patch_runtime_sets_supports_vision(async_session, auth_client):
    rt = _omp_runtime(slug="patch-vision-rt", supports_vision=False)
    async_session.add(rt)
    await async_session.commit()

    resp = await auth_client.patch(
        "/api/v1/runtimes/db/patch-vision-rt", json={"supports_vision": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["supports_vision"] is True

    await async_session.refresh(rt)
    assert rt.supports_vision is True


# ── 5. render-omp-config.sh — models.yml `input:` ────────────────────────────


def _run_render_script(tmp_path: Path, *, model_input: str | None) -> Path:
    if not RENDER_SCRIPT.is_file():
        pytest.fail(f"render-omp-config.sh nicht gefunden unter {RENDER_SCRIPT}")

    home = tmp_path / "home"
    home.mkdir()
    # Never inherit runtime-shaped variables: inside an omp agent container
    # OMP_ENV_FILE points at the agent's REAL omp.env and the script honours it
    # over OMP_HOME (2026-09-07 incident — an omp agent rewrote its own config while
    # running this suite). Start from a scrubbed env and pin every path.
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("OMP_", "OPENAI_", "PI_CODING_AGENT_DIR"))
    }
    env.update(
        HOME=str(home),
        OMP_PROFILE="mc-agent",
        OMP_HOME=str(home / ".omp"),
        OMP_ENV_FILE=str(home / ".omp" / "omp.env"),
        OPENAI_BASE_URL="http://192.0.2.20:8000/v1",
        OPENAI_MODEL="org/glm53-exl3",
    )
    if model_input is not None:
        env["OMP_MODEL_INPUT"] = model_input
    else:
        env.pop("OMP_MODEL_INPUT", None)

    result = subprocess.run(
        [str(RENDER_SCRIPT), "--no-bootstrap"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return home / ".omp" / "profiles" / "mc-agent" / "agent" / "models.yml"


def test_render_script_writes_input_text_image_when_vision_flag_set(tmp_path):
    models_yml = _run_render_script(tmp_path, model_input="text,image")
    content = models_yml.read_text()
    assert "input: [text, image]" in content


def test_render_script_writes_input_text_only_by_default(tmp_path):
    """Sabotage-Probe: ohne OMP_MODEL_INPUT (alte/unbekannte Runtime) bleibt
    der sichere Standard [text] — kein stillschweigendes 'image'."""
    models_yml = _run_render_script(tmp_path, model_input=None)
    content = models_yml.read_text()
    assert "input: [text]" in content
    assert "image" not in content


def test_render_script_writes_input_text_only_when_flag_explicitly_text(tmp_path):
    models_yml = _run_render_script(tmp_path, model_input="text")
    content = models_yml.read_text()
    assert "input: [text]" in content


def test_render_script_never_touches_inherited_omp_env_file(tmp_path, monkeypatch):
    """Sabotage-Probe (Vorfall 07.09.2026): laeuft die Suite in einem omp-Agenten-
    Container, steht OMP_ENV_FILE auf dessen ECHTER omp.env. Der Helfer darf
    diesen Wert nie erben — die Datei muss unangetastet bleiben."""
    live_env = tmp_path / "live-agent" / ".omp" / "omp.env"
    live_env.parent.mkdir(parents=True)
    live_env.write_text("OPENAI_MODEL=GLM-5.3-Flash-EXL3\nHOME=/home/agent\n")
    monkeypatch.setenv("OMP_ENV_FILE", str(live_env))
    monkeypatch.setenv("OMP_TURN_SIGNAL_FILE", str(tmp_path / "live-agent" / "turn-signal.ndjson"))

    models_yml = _run_render_script(tmp_path, model_input="text")

    assert models_yml.is_file()
    assert live_env.read_text() == "OPENAI_MODEL=GLM-5.3-Flash-EXL3\nHOME=/home/agent\n"
    assert (tmp_path / "home" / ".omp" / "omp.env").is_file()
