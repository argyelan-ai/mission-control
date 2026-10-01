"""Follow-ups of the recipe-switch live test 01.10.2026 (GLM vLLM ↔ TensorFold).

1. Ping-pong: after an operator switched recipes, the watcher's auto-recovery
   must never flip the box back on its own — neither while the new engine is
   still loading (its grace marker gone, e.g. a start reported as failed while
   the engine actually came up), nor for a runtime a switch stopped on purpose.
2. Order: starting an engine that already runs answers "läuft bereits" BEFORE
   any eviction or memory prep (live: a second TensorFold start waited 180 s
   for memory TensorFold itself held, then failed with 400).

No network, RFC 5737 addresses only (public repo).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import host_memory_prep, runtime_manager
from app.services.host_resolver import ResolvedHost

SSH_PROCESS = runtime_manager.SSH_PROCESS_TYPE
BOX = ResolvedHost(ssh_host="192.0.2.10", ssh_user="op", kind="ssh", source="registry")

TF_RT = {
    "id": "tf-box-a",
    "slug": "tf-box-a",
    "display_name": "GLM TensorFold",
    "runtime_type": SSH_PROCESS,
    "endpoint": "http://192.0.2.10:8000/v1",
    "process_name": "tf-engine",
    "launch_command": "cd ~/engine && exec setsid ./start.sh < /dev/null",
    "stop_command": "cd ~/engine && ./stop.sh",
    "exclusive_memory": True,
}
VLLM_RT = {
    "id": "vllm-box-a",
    "slug": "vllm-box-a",
    "display_name": "GLM vLLM",
    "runtime_type": "vllm_docker",
    "endpoint": "http://192.0.2.10:8000/v1",
    "container_name": "vllm-head",
    "launch_command": "docker run -d --name vllm-head img",
    "exclusive_memory": True,
}


def _order_patches(state: dict):
    """Everything after the 'already running?' answer is a spy that must stay silent."""
    exclusive = AsyncMock(return_value={"ok": True, "message": "frei", "stopped": []})
    prep = AsyncMock(return_value=None)
    impl = AsyncMock(return_value={"ok": True, "message": "gestartet"})
    patches = (
        patch.object(runtime_manager, "get_runtime_state", AsyncMock(return_value=state)),
        patch.object(runtime_manager, "ensure_exclusive_host", exclusive),
        patch.object(runtime_manager, "_emit_exclusive_event", AsyncMock()),
        patch.object(host_memory_prep, "prepare_for_runtime", prep),
        patch.object(runtime_manager, "_start_runtime_impl", impl),
    )
    return patches, exclusive, prep, impl


# ── 2. order: "already running" before eviction and memory prep ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("rt", [TF_RT, VLLM_RT], ids=["ssh_process", "docker"])
@pytest.mark.parametrize("state", ["ready", "warming"])
async def test_a_running_engine_is_answered_before_eviction_and_memory_prep(rt, state, fake_redis):
    patches, exclusive, prep, impl = _order_patches({"state": state})
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await runtime_manager.start_runtime(rt, host=BOX)

    assert result["ok"] is True
    assert result["already_running"] is True
    assert "läuft bereits" in result["message"] or "startet bereits" in result["message"]
    exclusive.assert_not_awaited()
    prep.assert_not_awaited()
    impl.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stopped", "unknown", "failed"])
async def test_a_stopped_or_unknown_engine_still_takes_the_full_start_path(state, fake_redis):
    patches, exclusive, prep, impl = _order_patches({"state": state})
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await runtime_manager.start_runtime(TF_RT, host=BOX)

    assert result["ok"] is True
    assert "already_running" not in result
    exclusive.assert_awaited_once()
    prep.assert_awaited_once()
    impl.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failing_state_probe_never_blocks_a_start(fake_redis):
    patches, exclusive, prep, impl = _order_patches({})
    with (
        patch.object(runtime_manager, "get_runtime_state", AsyncMock(side_effect=OSError("ssh"))),
        patches[1], patches[2], patches[3], patches[4],
    ):
        result = await runtime_manager.start_runtime(TF_RT, host=BOX)
    assert result["ok"] is True
    impl.assert_awaited_once()


# ── 2b. the recipe switcher: a running duo is not "switched" onto itself ──


@pytest.mark.asyncio
async def test_switching_to_the_recipe_that_already_runs_touches_no_box(auth_client, session):
    """Live 19:49:50: TensorFold ran, a second start of TensorFold rewrote the
    .env, ran the worker eviction, waited 180 s in the memory prep and failed
    with 400. Now: 200 "läuft bereits", and nothing on either box moves."""
    from sqlmodel import select

    from app.models.runtime import Runtime
    from tests.test_recipe_switcher_p3 import _duo_recipe, _FakeBox, _host, _probe, _runtime

    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    recipe = await _duo_recipe(session)
    await _runtime(
        session, "recipe-duo-box-a", box_a,
        runtime_type=SSH_PROCESS, process_name="tf-engine",
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug, "worker_host_id": str(box_b.id)},
    )

    box = _FakeBox()
    exclusive = AsyncMock(return_value={"ok": True, "message": "frei", "stopped": []})
    start = AsyncMock(return_value={"ok": True, "message": "gestartet"})
    with (
        _probe({"recipe-duo-box-a"}),
        patch("app.services.runtime_manager._ssh_run", box),
        patch("app.services.runtime_manager.get_runtime_state",
              AsyncMock(return_value={"state": "ready"})),
        patch("app.services.runtime_manager.ensure_exclusive_host", exclusive),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/{recipe.slug}/start")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["already_running"] is True
    assert "läuft bereits" in body["message"]
    assert body["created"] is False
    assert body["env_written"] == []
    assert body["switch_lock"]["state"] == "no_switch"
    exclusive.assert_not_awaited()
    start.assert_not_awaited()
    assert box.commands == []  # no .env write, no SSH at all
    assert len((await session.exec(select(Runtime))).all()) == 1


# ── 1. ping-pong: the watcher never flips a switched box back ────────────
#
# Live 01.10.2026: two recipes (vLLM and TensorFold) on one box, autostart on.
# Two ways the watcher could undo an operator's switch on its own:
#   a) the new engine is loading, but every marker the sibling guard reads is
#      gone (the start reported "failed" although the engine came up — the
#      foreground-launch bug of #728 — so grace and memory-prep handle were
#      cleared), and the autostart still names the OLD recipe (it only follows
#      a confirmed answer);
#   b) the runtime the switch stopped on purpose is "down" and the autostart
#      still points at it.


def _box_shell(running: set[str]):
    """Fake SSH: an anchor check answers 0 for a name in ``running``, 1 otherwise."""

    async def run(command: str, **kwargs):
        if command.strip() == "true":
            return "", "", 0
        if "pgrep -x" in command or "docker inspect" in command:
            return "", "", 0 if any(name in command for name in running) else 1
        return "", "", 0

    return run


async def _two_recipes_one_box(session, *, autostart_recipe: str):
    from tests.test_recipe_switcher_p3 import _host, _recipe, _runtime

    await _recipe(session, "recipe-vllm", model_identifier="org/glm")
    await _recipe(session, "recipe-tf", model_identifier="org/glm", engine=SSH_PROCESS)
    box = await _host(
        session, "box-a", autostart_enabled=True, autostart_recipe_slug=autostart_recipe
    )
    vllm = await _runtime(
        session, "vllm-box-a", box, container_name="vllm-head", model_identifier="org/glm",
        topology={"nodes": 1, "recipe_slug": "recipe-vllm"},
    )
    tf = await _runtime(
        session, "tf-box-a", box, runtime_type=SSH_PROCESS, process_name="tf-engine",
        model_identifier="org/glm", topology={"nodes": 1, "recipe_slug": "recipe-tf"},
    )
    return box, vllm, tf


async def _tick_unreachable(session, fake_redis, *, running: set[str], start, ticks=4):
    from app.services.agent_runtime_switch import ProbedModel
    from app.services.runtime_watcher import RuntimeWatcher

    async def _get():
        return fake_redis

    watcher = RuntimeWatcher(interval=90)
    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _get),
        patch("app.services.runtime_grace.get_redis", _get),
        patch("app.services.runtime_watcher.resolve_host_for_runtime", new=AsyncMock(return_value=BOX)),
        patch("app.services.runtime_manager._ssh_run", _box_shell(running)),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        for _ in range(ticks):
            await watcher.tick(session=session)


@pytest.mark.asyncio
async def test_a_loading_engine_without_any_marker_is_never_evicted_by_autostart(
    async_session, fake_redis
):
    """(a) The operator switched to vLLM; its start was reported as failed, so
    no grace marker and no prep handle exist — but the vLLM container RUNS
    (loading). Autostart still names TensorFold. Reviving TensorFold here
    would evict the loading vLLM: the 19:36 flip shape."""
    await _two_recipes_one_box(async_session, autostart_recipe="recipe-tf")
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    await _tick_unreachable(async_session, fake_redis, running={"vllm-head"}, start=start)

    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_runtime_stopped_by_a_switch_is_not_revived(async_session, fake_redis):
    """(b) The switch to vLLM stopped TensorFold on purpose. Even when nothing
    else runs on the box (the new engine's start script is still preparing,
    no anchor yet), the watcher must not bring the stopped engine back."""
    from app.services import runtime_grace

    await _two_recipes_one_box(async_session, autostart_recipe="recipe-tf")
    with patch("app.services.runtime_grace.get_redis", AsyncMock(return_value=fake_redis)):
        await runtime_grace.mark_evicted("tf-box-a", by="vllm-box-a")
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    await _tick_unreachable(async_session, fake_redis, running=set(), start=start)

    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_empty_box_without_a_switch_is_still_revived(async_session, fake_redis):
    """The guard must not kill autostart: nothing runs, nobody switched → the
    box's own recipe comes back (reboot case)."""
    await _two_recipes_one_box(async_session, autostart_recipe="recipe-tf")
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    await _tick_unreachable(async_session, fake_redis, running=set(), start=start)

    start.assert_awaited_once()
    assert start.await_args.args[0]["slug"] == "tf-box-a"


@pytest.mark.asyncio
async def test_the_eviction_marker_is_set_by_the_exclusive_sweep_and_lifted_by_a_new_start(
    session, fake_redis
):
    """Who sets the marker: the eviction itself (every start path goes through
    it). Who lifts it: starting that runtime again — the operator's choice."""
    from app.services import runtime_grace
    from tests.test_recipe_switcher_p3 import _host, _runtime

    box = await _host(session, "box-a")
    await _runtime(session, "tf-box-a", box, container_name="tf-engine")
    new = await _runtime(session, "vllm-box-a", box, container_name="vllm-head")

    async def fake_evict(slug, **_kw):
        return {"ok": True}

    with (
        patch("app.services.runtime_grace.get_redis", AsyncMock(return_value=fake_redis)),
        patch.object(runtime_manager, "evict_spark_runtime_containers", fake_evict),
        patch.object(runtime_manager, "get_runtime_state", AsyncMock(return_value={"state": "ready"})),
        patch("app.services.heads.box_guard.switch_lock", AsyncMock(return_value=(None, []))),
    ):
        result = await runtime_manager.ensure_exclusive_host(new.model_dump(), session=session)
        assert result["stopped"] == ["tf-box-a"]
        doc = await runtime_grace.get_evicted("tf-box-a")
        assert doc is not None and doc["by"] == "vllm-box-a"

        patches, *_ = _order_patches({"state": "stopped"})
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            await runtime_manager.start_runtime({**TF_RT, "slug": "tf-box-a"}, host=BOX)
        assert await runtime_grace.get_evicted("tf-box-a") is None


# ── forensics: the next flip must be provable from the activity feed ─────
# On 01.10. two questions had no answer in the feed: what stopped the old
# engine ("Box war bereits frei" — the silent worker eviction did), and who
# sent the start (backend logs were gone after a deploy).


@pytest.mark.asyncio
async def test_a_recipe_start_records_who_asked_and_what_the_worker_eviction_stopped(
    auth_client, session
):
    from sqlmodel import select

    from app.models.activity import ActivityEvent
    from tests.test_recipe_switcher_p3 import _duo_recipe, _FakeBox, _host, _probe

    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)

    async def _ensure(runtime, *, host=None, session=None, host_id=None):
        return {"ok": True, "stopped": ["old-duo"], "message": "Box freigegeben (gestoppt: old-duo)."}

    emitted: list[tuple] = []

    async def _emit(slug, result, *, box=None):
        emitted.append((slug, result.get("stopped"), box))

    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", _FakeBox()),
        patch("app.services.runtime_manager.ensure_exclusive_host", _ensure),
        patch("app.services.runtime_manager._emit_exclusive_event", _emit),
        patch("app.services.runtime_manager.start_runtime",
              AsyncMock(return_value={"ok": True, "message": "läuft an"})),
    ):
        resp = await auth_client.post(
            f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start",
            headers={"User-Agent": "curl/8.7.1"},
        )
    assert resp.status_code == 200, resp.text

    assert emitted == [("recipe-duo-box-a", ["old-duo"], "worker")]

    events = (
        await session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "host.recipe_start_requested")
        )
    ).all()
    assert len(events) == 1
    detail = events[0].detail
    assert detail["recipe_slug"] == "recipe-duo"
    assert detail["host_id"] == str(box_a.id)
    assert detail["client"] == "curl/8.7.1"
    assert detail["user_id"]
