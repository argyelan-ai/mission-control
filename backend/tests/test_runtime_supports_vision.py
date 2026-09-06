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
    assert "glm53" in seed_sql
    assert "vision" in seed_sql.lower()
    # Sabotage-Probe: kein Hostname/keine IP im Seed-Muster.
    assert "192." not in seed_sql
    assert "ssh" not in seed_sql.lower()


def test_migration_downgrade_drops_both_columns():
    module, calls = _load_migration()
    module.upgrade()
    module.downgrade()

    dropped = {a for a, _k in calls["drop_column"]}
    assert dropped == {("runtimes", "supports_vision"), ("local_recipes", "supports_vision")}


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
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        OMP_PROFILE="mc-agent",
        OMP_HOME=str(home / ".omp"),
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
