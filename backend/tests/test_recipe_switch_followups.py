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
