"""Builtin recipes follow the shipped catalog (services/local_registry.sync_builtin_recipes).

The gap this closes: ``seed_local_recipes`` only inserts, so a fix to a shipped
recipe (a new install template, ``drop_page_cache``, ``gb10_validated``) never
reached an install that already had the row. These tests run the startup pass
against a DB seeded from an OLD catalog and then a NEW one:

  * an untouched builtin row is brought up to the new catalog,
  * a locally edited row stays exactly as it is and is reported once,
  * a recipe that left the catalog keeps its row,
  * an instance whose drop_page_cache came from the recipe follows it,
  * the real catalog is always recorded in local-recipes.shipped.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import local_registry as lr
from app.services.local_registry import (
    EVENT_CATALOG_KEPT_LOCAL,
    RecipeSpec,
    load_shipped_fingerprints,
    row_fingerprint,
    seed_local_recipes,
    spec_fingerprint,
    sync_builtin_recipes,
)

BACKEND = Path(__file__).resolve().parents[1]
REAL_SEED = BACKEND / "config" / "local-recipes.json"


def _recipe(slug: str, **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": f"Recipe {slug}",
        "description": "two boxes, one window",
        "engine": "ssh_process",
        "model_identifier": f"org/{slug}",
        "est_weights_gb": 178,
        "context_len": 1048576,
        "arch": "arm64",
        "gb10_validated": False,
        "install_template": "./prepare.sh",
        "launch_template": "./start.sh",
        "stop_template": "./stop.sh",
        "process_name": f"{slug}-proc",
        "topology": {"nodes": 2, "tp": 2},
        "env_map": {"WORKER": "{worker_ssh}"},
        "drop_page_cache": False,
        "tags": ["duo"],
        "notes": "old notes",
    }
    body.update(overrides)
    return body


V1 = [_recipe("tf-glm"), _recipe("solo-old", topology=None, env_map=None)]
# The fix release: new install step, cache drop on, validated — and solo-old
# is no longer shipped.
V2 = [
    _recipe(
        "tf-glm",
        install_template="./take-back-locks.sh && ./prepare.sh",
        drop_page_cache=True,
        gb10_validated=True,
        notes="validated on 2x GB10",
    )
]


class Catalog:
    """Point the seed at a temp catalog; record shipped versions next to it."""

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "local-recipes.json"
        self.shipped: dict[str, set[str]] = {}

    def ship(self, entries: list[dict]) -> None:
        self.path.write_text(json.dumps(entries), encoding="utf-8")
        for raw in entries:
            self.shipped.setdefault(raw["slug"], set()).add(
                spec_fingerprint(RecipeSpec.model_validate(raw))
            )
        lr._shipped_path().write_text(
            json.dumps({"fingerprints": {k: sorted(v) for k, v in self.shipped.items()}}),
            encoding="utf-8",
        )


@pytest.fixture
def catalog(tmp_path):
    saved = lr._SEED_PATH
    cat = Catalog(tmp_path)
    lr._SEED_PATH = cat.path
    yield cat
    lr._SEED_PATH = saved


async def _row(session, slug: str) -> LocalRecipe | None:
    session.expire_all()
    return (await session.exec(select(LocalRecipe).where(LocalRecipe.slug == slug))).first()


async def _kept_events(session) -> list[ActivityEvent]:
    return list(
        (
            await session.exec(
                select(ActivityEvent).where(ActivityEvent.event_type == EVENT_CATALOG_KEPT_LOCAL)
            )
        ).all()
    )


def _instance(slug: str, recipe_slug: str | None, drop: bool, **extra) -> Runtime:
    topology = {"nodes": 2}
    if recipe_slug:
        topology["recipe_slug"] = recipe_slug
    return Runtime(
        slug=slug,
        display_name=slug,
        runtime_type="ssh_process",
        endpoint="http://box.invalid:8000",
        model_identifier="org/tf-glm",
        topology=topology,
        drop_page_cache=drop,
        **extra,
    )


# ── The fingerprint ──────────────────────────────────────────────────────────


async def test_a_seeded_row_has_the_fingerprint_of_its_catalog_entry(session, catalog):
    """The whole mechanism rests on this: DB round trip changes nothing."""
    catalog.ship(V1)
    await seed_local_recipes(session)
    for raw in V1:
        row = await _row(session, raw["slug"])
        assert row_fingerprint(row) == spec_fingerprint(RecipeSpec.model_validate(raw))


def test_a_new_optional_field_with_its_default_keeps_old_fingerprints(monkeypatch):
    """Fields at their default are left out, so adding a field to the schema
    (the code before it did not know it) changes no fingerprint that was
    shipped before — otherwise every install would look locally edited."""
    spec = RecipeSpec.model_validate(_recipe("x"))
    today = spec_fingerprint(spec)
    older_code = tuple(f for f in lr.MANAGED_FIELDS if f not in ("supports_vision", "exclusive"))
    monkeypatch.setattr(lr, "MANAGED_FIELDS", older_code)
    assert spec_fingerprint(spec) == today
    assert today != spec_fingerprint(RecipeSpec.model_validate(_recipe("x", notes="new")))


def test_enabled_is_not_part_of_the_fingerprint():
    """Hiding a recipe is an operator decision, not an edit of its content."""
    assert spec_fingerprint(RecipeSpec.model_validate(_recipe("x"))) == spec_fingerprint(
        RecipeSpec.model_validate(_recipe("x", enabled=False))
    )


def test_the_real_catalog_is_recorded_as_shipped():
    """Forgetting the record would make the NEXT catalog fix skip every
    install that runs today's version. Fix: python scripts/record_shipped_recipes.py"""
    shipped = load_shipped_fingerprints(REAL_SEED.with_name("local-recipes.shipped.json"))
    specs = lr._parse_entries(json.loads(REAL_SEED.read_text(encoding="utf-8")), "seed", [])
    assert specs, "the real catalog parsed to nothing"
    missing = [s.slug for s in specs if spec_fingerprint(s) not in shipped.get(s.slug, set())]
    assert missing == [], f"run backend/scripts/record_shipped_recipes.py — not recorded: {missing}"


def test_record_script_check_mode_agrees():
    proc = subprocess.run(
        [sys.executable, "scripts/record_shipped_recipes.py", "--check"],
        cwd=BACKEND, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ── The startup pass ─────────────────────────────────────────────────────────


async def test_an_untouched_builtin_row_follows_the_new_catalog(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)

    catalog.ship(V2)
    result = await sync_builtin_recipes(session)

    assert result.updated == ["tf-glm"]
    assert result.kept_local == []
    row = await _row(session, "tf-glm")
    assert row.install_template == "./take-back-locks.sh && ./prepare.sh"
    assert row.drop_page_cache is True
    assert row.gb10_validated is True
    assert row.notes == "validated on 2x GB10"
    assert row_fingerprint(row) == spec_fingerprint(RecipeSpec.model_validate(V2[0]))
    assert await _kept_events(session) == []


async def test_the_sync_keeps_the_operators_hide_decision(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)
    row = await _row(session, "tf-glm")
    row.enabled = False
    session.add(row)
    await session.commit()

    catalog.ship(V2)
    result = await sync_builtin_recipes(session)

    assert result.updated == ["tf-glm"]
    row = await _row(session, "tf-glm")
    assert row.enabled is False
    assert row.gb10_validated is True


async def test_a_second_pass_changes_nothing(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)
    catalog.ship(V2)
    await sync_builtin_recipes(session)
    stamp = (await _row(session, "tf-glm")).updated_at

    again = await sync_builtin_recipes(session)

    assert again.updated == [] and again.kept_local == []
    assert (await _row(session, "tf-glm")).updated_at == stamp


async def test_a_locally_edited_row_is_kept_and_reported_once(session, catalog, caplog):
    catalog.ship(V1)
    await seed_local_recipes(session)
    row = await _row(session, "tf-glm")
    row.launch_template = "./start.sh --my-own-flag"
    session.add(row)
    await session.commit()

    catalog.ship(V2)
    with caplog.at_level("WARNING", logger="mc.local_registry"):
        first = await sync_builtin_recipes(session)
        second = await sync_builtin_recipes(session)

    assert first.kept_local == ["tf-glm"] and first.updated == []
    assert second.kept_local == ["tf-glm"]
    row = await _row(session, "tf-glm")
    assert row.launch_template == "./start.sh --my-own-flag"
    assert row.install_template == "./prepare.sh"  # NOT the catalog's new one
    assert row.drop_page_cache is False
    assert "catalog has a newer version, local edits kept" in caplog.text

    events = await _kept_events(session)
    assert len(events) == 1, "one event per catalog version, not one per boot"
    assert events[0].detail["slug"] == "tf-glm"
    assert "local edits kept" in events[0].title


async def test_a_newer_catalog_version_reports_a_kept_row_again(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)
    row = await _row(session, "tf-glm")
    row.display_name = "my name"
    session.add(row)
    await session.commit()

    catalog.ship(V2)
    await sync_builtin_recipes(session)
    catalog.ship([_recipe("tf-glm", notes="v3")])
    await sync_builtin_recipes(session)

    assert len(await _kept_events(session)) == 2


async def test_a_recipe_that_left_the_catalog_keeps_its_row(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)

    catalog.ship(V2)  # solo-old is gone
    await sync_builtin_recipes(session)

    gone = await _row(session, "solo-old")
    assert gone is not None
    assert gone.notes == "old notes"


async def test_a_registry_owned_row_is_not_touched(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)
    row = await _row(session, "tf-glm")
    row.source_registry = "community-registry"
    session.add(row)
    await session.commit()

    catalog.ship(V2)
    result = await sync_builtin_recipes(session)

    assert result.updated == [] and result.kept_local == []
    assert (await _row(session, "tf-glm")).install_template == "./prepare.sh"


async def test_without_a_shipped_record_nothing_is_overwritten(session, catalog):
    """No record (or an unreadable one) = every difference counts as local."""
    catalog.ship(V1)
    await seed_local_recipes(session)
    catalog.ship(V2)
    lr._shipped_path().write_text("{not json", encoding="utf-8")

    result = await sync_builtin_recipes(session)

    assert result.updated == []
    assert result.kept_local == ["tf-glm"]
    assert (await _row(session, "tf-glm")).drop_page_cache is False


# ── Instances follow drop_page_cache ─────────────────────────────────────────


async def test_an_instance_follows_drop_page_cache_from_its_recipe(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)
    session.add(_instance("tf-glm-a", "tf-glm", drop=False))  # copied from the recipe
    session.add(_instance("tf-glm-hand", "tf-glm", drop=True))  # already differs: hand-set
    session.add(_instance("other-recipe", "something-else", drop=False))
    session.add(_instance("no-backlink", None, drop=False))  # same model, no instance link
    session.add(_instance("slot-a", "tf-glm", drop=False, is_slot=True))
    await session.commit()

    catalog.ship(V2)
    result = await sync_builtin_recipes(session)

    assert result.runtimes_followed == ["tf-glm-a"]
    session.expire_all()
    by_slug = {rt.slug: rt for rt in (await session.exec(select(Runtime))).all()}
    assert by_slug["tf-glm-a"].drop_page_cache is True
    assert by_slug["tf-glm-hand"].drop_page_cache is True
    assert by_slug["other-recipe"].drop_page_cache is False
    assert by_slug["no-backlink"].drop_page_cache is False
    assert by_slug["slot-a"].drop_page_cache is False


async def test_an_instance_of_a_kept_local_recipe_does_not_move(session, catalog):
    catalog.ship(V1)
    await seed_local_recipes(session)
    row = await _row(session, "tf-glm")
    row.notes = "mine"
    session.add(row)
    session.add(_instance("tf-glm-a", "tf-glm", drop=False))
    await session.commit()

    catalog.ship(V2)
    await sync_builtin_recipes(session)

    session.expire_all()
    rt = (await session.exec(select(Runtime).where(Runtime.slug == "tf-glm-a"))).first()
    assert rt.drop_page_cache is False


# ── Startup wiring ───────────────────────────────────────────────────────────


async def test_the_startup_seed_runs_the_sync(session, catalog, monkeypatch):
    """Boot path, not just the function: app.seeds._seed_local_recipes."""
    from app import seeds
    from tests.conftest import test_engine

    monkeypatch.setattr(seeds, "engine", test_engine)
    catalog.ship(V1)
    await seeds._seed_local_recipes()
    catalog.ship(V2)
    await seeds._seed_local_recipes()

    assert (await _row(session, "tf-glm")).gb10_validated is True


async def test_a_registry_refresh_carries_drop_page_cache_into_instances(
    session, catalog, monkeypatch, fake_redis
):
    """Same rule on the other write path: a registry that flips the flag."""
    from tests.test_local_registry import json_source, mock_httpx, use_fake_redis, use_sources

    use_fake_redis(monkeypatch, fake_redis)
    entry = _recipe("tf-glm", source_registry="community")
    use_sources(monkeypatch, "https://registry.invalid/recipes.json")
    mock_httpx(monkeypatch, json_source(entry))
    await lr.refresh_from_sources(session)
    session.add(_instance("tf-glm-a", "tf-glm", drop=False))
    await session.commit()

    mock_httpx(monkeypatch, json_source({**entry, "drop_page_cache": True}))
    await lr.refresh_from_sources(session)

    session.expire_all()
    rt = (await session.exec(select(Runtime).where(Runtime.slug == "tf-glm-a"))).first()
    assert rt.drop_page_cache is True


# ── Short text per UI language (summary) ─────────────────────────────────────


def test_the_tensorfold_recipe_ships_a_short_text_in_english_and_german():
    """The deploy dialog shows this in the UI's language; the long English
    description stays under Details (never mix UI languages)."""
    entry = next(
        e for e in json.loads(REAL_SEED.read_text(encoding="utf-8"))
        if e["slug"] == "glm53-flash-exl3-tensorfold"
    )
    summary = RecipeSpec.model_validate(entry).summary
    assert set(summary) == {"en", "de"}
    for lang, text in summary.items():
        assert 0 < len(text) <= 360, lang
        assert "CC BY-NC-ND" in text, f"{lang}: the drafter licence must be named"
    assert "nicht-kommerziell" in summary["de"]
    assert "Non-commercial" in summary["en"]


def test_an_older_mc_ignores_summary():
    """RecipeSpec drops unknown keys — that is what lets a newer catalog
    reach an older MC. Shown here for the inverse: the key parses today."""
    spec = RecipeSpec.model_validate(_recipe("x", summary={"en": "a", "de": "b"}, future_key=1))
    assert spec.summary == {"en": "a", "de": "b"}


async def test_the_list_endpoint_serves_summary_for_builtin_rows_only(auth_client, session, catalog):
    catalog.ship([
        _recipe("tf-glm", summary={"en": "Two boxes.", "de": "Zwei Boxen."}),
        _recipe("no-summary"),
        _recipe("from-registry", summary={"en": "Catalog text.", "de": "Katalogtext."}),
    ])
    await seed_local_recipes(session)
    # A registry refresh took this slug over: its text is the registry's
    # business, not the shipped catalog's.
    taken = await _row(session, "from-registry")
    taken.source_registry = "community"
    session.add(taken)
    await session.commit()

    body = (await auth_client.get("/api/v1/local-registry")).json()
    by_slug = {r["slug"]: r for r in body["recipes"]}
    assert by_slug["tf-glm"]["summary"] == {"en": "Two boxes.", "de": "Zwei Boxen."}
    assert by_slug["no-summary"]["summary"] is None
    assert by_slug["from-registry"]["summary"] is None


# ── The real case (review of PR #860) ────────────────────────────────────────

BEFORE_FIX = Path(__file__).resolve().parent / "fixtures" / "local-recipes-tensorfold-2026-10-18-before-fix.json"


async def test_an_install_with_the_old_tensorfold_row_gets_the_fixed_recipe(session, monkeypatch):
    """An install seeded from the catalog before the TensorFold fix (no HF
    lock take-back, drop_page_cache=false, gb10_validated=false) and with an
    instance built from it: one startup later both carry the fix."""
    slug = "glm53-flash-exl3-tensorfold"
    monkeypatch.setattr(lr, "_SEED_PATH", BEFORE_FIX)
    await seed_local_recipes(session)
    old = await _row(session, slug)
    assert old.drop_page_cache is False and old.gb10_validated is False
    session.add(_instance("glm53-flash-exl3-tensorfold-a", slug, drop=False))
    await session.commit()

    monkeypatch.setattr(lr, "_SEED_PATH", REAL_SEED)
    result = await sync_builtin_recipes(session)

    assert result.updated == [slug]
    assert result.runtimes_followed == ["glm53-flash-exl3-tensorfold-a"]
    row = await _row(session, slug)
    current = next(
        e for e in json.loads(REAL_SEED.read_text(encoding="utf-8")) if e["slug"] == slug
    )
    assert row.install_template == current["install_template"]
    assert row.drop_page_cache is current["drop_page_cache"] is True
    assert row.gb10_validated is current["gb10_validated"] is True
    session.expire_all()
    rt = (await session.exec(select(Runtime).where(Runtime.slug == "glm53-flash-exl3-tensorfold-a"))).first()
    assert rt.drop_page_cache is True
