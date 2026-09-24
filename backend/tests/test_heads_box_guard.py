"""A13 — box guard: no recipe switch / runtime stop / restart under a working head.

Only a DISPLACEMENT is refused. Recovering a dead engine (same recipe, or
nothing running on the box) stays allowed — that is the runtime watcher's
auto-recovery path (spec §6.7).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services.heads import box_guard, engine
from tests.heads_backend_helpers import heads_root, make_run  # noqa: F401
from tests.test_recipe_switcher_p3 import _duo_recipe, _FakeBox, _host, _probe, _recipe, _runtime


def _served(value):
    async def fake(endpoint, now=None):
        return value

    return patch.object(engine, "served_models", fake)


def _idle():
    async def fake(endpoint):
        return None

    return patch.object(engine, "running_requests", fake)


# ── unit ────────────────────────────────────────────────────────────────


def test_live_head_on_box_refuses_displacement(heads_root):
    run_id = make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=["b1"])
    assert box_guard.occupancy()["b1"]["run_id"] == run_id
    with pytest.raises(HTTPException) as exc:
        box_guard.check_displacement(["b1"], "switch", displaces_engine=True)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "head_on_box"


def test_recovery_is_not_a_displacement(heads_root):
    make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=["b1"])
    box_guard.check_displacement(["b1"], "switch", displaces_engine=False)


def test_exited_head_does_not_hold_the_box(heads_root):
    make_run(heads_root, status={"phase": "exited", "reason": "stopped"}, box_keys=["b1"])
    assert box_guard.occupancy() == {}
    box_guard.check_displacement(["b1"], "switch", displaces_engine=True)


def test_guard_is_inert_when_heads_are_disabled(heads_root, monkeypatch):
    make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=["b1"])
    monkeypatch.setattr(settings, "heads_enabled", False)
    box_guard.check_displacement(["b1"], "switch", displaces_engine=True)


def test_spooled_run_already_holds_its_box(heads_root):
    make_run(heads_root, status=None, box_keys=["b1"])  # not picked up yet
    assert "b1" in box_guard.occupancy()


# ── recipe switch (start_recipe_on_host) ─────────────────────────────────


@pytest.mark.asyncio
async def test_recipe_switch_under_a_head_is_409(auth_client, session, heads_root):
    box_a = await _host(session, "box-a")
    await _runtime(session, "running-old", box_a)
    await _recipe(session, "recipe-new")
    make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=[str(box_a.id)])
    start = AsyncMock(return_value={"ok": True, "message": "x"})
    with _probe({"running-old"}), _idle(), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-new/start")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "head_on_box"
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_of_a_dead_engine_under_a_head_is_allowed(auth_client, session, heads_root):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x")
    make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=[str(box_a.id)])
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with _probe(set()), _idle(), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")
    assert resp.status_code == 200, resp.text
    start.assert_awaited_once()


@pytest.mark.asyncio
async def test_duo_recipe_checks_both_boxes(auth_client, session, heads_root):
    """The head works on the WORKER box; the duo start on the head box would
    pull that box too."""
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2", role="worker")
    await _runtime(session, "running-solo-a", box_a)
    await _duo_recipe(session)
    make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=[str(box_b.id)])
    start = AsyncMock(return_value={"ok": True, "message": "x"})
    with (
        _probe({"running-solo-a"}), _idle(),
        patch("app.services.runtime_manager._ssh_run", _FakeBox()),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(
            f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start", json={"worker_host_id": str(box_b.id)}
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "head_on_box"
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_refused_while_engine_reports_running_requests(auth_client, session, heads_root):
    box_a = await _host(session, "box-a")
    await _runtime(session, "running-old", box_a)
    await _recipe(session, "recipe-new")

    async def busy(endpoint):
        return 3

    with _probe({"running-old"}), patch.object(engine, "running_requests", busy), \
            patch("app.services.runtime_manager.start_runtime", AsyncMock()):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-new/start")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"code": "engine_busy", "running_requests": 3}


# ── runtime stop / restart ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "restart"])
async def test_stop_and_restart_of_a_live_engine_under_a_head_are_409(action, auth_client, session, heads_root):
    box_a = await _host(session, "box-a")
    rt = await _runtime(session, "recipe-a", box_a)
    make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=[str(box_a.id)])
    mgr = AsyncMock(return_value={"ok": True, "message": "ok"})
    with _served(frozenset({"m"})), patch(f"app.services.runtime_manager.{action}_runtime", mgr):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/{action}?force=true")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "head_on_box"
    mgr.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "restart"])
async def test_dead_engine_may_be_stopped_or_restarted(action, auth_client, session, heads_root):
    box_a = await _host(session, "box-a")
    rt = await _runtime(session, "recipe-a", box_a)
    make_run(heads_root, status={"phase": "running"}, heartbeat_age=5, box_keys=[str(box_a.id)])
    mgr = AsyncMock(return_value={"ok": True, "message": "ok"})
    with _served(None), patch(f"app.services.runtime_manager.{action}_runtime", mgr):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/{action}")
    assert resp.status_code == 200, resp.text
    mgr.assert_awaited_once()
