"""E1 switch lock stage 1 — no model switch while the engine reports running requests.

ADR-085 names exactly one addition to the frozen box manager: refuse a model
switch while the engine says it is working. This holds WITHOUT the head
launcher (``heads_enabled`` off) — it protects whoever sends the request.

The engine is asked through its Prometheus ``/metrics`` (fake endpoint here:
an ``httpx.MockTransport``). An engine that does not answer or has no known
metric does NOT block the switch (fail-open) — the lock says "unknown" in the
log and in the recipe start response instead.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import runtime_manager
from app.services.heads import box_guard, engine
from tests.test_recipe_switcher_p3 import _host, _probe, _recipe, _runtime

VLLM_BUSY = (
    "# HELP vllm:num_requests_running Number of requests in model execution batches.\n"
    "# TYPE vllm:num_requests_running gauge\n"
    'vllm:num_requests_running{engine="0",model_name="GLM-5.3-Flash-EXL3"} 2.0\n'
    'vllm:num_requests_waiting{engine="0",model_name="GLM-5.3-Flash-EXL3"} 0.0\n'
)
VLLM_IDLE = VLLM_BUSY.replace("} 2.0", "} 0.0")
SGLANG_BUSY = 'sglang:num_running_reqs{model_name="qwen",tp_rank="0"} 4.0\n'
LLAMACPP_BUSY = "llamacpp:requests_processing 1\n"
NO_METRIC = "python_gc_objects_collected_total 12.0\n"


def _metrics(text: str | None, status: int = 200):
    """Fake engine: ``/metrics`` answers ``text``; ``None`` = engine unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if text is None:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/health":
            # An engine without a /health JSON (vLLM answers it empty).
            return httpx.Response(404, text="not found")
        assert request.url.path == "/metrics"
        return httpx.Response(status, text=text)

    return patch.object(engine, "_transport", httpx.MockTransport(handler))


# ── the probe ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [(VLLM_BUSY, 2), (VLLM_IDLE, 0), (SGLANG_BUSY, 4), (LLAMACPP_BUSY, 1)],
)
async def test_probe_reads_the_running_metric_of_each_engine(text, expected):
    with _metrics(text):
        assert await engine.probe_running_requests("http://box:8000/v1") == (expected, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "status", "reason"),
    [(None, 200, "unreachable"), ("oops", 500, "HTTP 500"), (NO_METRIC, 200, "no running-requests metric")],
)
async def test_probe_says_why_the_load_is_unknown(text, status, reason):
    with _metrics(text, status):
        assert await engine.probe_running_requests("http://box:8000/v1") == (None, reason)


# ── the lock (heads launcher OFF — the lock does not depend on it) ────────


@pytest.mark.asyncio
async def test_lock_refuses_while_engine_reports_running_requests(session):
    assert settings.heads_enabled is False
    box = await _host(session, "box-a")
    old = await _runtime(session, "glm-old", box, display_name="GLM old")
    with _metrics(VLLM_BUSY), pytest.raises(HTTPException) as exc:
        await box_guard.check_engine_idle([old])
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["code"] == "engine_busy"
    assert detail["engine"] == "GLM old"
    assert detail["running_requests"] == 2
    assert "GLM old" in detail["message"] and "2" in detail["message"]
    assert "stop the runtime first" in detail["message"]  # the way out of a stuck counter


@pytest.mark.asyncio
async def test_lock_allows_an_idle_engine(session):
    box = await _host(session, "box-a")
    old = await _runtime(session, "glm-old", box)
    with _metrics(VLLM_IDLE):
        assert await box_guard.check_engine_idle([old]) == []


@pytest.mark.asyncio
async def test_lock_fails_open_and_flags_unknown(session, caplog):
    box = await _host(session, "box-a")
    old = await _runtime(session, "glm-old", box, display_name="GLM old")
    with _metrics(None), caplog.at_level(logging.WARNING):
        unknown = await box_guard.check_engine_idle([old])
    assert unknown == [{"engine": "GLM old", "reason": "unreachable"}]
    assert "switch lock unknown" in caplog.text


# ── path 1: recipe switch (POST /hosts/{id}/recipes/{slug}/start) ─────────


@pytest.mark.asyncio
async def test_recipe_switch_refused_while_engine_busy(auth_client, session):
    box = await _host(session, "box-a")
    await _runtime(session, "glm-old", box, display_name="GLM old")
    await _recipe(session, "recipe-new")
    start = AsyncMock(return_value={"ok": True, "message": "x"})
    with _probe({"glm-old"}), _metrics(VLLM_BUSY), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box.id}/recipes/recipe-new/start")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "engine_busy"
    assert resp.json()["detail"]["engine"] == "GLM old"
    assert resp.json()["detail"]["running_requests"] == 2
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipe_switch_allowed_when_engine_idle(auth_client, session):
    box = await _host(session, "box-a")
    await _runtime(session, "glm-old", box)
    await _recipe(session, "recipe-new")
    start = AsyncMock(return_value={"ok": True, "message": "x"})
    with _probe({"glm-old"}), _metrics(VLLM_IDLE), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box.id}/recipes/recipe-new/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["switch_lock"] == {"state": "idle", "unknown": []}
    start.assert_awaited_once()


@pytest.mark.asyncio
async def test_recipe_switch_allowed_but_flagged_when_metrics_unreachable(auth_client, session):
    box = await _host(session, "box-a")
    await _runtime(session, "glm-old", box, display_name="GLM old")
    await _recipe(session, "recipe-new")
    start = AsyncMock(return_value={"ok": True, "message": "x"})
    with _probe({"glm-old"}), _metrics(None), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box.id}/recipes/recipe-new/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["switch_lock"] == {
        "state": "unknown",
        "unknown": [{"engine": "GLM old", "reason": "unreachable"}],
    }
    start.assert_awaited_once()


# ── path 2: every start that frees an exclusive box (ensure_exclusive_host) ─
# POST /runtimes/{id}/start, the runtime schedule, the lifecycle API and the
# recipe start itself all displace the old engine here.


def _exclusive_patches(stopped: list[str], state: str = "ready"):
    async def fake_evict(slug, **_kw):
        stopped.append(slug)
        return {"ok": True}

    return (
        patch.object(runtime_manager, "evict_spark_runtime_containers", fake_evict),
        patch.object(runtime_manager, "get_runtime_state", AsyncMock(return_value={"state": state})),
        patch.object(runtime_manager, "resolve_host_for_runtime", AsyncMock(return_value=None), create=True),
    )


@pytest.mark.asyncio
async def test_exclusive_start_refused_while_old_engine_busy(session):
    box = await _host(session, "box-a")
    await _runtime(session, "glm-old", box, display_name="GLM old")
    new = await _runtime(session, "qwen-new", box)
    stopped: list[str] = []
    p1, p2, p3 = _exclusive_patches(stopped)
    with p1, p2, p3, _metrics(VLLM_BUSY):
        result = await runtime_manager.ensure_exclusive_host(new.model_dump(), session=session)
    assert result["ok"] is False
    assert result["switch_lock"]["code"] == "engine_busy"
    assert result["switch_lock"]["engine"] == "GLM old"
    assert stopped == []


@pytest.mark.asyncio
async def test_exclusive_start_proceeds_when_old_engine_idle(session):
    box = await _host(session, "box-a")
    await _runtime(session, "glm-old", box)
    new = await _runtime(session, "qwen-new", box)
    stopped: list[str] = []
    p1, p2, p3 = _exclusive_patches(stopped)
    with p1, p2, p3, _metrics(VLLM_IDLE):
        result = await runtime_manager.ensure_exclusive_host(new.model_dump(), session=session)
    assert result["ok"] is True
    assert stopped == ["glm-old"]
    assert result["switch_lock_unknown"] == []


@pytest.mark.asyncio
async def test_exclusive_start_fails_open_when_metrics_unreachable(session):
    box = await _host(session, "box-a")
    await _runtime(session, "glm-old", box, display_name="GLM old")
    new = await _runtime(session, "qwen-new", box)
    stopped: list[str] = []
    p1, p2, p3 = _exclusive_patches(stopped)
    with p1, p2, p3, _metrics(None):
        result = await runtime_manager.ensure_exclusive_host(new.model_dump(), session=session)
    assert result["ok"] is True
    assert stopped == ["glm-old"]
    assert result["switch_lock_unknown"] == [{"engine": "GLM old", "reason": "unreachable"}]


@pytest.mark.asyncio
async def test_stopped_engine_is_not_asked(session):
    """Recovery onto a box whose engine is already down: nothing to protect,
    nothing probed (the watcher's autostart stays untouched)."""
    box = await _host(session, "box-a")
    await _runtime(session, "glm-old", box)
    new = await _runtime(session, "qwen-new", box)
    stopped: list[str] = []
    p1, p2, p3 = _exclusive_patches(stopped, state="stopped")
    with p1, p2, p3, _metrics(VLLM_BUSY):
        result = await runtime_manager.ensure_exclusive_host(new.model_dump(), session=session)
    assert result["ok"] is True
    assert stopped == []


@pytest.mark.asyncio
async def test_start_runtime_passes_the_refusal_through():
    refusal = {"code": "engine_busy", "engine": "GLM old", "running_requests": 2, "message": "m"}
    exclusive = AsyncMock(return_value={"ok": False, "message": "m", "stopped": [], "switch_lock": refusal})
    rt = {"id": "x", "slug": "qwen-new", "runtime_type": "vllm_docker", "exclusive_memory": True}
    with patch.object(runtime_manager, "ensure_exclusive_host", exclusive), \
            patch.object(runtime_manager, "_emit_exclusive_event", AsyncMock()):
        result = await runtime_manager.start_runtime(rt)
    assert result["ok"] is False
    assert result["switch_lock"] == refusal


@pytest.mark.asyncio
async def test_runtime_start_endpoint_answers_409(auth_client, session):
    box = await _host(session, "box-a")
    new = await _runtime(session, "qwen-new", box)
    refusal = {"code": "engine_busy", "engine": "GLM old", "running_requests": 2, "message": "m"}
    start = AsyncMock(return_value={"ok": False, "message": "m", "switch_lock": refusal})
    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/runtimes/{new.slug}/start")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == refusal


# ── TensorFold (live sample 01.10.2026) ──────────────────────────────────
# TensorFold is not vLLM: it reports its load as ``tensorfold:requests_running``
# and in ``/health`` as ``requests_running`` / ``busy``. Before this, the lock
# saw "no running-requests metric" and let every switch through (fail-open).

TENSORFOLD_IDLE = (
    "# HELP tensorfold:requests_running Requests in prefill or decode.\n"
    "# TYPE tensorfold:requests_running gauge\n"
    "tensorfold:requests_running 0\n"
    "# HELP tensorfold:requests_waiting Requests queued or held until a lane is free.\n"
    "# TYPE tensorfold:requests_waiting gauge\n"
    "tensorfold:requests_waiting 0\n"
    "tensorfold_health:requests_total 2\n"
    "# HELP tensorfold_health:completion_tokens_total Reply tokens, the running replies' tokens so far included.\n"
    "tensorfold_health:completion_tokens_total 44\n"
    'tensorfold_health:streams{state="decoding"} 0\n'
    'tensorfold_health:streams{state="filling"} 0\n'
)
TENSORFOLD_BUSY = TENSORFOLD_IDLE.replace(
    "tensorfold:requests_running 0", "tensorfold:requests_running 3"
)


def _engine(metrics: str | None, metrics_status: int = 200, health: dict | None = None,
            health_status: int = 200):
    """Fake engine with ``/metrics`` AND ``/health``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            if metrics is None:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(metrics_status, text=metrics)
        if request.url.path == "/health":
            if health is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(health_status, json=health)
        raise AssertionError(f"unexpected path {request.url.path}")

    return patch.object(engine, "_transport", httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"), [(TENSORFOLD_IDLE, 0), (TENSORFOLD_BUSY, 3)], ids=["idle", "busy"]
)
async def test_probe_reads_the_tensorfold_metric(text, expected):
    with _metrics(text):
        assert await engine.probe_running_requests("http://box:8000/v1") == (expected, None)


@pytest.mark.asyncio
async def test_tensorfold_waiting_and_health_counters_are_not_running_requests():
    # Only the running gauge counts — queued requests and lifetime totals
    # (requests_total 2) must not make an idle engine look busy.
    with _metrics(TENSORFOLD_IDLE):
        assert await engine.probe_running_requests("http://box:8000/v1") == (0, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health", "expected"),
    [
        ({"status": "ok", "requests_running": 2, "busy": True}, 2),
        ({"status": "ok", "requests_running": 0, "busy": False}, 0),
        ({"status": "ok", "busy": True}, 1),
        ({"status": "ok", "busy": False}, 0),
    ],
)
async def test_health_json_is_the_fallback_when_metrics_say_nothing(health, expected):
    with _engine(NO_METRIC, health=health):
        assert await engine.probe_running_requests("http://box:8000/v1") == (expected, None)


@pytest.mark.asyncio
async def test_health_fallback_also_covers_missing_metrics_endpoint():
    with _engine("not found", metrics_status=404, health={"requests_running": 1, "busy": True}):
        assert await engine.probe_running_requests("http://box:8000/v1") == (1, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metrics", "metrics_status", "health", "reason"),
    [
        (NO_METRIC, 200, None, "no running-requests metric"),
        (NO_METRIC, 200, {"status": "ok"}, "no running-requests metric"),
        ("oops", 500, None, "HTTP 500"),
    ],
)
async def test_health_fallback_keeps_the_reason_when_it_knows_nothing(
    metrics, metrics_status, health, reason
):
    with _engine(metrics, metrics_status=metrics_status, health=health):
        assert await engine.probe_running_requests("http://box:8000/v1") == (None, reason)


@pytest.mark.asyncio
async def test_lock_refuses_a_switch_away_from_a_busy_tensorfold(session):
    box = await _host(session, "box-a")
    old = await _runtime(session, "glm-tf", box, display_name="GLM TensorFold")
    with _metrics(TENSORFOLD_BUSY), pytest.raises(HTTPException) as exc:
        await box_guard.check_engine_idle([old])
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "engine_busy"
    assert exc.value.detail["running_requests"] == 3


@pytest.mark.asyncio
async def test_lock_allows_a_switch_away_from_an_idle_tensorfold(session):
    box = await _host(session, "box-a")
    old = await _runtime(session, "glm-tf", box)
    with _metrics(TENSORFOLD_IDLE):
        assert await box_guard.check_engine_idle([old]) == []
