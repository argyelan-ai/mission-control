"""Local Model Registry (services/local_registry.py + routers/local_registry.py).

No network: every source fetch runs through an httpx.MockTransport, the same
idiom as tests/test_model_catalog_check.py. Redis is fakeredis, the DB is the
in-memory async session.

The properties that matter operationally:
  * seeding twice changes nothing (a deploy may not undo a curated edit),
  * a refresh adds new entries, updates changed ones — and NEVER re-enables one
    the operator hid,
  * a broken source degrades to a reason string instead of an exception,
  * a genuinely new recipe notifies exactly once, and only after a successful
    fetch,
  * the endpoints filter, refresh and toggle,
  * the new migration keeps the chain at exactly one head.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from app.config import settings
from app.models.activity import ActivityEvent
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import local_registry
from app.services import sse as sse_mod
from app.services.local_registry import (
    EVENT_NEW_LOCAL_MODEL,
    LocalRegistryChecker,
    RefreshResult,
    refresh_from_sources,
    registry_sources,
    seed_local_recipes,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
SEED_FILE = Path(__file__).resolve().parents[1] / "config" / "local-recipes.json"


# ── Helpers ──────────────────────────────────────────────────────────────────


def recipe_payload(slug="test-model", **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": f"Test {slug}",
        "description": "a test recipe",
        "engine": "vllm_docker",
        "model_identifier": f"org/{slug}",
        "quant": "nvfp4",
        "est_weights_gb": 12.0,
        "min_vram_gb": 24.0,
        "context_len": 32768,
        "arch": "any",
        "gb10_validated": False,
        "source_registry": "test-registry",
        "tags": ["testing"],
    }
    body.update(overrides)
    return body


def use_sources(monkeypatch, *urls: str) -> None:
    monkeypatch.setattr(settings, "local_registry_sources", ",".join(urls))
    monkeypatch.setattr(local_registry.settings, "local_registry_sources", ",".join(urls))


#: The pristine class, captured at import time. A test that calls mock_httpx
#: twice would otherwise wrap the previous fake and keep serving the FIRST
#: payload — which silently turned an "update" assertion into a no-op.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_httpx(monkeypatch, handler):
    """Route every local_registry HTTP call through `handler`; collect requests."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(local_registry.httpx, "AsyncClient", fake_async_client)
    return seen


def use_fake_redis(monkeypatch, fake_redis):
    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(local_registry, "get_redis", _get_redis)
    # emit_event fans out over SSE, which opens its own Redis connection.
    monkeypatch.setattr(sse_mod, "get_redis", _get_redis)


def json_source(*entries: dict):
    return lambda request: httpx.Response(200, json=list(entries))


async def all_recipes(session) -> list[LocalRecipe]:
    return list((await session.exec(select(LocalRecipe))).all())


async def get_recipe(session, slug: str) -> LocalRecipe | None:
    return (
        await session.exec(select(LocalRecipe).where(LocalRecipe.slug == slug))
    ).first()


async def new_model_events(session) -> list[ActivityEvent]:
    return list(
        (
            await session.exec(
                select(ActivityEvent).where(
                    ActivityEvent.event_type == EVENT_NEW_LOCAL_MODEL
                )
            )
        ).all()
    )


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_settings_and_redis_keys_exist():
    # Empty by default: without a configured source MC makes no outbound
    # request for this feature at all.
    assert settings.local_registry_sources == ""
    assert settings.local_registry_check_interval == 21600
    assert RedisKeys.local_registry_check_lock() == "mc:local-registry:check-lock"
    assert (
        RedisKeys.local_registry_notified("laguna-s21-nvfp4")
        == "mc:local-registry:notified:laguna-s21-nvfp4"
    )


def test_registry_sources_parses_and_trims(monkeypatch):
    assert registry_sources() == []
    use_sources(monkeypatch, " https://a.example/r.json ", "https://b.example/r.json")
    assert registry_sources() == ["https://a.example/r.json", "https://b.example/r.json"]


def test_seed_file_is_public_safe_and_valid():
    """The seed ships in a PUBLIC repo — no private hosts, no home paths."""
    raw = SEED_FILE.read_text(encoding="utf-8")
    entries = json.loads(raw)
    assert len(entries) >= 8
    for forbidden in ("192" ".168.", "100" ".", "/Users" "/", "tail", "ssh"):  # split literals: der Leak-Scanner soll den Seed treffen, nicht diesen Test
        assert forbidden not in raw, f"seed leaks {forbidden!r}"
    slugs = [e["slug"] for e in entries]
    assert len(slugs) == len(set(slugs)), "duplicate slug in seed"


# ── Seeding ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_inserts_the_builtin_catalogue(session):
    inserted, skipped = await seed_local_recipes(session)

    assert inserted >= 8
    assert skipped == 0
    rows = await all_recipes(session)
    assert len(rows) == inserted
    laguna = await get_recipe(session, "laguna-s21-nvfp4")
    assert laguna is not None
    assert laguna.engine == "sparkrun"
    assert laguna.recipe_ref == "@mark/laguna-s21-nvfp4-vllm"
    assert laguna.gb10_validated is True
    assert laguna.est_weights_gb == 71.0
    assert laguna.source_registry == "builtin"


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_never_overwrites(session):
    """A deploy may not undo a curated edit — the file is seed-only."""
    inserted_first, _ = await seed_local_recipes(session)

    recipe = await get_recipe(session, "laguna-s21-nvfp4")
    recipe.display_name = "Hand-edited by the operator"
    recipe.enabled = False
    session.add(recipe)
    await session.commit()

    inserted_second, skipped_second = await seed_local_recipes(session)

    assert inserted_second == 0
    assert skipped_second == inserted_first
    assert len(await all_recipes(session)) == inserted_first
    again = await get_recipe(session, "laguna-s21-nvfp4")
    assert again.display_name == "Hand-edited by the operator"
    assert again.enabled is False


def test_timestamps_are_timezone_aware():
    """Naive datetimes meeting aware ones is a recurring 500 in this codebase.
    Asserted on the Python default, not on a round-tripped row: the test DB is
    SQLite, which silently drops tzinfo where PostgreSQL keeps it (timestamptz).
    """
    row = LocalRecipe(slug="x", display_name="X", engine="vllm_docker", model_identifier="o/x")
    assert row.first_seen_at.tzinfo is not None
    assert row.updated_at.tzinfo is not None


# ── Refresh: upsert semantics ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_without_sources_is_a_clean_noop(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    seen = mock_httpx(monkeypatch, json_source(recipe_payload()))

    result = await refresh_from_sources(session)

    assert isinstance(result, RefreshResult)
    assert (result.fetched, result.added, result.updated, result.failed) == (0, 0, 0, 0)
    assert "no sources configured" in result.reasons[0]
    assert seen == []  # no outbound request at all


@pytest.mark.asyncio
async def test_refresh_adds_new_entries(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, json_source(recipe_payload("alpha"), recipe_payload("beta")))

    result = await refresh_from_sources(session)

    assert (result.fetched, result.added, result.updated, result.failed) == (1, 2, 0, 0)
    slugs = sorted(r.slug for r in await all_recipes(session))
    assert slugs == ["alpha", "beta"]
    alpha = await get_recipe(session, "alpha")
    assert alpha.source_registry == "test-registry"
    assert alpha.min_vram_gb == 24.0


@pytest.mark.asyncio
async def test_refresh_updates_changed_fields_only(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, json_source(recipe_payload("alpha")))
    await refresh_from_sources(session)
    first_seen = (await get_recipe(session, "alpha")).first_seen_at

    mock_httpx(
        monkeypatch,
        json_source(recipe_payload("alpha", display_name="Alpha v2", est_weights_gb=20.0)),
    )
    second = await refresh_from_sources(session)

    assert (second.added, second.updated) == (0, 1)
    alpha = await get_recipe(session, "alpha")
    assert alpha.display_name == "Alpha v2"
    assert alpha.est_weights_gb == 20.0
    # first_seen_at is the day MC learned of it, not the day it last changed.
    assert alpha.first_seen_at == first_seen

    third = await refresh_from_sources(session)
    assert (third.added, third.updated) == (0, 0)  # nothing moved → no write


@pytest.mark.asyncio
async def test_refresh_never_re_enables_a_hidden_entry(session, monkeypatch, fake_redis):
    """enabled=False is the operator's decision. A source claiming enabled=True
    must not undo it — otherwise every refresh un-hides what was hidden."""
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, json_source(recipe_payload("alpha")))
    await refresh_from_sources(session)

    alpha = await get_recipe(session, "alpha")
    alpha.enabled = False
    session.add(alpha)
    await session.commit()

    mock_httpx(
        monkeypatch,
        json_source(recipe_payload("alpha", display_name="Alpha v2", enabled=True)),
    )
    await refresh_from_sources(session)

    alpha = await get_recipe(session, "alpha")
    assert alpha.enabled is False  # still hidden
    assert alpha.display_name == "Alpha v2"  # but otherwise updated


@pytest.mark.asyncio
async def test_refresh_never_deletes_vanished_entries(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, json_source(recipe_payload("alpha"), recipe_payload("beta")))
    await refresh_from_sources(session)

    mock_httpx(monkeypatch, json_source(recipe_payload("alpha")))  # beta gone
    await refresh_from_sources(session)

    slugs = sorted(r.slug for r in await all_recipes(session))
    assert slugs == ["alpha", "beta"]


# ── Refresh: failure isolation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broken_source_degrades_with_a_reason(session, monkeypatch, fake_redis):
    """Three ways to be broken; none may raise, all must be nameable."""
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(
        monkeypatch,
        "https://down.example/r.json",
        "https://http500.example/r.json",
        "https://notjson.example/r.json",
        "https://good.example/r.json",
    )

    def route(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "down." in url:
            raise httpx.ConnectError("connection refused", request=request)
        if "http500." in url:
            return httpx.Response(500, text="boom")
        if "notjson." in url:
            return httpx.Response(200, text="<html>nope</html>")
        return httpx.Response(200, json=[recipe_payload("alpha")])

    mock_httpx(monkeypatch, route)

    result = await refresh_from_sources(session)  # must not raise

    assert result.failed == 3
    assert result.fetched == 1
    assert result.added == 1  # the healthy source still landed
    joined = " | ".join(result.reasons)
    assert "unreachable" in joined
    assert "HTTP 500" in joined
    assert "not JSON" in joined


@pytest.mark.asyncio
async def test_invalid_entry_is_skipped_not_fatal(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(
        monkeypatch,
        json_source(
            recipe_payload("alpha"),
            {"slug": "no-name-no-engine"},  # fails schema validation
            recipe_payload("weird-engine", engine="tensorrt_magic"),  # bad vocabulary
            recipe_payload("beta"),
        ),
    )

    result = await refresh_from_sources(session)

    assert result.added == 2
    assert sorted(r.slug for r in await all_recipes(session)) == ["alpha", "beta"]
    joined = " | ".join(result.reasons)
    assert "no-name-no-engine" in joined
    assert "unknown engine" in joined


@pytest.mark.asyncio
async def test_non_array_payload_counts_as_failure(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"recipes": []}))

    result = await refresh_from_sources(session)

    assert result.failed == 1
    assert result.added == 0
    assert await all_recipes(session) == []


# ── Notification ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_recipe_emits_exactly_one_event_once(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, json_source(recipe_payload("alpha")))

    first = await refresh_from_sources(session)
    assert first.notified == ["alpha"]

    events = await new_model_events(session)
    assert len(events) == 1
    assert events[0].severity == "info"  # info → no Discord push (anti-storm)
    assert events[0].detail["slug"] == "alpha"
    assert "Test alpha" in events[0].title

    # Dedup lives in Redis, so this holds across a backend restart too.
    second = await refresh_from_sources(session)
    assert second.notified == []
    assert len(await new_model_events(session)) == 1
    ttl = await fake_redis.ttl(RedisKeys.local_registry_notified("alpha"))
    assert 0 < ttl <= local_registry._NOTIFIED_TTL


@pytest.mark.asyncio
async def test_seeding_alone_never_notifies(session, monkeypatch, fake_redis):
    """The builtin seed ships with the deploy — announcing eight models on
    every fresh install is noise, not news."""
    use_fake_redis(monkeypatch, fake_redis)

    await seed_local_recipes(session)

    assert await new_model_events(session) == []
    assert not await fake_redis.exists(
        RedisKeys.local_registry_notified("laguna-s21-nvfp4")
    )


@pytest.mark.asyncio
async def test_failed_fetch_notifies_nothing(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://down.example/r.json")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_httpx(monkeypatch, boom)

    result = await refresh_from_sources(session)

    assert result.failed == 1
    assert result.notified == []
    assert await new_model_events(session) == []


@pytest.mark.asyncio
async def test_burst_collapses_into_one_summary_event(session, monkeypatch, fake_redis):
    """First refresh against a fresh registry: everything looks new at once.
    One line — but nothing may be lost either."""
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    slugs = [f"model-{n}" for n in range(6)]
    mock_httpx(monkeypatch, json_source(*[recipe_payload(s) for s in slugs]))

    await refresh_from_sources(session)

    events = await new_model_events(session)
    assert len(events) == 1
    assert events[0].detail["count"] == 6
    assert sorted(events[0].detail["slugs"]) == sorted(slugs)
    for slug in slugs:
        assert await fake_redis.exists(RedisKeys.local_registry_notified(slug))


@pytest.mark.asyncio
async def test_redis_dedup_unavailable_stays_silent(session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, json_source(recipe_payload("alpha")))

    class _Broken:
        async def set(self, *_a, **_kw):
            raise ConnectionError("redis down")

    async def _broken_redis():
        return _Broken()

    monkeypatch.setattr(local_registry, "get_redis", _broken_redis)

    result = await refresh_from_sources(session)

    assert result.added == 1  # the data still lands
    assert result.notified == []  # but nothing is announced
    assert await new_model_events(session) == []


# ── Background loop ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_disabled_when_interval_zero(monkeypatch, fake_redis):
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    checker = LocalRegistryChecker(interval=0)
    use_fake_redis(monkeypatch, fake_redis)

    await checker.start()

    assert checker._task is None
    await checker.stop()  # no-op, must not raise


@pytest.mark.asyncio
async def test_loop_stays_idle_without_sources(monkeypatch, fake_redis):
    """Default install: nothing configured → no task, no HTTP, no surprises."""
    checker = LocalRegistryChecker(interval=21600)
    use_fake_redis(monkeypatch, fake_redis)

    await checker.start()

    assert checker._task is None
    await checker.stop()


@pytest.mark.asyncio
async def test_lock_prevents_concurrent_tick(monkeypatch, fake_redis):
    checker = LocalRegistryChecker(interval=21600)
    use_fake_redis(monkeypatch, fake_redis)

    assert await checker._acquire_lock() is True
    assert await checker._acquire_lock() is False


# ── Endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_endpoint_returns_sorted_catalogue(auth_client, session):
    await seed_local_recipes(session)

    response = await auth_client.get("/api/v1/local-registry")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 8
    names = [r["display_name"] for r in body["recipes"]]
    assert names == sorted(names, key=str.lower)
    assert body["sources"] == []
    laguna = next(r for r in body["recipes"] if r["slug"] == "laguna-s21-nvfp4")
    assert laguna["engine"] == "sparkrun"
    assert laguna["gb10_validated"] is True
    assert laguna["running"] is False  # nothing is serving it in this fixture


@pytest.mark.asyncio
async def test_list_endpoint_filters(auth_client, session):
    await seed_local_recipes(session)

    engines = await auth_client.get("/api/v1/local-registry?engine=sparkrun")
    assert {r["engine"] for r in engines.json()["recipes"]} == {"sparkrun"}

    # arm64 is inclusive: an `any` recipe DOES run on an arm64 box.
    arm = await auth_client.get("/api/v1/local-registry?arch=arm64")
    assert {r["arch"] for r in arm.json()["recipes"]} == {"arm64", "any"}

    only_any = await auth_client.get("/api/v1/local-registry?arch=any")
    assert {r["arch"] for r in only_any.json()["recipes"]} == {"any"}

    search = await auth_client.get("/api/v1/local-registry?q=embedding")
    slugs = [r["slug"] for r in search.json()["recipes"]]
    assert slugs == ["qwen3-embedding-0.6b"]


@pytest.mark.asyncio
async def test_list_endpoint_marks_running_recipes(auth_client, session):
    """`running` is derived from the runtime rows at read time — the registry
    itself never claims to know what is live."""
    await seed_local_recipes(session)
    session.add(
        Runtime(
            slug="spark-vllm",
            display_name="Spark vLLM",
            runtime_type="vllm_docker",
            endpoint="http://192.0.2.10:8000/v1",
            launch_command="sparkrun run @mark/laguna-s21-nvfp4-vllm --solo",
        )
    )
    session.add(
        Runtime(
            slug="small-box",
            display_name="Small box",
            runtime_type="vllm_docker",
            endpoint="http://192.0.2.11:8000/v1",
            model_identifier="unsloth/Qwen3.6-27B-NVFP4",
        )
    )
    await session.commit()

    body = (await auth_client.get("/api/v1/local-registry")).json()
    running = {r["slug"] for r in body["recipes"] if r["running"]}

    # recipe_ref match (via launch_command) and model_identifier match.
    assert running == {"laguna-s21-nvfp4", "qwen36-27b-nvfp4"}


@pytest.mark.asyncio
async def test_refresh_endpoint_returns_the_result(auth_client, session, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://registry.example/recipes.json")
    mock_httpx(monkeypatch, json_source(recipe_payload("alpha")))

    response = await auth_client.post("/api/v1/local-registry/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 1
    assert body["fetched"] == 1
    assert body["failed"] == 0
    assert (await auth_client.get("/api/v1/local-registry")).json()["total"] == 1


@pytest.mark.asyncio
async def test_refresh_endpoint_reports_a_broken_source_as_200(
    auth_client, session, monkeypatch, fake_redis
):
    """A down registry is information, not an error page — the operator needs
    to see WHICH source failed."""
    use_fake_redis(monkeypatch, fake_redis)
    use_sources(monkeypatch, "https://down.example/r.json")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    mock_httpx(monkeypatch, boom)

    response = await auth_client.post("/api/v1/local-registry/refresh")

    assert response.status_code == 200
    assert response.json()["failed"] == 1
    assert "unreachable" in " ".join(response.json()["reasons"])


@pytest.mark.asyncio
async def test_patch_endpoint_toggles_enabled(auth_client, session):
    await seed_local_recipes(session)

    response = await auth_client.patch(
        "/api/v1/local-registry/qwen3-8b-gguf-q4", json={"enabled": False}
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False

    hidden = (await auth_client.get("/api/v1/local-registry?enabled=false")).json()
    assert [r["slug"] for r in hidden["recipes"]] == ["qwen3-8b-gguf-q4"]

    back = await auth_client.patch(
        "/api/v1/local-registry/qwen3-8b-gguf-q4", json={"enabled": True}
    )
    assert back.json()["enabled"] is True


@pytest.mark.asyncio
async def test_patch_unknown_slug_is_404(auth_client, session):
    response = await auth_client.patch(
        "/api/v1/local-registry/does-not-exist", json={"enabled": False}
    )
    assert response.status_code == 404


# ── Migration guard ──────────────────────────────────────────────────────────


def _revisions() -> list[tuple[str, tuple[str, ...]]]:
    """(revision, parents) per migration file — ast, so merge tuples survive."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for f in sorted(MIGRATIONS.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rev: str | None = None
        parents: tuple[str, ...] = ()
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                targets = [node.target] if node.value is not None else []
            elif isinstance(node, ast.Assign):
                targets = node.targets
            else:
                continue
            for target in targets:
                name = getattr(target, "id", None)
                if name == "revision":
                    rev = ast.literal_eval(node.value)
                elif name == "down_revision":
                    val = ast.literal_eval(node.value)
                    parents = () if val is None else (val,) if isinstance(val, str) else tuple(val)
        if rev is not None:
            out.append((rev, parents))
    return out


def test_migration_0175_is_the_single_head():
    """Two heads make `alembic upgrade head` refuse to run — that has broken a
    real deploy before (see tests/test_alembic_chain_integrity.py)."""
    entries = _revisions()
    revs = {r for r, _ in entries}
    referenced = {p for _, parents in entries for p in parents}
    heads = sorted(revs - referenced)

    assert "0176_local_recipes" in revs
    assert heads == ["0176_local_recipes"], f"expected one head, got {heads}"
    parents = dict(entries)["0176_local_recipes"]
    assert parents == ("0175_app_settings",)
    assert len("0176_local_recipes") <= 32  # alembic_version.version_num is varchar(32)
