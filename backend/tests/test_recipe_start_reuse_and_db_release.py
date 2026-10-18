"""Recipe start: reuse the existing instance row and never hold a DB
transaction across the long box steps.

Field findings from a real two-box deploy (2026-10-18):

1. "Create runtime & start" made a SECOND instance row (``…-2``) although
   one for the same recipe on the same box already existed. Cause: the
   lookup only saw ``enabled`` rows, and the existing one was paused
   (``runtimes.enabled = false`` — the documented way to keep the watcher
   away during a hand test). A paused row is now found; the start refuses
   with a sentence instead of duplicating it, and only an explicit
   ``resume_paused`` turns it back on (with an activity event).
2. The backend warned "DB connection held 32.4s … OPEN transaction" on
   ``POST /hosts/{id}/recipes/{slug}/start``: the request session sat in an
   autobegun transaction while MC talked SSH to the boxes. Every long step
   (engine probe, SSH reachability, worker eviction, .env write, the start
   itself) now runs with no open transaction on the caller's session, and
   the eviction's own session is closed before it stops anything.

Test data: box-a / box-b / recipe-x. No network, no device data.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import recipe_switcher

TEMPLATE = "docker run -d --name {container_name} --label mc.runtime.slug={slug} -p {port}:8000 img"
ENV_FILE = "~/rezept/.env"
ENV_MAP = {"HEAD_IP": "{head_fabric_ip}", "WORKER_IP": "{worker_fabric_ip}"}


# ── Setup ────────────────────────────────────────────────────────────────────


async def _host(session: AsyncSession, slug: str, *, ssh_host: str = "192.0.2.10", **kw) -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind="ssh", ssh_host=ssh_host, **kw)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, slug: str = "recipe-x", **kw) -> LocalRecipe:
    fields = dict(
        display_name=slug,
        engine="vllm_docker",
        model_identifier=f"org/{slug}",
        launch_template=TEMPLATE,
        port=8000,
    )
    fields.update(kw)
    recipe = LocalRecipe(slug=slug, **fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


async def _duo_recipe(session: AsyncSession, slug: str = "recipe-duo") -> LocalRecipe:
    return await _recipe(
        session, slug, topology={"nodes": 2}, env_file=ENV_FILE, env_map=dict(ENV_MAP),
        min_vram_gb=100.0,
    )


async def _instance(
    session: AsyncSession, recipe: LocalRecipe, host: Host, *, enabled: bool, nodes: int = 1
) -> Runtime:
    """An instance row exactly as ``build_runtime_from_recipe`` writes it."""
    rt = Runtime(
        slug=f"{recipe.slug}-{host.slug}",
        display_name=recipe.display_name,
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier=recipe.model_identifier,
        launch_command="docker run --label mc.runtime.slug=x img",
        exclusive_memory=True,
        host_id=host.id,
        topology={"nodes": nodes, "recipe_slug": recipe.slug},
        enabled=enabled,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _probe(running: set[str]):
    async def _fake(runtime: Runtime, **kw) -> bool:
        return runtime.slug in running

    return patch("app.services.recipe_switcher.probe_running", _fake)


# ── 1. A paused instance is found, not duplicated ────────────────────────────


@pytest.mark.asyncio
async def test_a_paused_instance_is_refused_with_a_sentence_not_duplicated(auth_client, session):
    box = await _host(session, "box-a")
    recipe = await _recipe(session)
    paused = await _instance(session, recipe, box, enabled=False)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box.id}/recipes/recipe-x/start")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "runtime_paused"
    assert detail["runtime_slug"] == paused.slug
    assert detail["runtime_id"] == str(paused.id)
    assert paused.slug in detail["message"]
    start.assert_not_awaited()
    rows = (await session.exec(select(Runtime))).all()
    assert [r.slug for r in rows] == [paused.slug]
    await session.refresh(paused)
    assert paused.enabled is False  # never switched back on silently


@pytest.mark.asyncio
async def test_resume_paused_reuses_the_row_and_says_so(auth_client, session):
    box = await _host(session, "box-a")
    recipe = await _recipe(session)
    paused = await _instance(session, recipe, box, enabled=False)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(
            f"/api/v1/hosts/{box.id}/recipes/recipe-x/start", json={"resume_paused": True}
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runtime_slug"] == paused.slug
    assert body["created"] is False
    assert body["resumed"] is True
    start.assert_awaited_once()
    rows = (await session.exec(select(Runtime))).all()
    assert len(rows) == 1
    await session.refresh(paused)
    assert paused.enabled is True
    events = (
        await session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "runtime.resumed_by_recipe_start")
        )
    ).all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_a_refused_resume_leaves_the_row_paused(auth_client, session):
    """Resume happens only once every offline check passed — a start that is
    refused (here: no second box for a two-box recipe) leaves the pause alone."""
    box = await _host(session, "box-a")
    recipe = await _duo_recipe(session)
    paused = await _instance(session, recipe, box, enabled=False, nodes=2)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(
            f"/api/v1/hosts/{box.id}/recipes/recipe-duo/start", json={"resume_paused": True}
        )

    assert resp.status_code == 409, resp.text
    await session.refresh(paused)
    assert paused.enabled is False
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_paused_duo_instance_is_reused_on_resume_not_a_dash_2(auth_client, session):
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2", role="worker")
    recipe = await _duo_recipe(session)
    paused = await _instance(session, recipe, box_a, enabled=False, nodes=2)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    ensure = AsyncMock(return_value={"ok": True, "stopped": [], "message": "frei"})

    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", _FakeEnvBox()),
        patch("app.services.runtime_manager.ensure_exclusive_host", ensure),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(
            f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start", json={"resume_paused": True}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["runtime_slug"] == paused.slug
    slugs = [r.slug for r in (await session.exec(select(Runtime))).all()]
    assert slugs == [paused.slug], slugs


@pytest.mark.asyncio
async def test_an_enabled_instance_still_wins_over_a_paused_one(auth_client, session):
    """Two rows for the same recipe on the same box (the state the bug left
    behind): the enabled one is the instance; the paused one does not block."""
    box = await _host(session, "box-a")
    recipe = await _recipe(session)
    await _instance(session, recipe, box, enabled=False)
    live = Runtime(
        slug="recipe-x-box-a-2", display_name="x", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", model_identifier=recipe.model_identifier,
        launch_command="docker run img", exclusive_memory=True, host_id=box.id,
        topology={"nodes": 1, "recipe_slug": recipe.slug}, enabled=True,
    )
    session.add(live)
    await session.commit()
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box.id}/recipes/recipe-x/start")

    assert resp.status_code == 200, resp.text
    assert resp.json()["runtime_slug"] == "recipe-x-box-a-2"
    assert len((await session.exec(select(Runtime))).all()) == 2


@pytest.mark.asyncio
async def test_a_paused_instance_on_another_box_does_not_count(auth_client, session):
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    recipe = await _recipe(session)
    await _instance(session, recipe, box_b, enabled=False)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")

    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True


# ── 2. No open transaction across the long steps ─────────────────────────────


class _FakeEnvBox:
    """SSH stand-in that answers ``true`` and echoes the .env it was told to
    write, so the read-back check of ``upsert_env_file`` passes."""

    def __init__(self) -> None:
        self.content = ""

    async def __call__(self, command: str, **kwargs) -> tuple[str, str, int]:
        if command.strip() == "true":
            return "", "", 0
        if command.startswith("cat "):
            return self.content, "", 0
        lines = []
        for line in command.splitlines():
            if "MC_K=" in line and "MC_V=" in line:
                key = line.split("MC_K=", 1)[1].split(" ", 1)[0].strip("'")
                value = line.split("MC_V=", 1)[1].split(" ", 1)[0].strip("'")
                lines.append(f"{key}={value}")
        if lines:
            self.content = "\n".join(lines) + "\n"
        return "", "", 0


@pytest.mark.asyncio
async def test_solo_start_runs_with_no_open_transaction(session):
    box = await _host(session, "box-a")
    recipe = await _recipe(session)
    await _instance(session, recipe, box, enabled=True)
    seen: dict[str, bool] = {}

    async def _already(runtime, **kw):
        seen["probe"] = session.in_transaction()
        return None

    async def _start(runtime, **kw):
        seen["start"] = session.in_transaction()
        return {"ok": True, "message": "läuft an"}

    with (
        _probe(set()),
        patch("app.services.runtime_manager.already_running", _already),
        patch("app.services.runtime_manager.start_runtime", _start),
    ):
        result = await recipe_switcher.start_recipe_on_host(session, box, recipe)

    assert result["ok"] is True
    assert seen == {"probe": False, "start": False}, seen


@pytest.mark.asyncio
async def test_duo_start_talks_ssh_with_no_open_transaction(session):
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2", role="worker")
    recipe = await _duo_recipe(session)
    env_box = _FakeEnvBox()
    ssh_states: list[bool] = []
    seen: dict[str, object] = {}

    async def _ssh(command, **kwargs):
        ssh_states.append(session.in_transaction())
        return await env_box(command, **kwargs)

    async def _ensure(runtime, *, host=None, session=None, host_id=None):
        seen["evict_in_txn"] = _outer.in_transaction()
        return {"ok": True, "stopped": [], "message": "frei"}

    async def _start(runtime, **kw):
        seen["start"] = _outer.in_transaction()
        return {"ok": True, "message": "läuft an"}

    _outer = session
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", _ssh),
        patch("app.services.runtime_manager.ensure_exclusive_host", _ensure),
        patch("app.services.runtime_manager.start_runtime", _start),
    ):
        result = await recipe_switcher.start_recipe_on_host(session, box_a, recipe)

    assert result["ok"] is True, result
    assert ssh_states and not any(ssh_states), ssh_states
    assert seen == {"evict_in_txn": False, "start": False}, seen


@pytest.mark.asyncio
async def test_eviction_closes_its_own_session_before_it_stops_anything(session):
    """``ensure_exclusive_host`` without a caller session: the DB reads come
    first in a short session, the stops (SSH, minutes) after it closed."""
    from app.services import runtime_manager
    from tests.conftest import test_engine

    box = await _host(session, "box-a")
    other = Runtime(
        slug="other", display_name="other", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", launch_command="docker run img",
        exclusive_memory=True, host_id=box.id,
    )
    session.add(other)
    await session.commit()

    scope = {"open": False, "opened": 0}

    @asynccontextmanager
    async def _scope():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            scope["open"] = True
            scope["opened"] += 1
            try:
                yield s
            finally:
                scope["open"] = False

    stopped_while_open: list[bool] = []

    async def _state(runtime, **kw):
        return {"state": "running"}

    async def _evict(slug, **kw):
        stopped_while_open.append(scope["open"])
        return {"ok": True, "message": "gestoppt"}

    with (
        patch("app.services.runtime_model_resolver.session_scope", _scope),
        patch("app.services.runtime_manager.get_runtime_state", _state),
        patch("app.services.runtime_manager.evict_spark_runtime_containers", _evict),
        patch("app.services.host_resolver.resolve_host_for_runtime", AsyncMock(return_value=None)),
    ):
        result = await runtime_manager.ensure_exclusive_host(
            {"id": "neu", "slug": "neu", "exclusive_memory": True, "runtime_type": "vllm_docker"},
            host_id=box.id,
        )

    assert result["ok"] is True, result
    assert scope["opened"] == 1
    assert stopped_while_open == [False]


@pytest.mark.asyncio
async def test_eviction_ends_a_caller_sessions_transaction_before_it_stops(session):
    """With a caller session (the switcher's worker eviction) the reads run in
    it — and its transaction is over before the first stop."""
    from app.services import runtime_manager

    box = await _host(session, "box-a")
    session.add(
        Runtime(
            slug="other", display_name="other", runtime_type="vllm_docker",
            endpoint="http://192.0.2.10:8000/v1", launch_command="docker run img",
            exclusive_memory=True, host_id=box.id,
        )
    )
    await session.commit()
    in_txn_at_stop: list[bool] = []

    async def _state(runtime, **kw):
        return {"state": "running"}

    async def _evict(slug, **kw):
        in_txn_at_stop.append(session.in_transaction())
        return {"ok": True, "message": "gestoppt"}

    with (
        patch("app.services.runtime_manager.get_runtime_state", _state),
        patch("app.services.runtime_manager.evict_spark_runtime_containers", _evict),
        patch("app.services.host_resolver.resolve_host_for_runtime", AsyncMock(return_value=None)),
    ):
        result = await runtime_manager.ensure_exclusive_host(
            {"id": "neu", "slug": "neu", "exclusive_memory": True, "runtime_type": "vllm_docker"},
            session=session,
            host_id=box.id,
        )

    assert result["ok"] is True, result
    assert in_txn_at_stop == [False]
