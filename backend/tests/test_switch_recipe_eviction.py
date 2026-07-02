"""switch_recipe must evict ALL running Spark containers before starting.

Pins the integration of the new eviction path into the recipe-switch flow:
  - eviction runs BEFORE start_runtime (P0+P1)
  - a failed eviction aborts the switch with an honest error (does not start a
    second model on top of a still-occupied GPU/RAM)
  - the launch-log path is surfaced on start failure (P4)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services import sparkrun_manager


@pytest.fixture
async def spark_runtime(async_session: AsyncSession) -> Runtime:
    rt = Runtime(
        slug="qwen-general",
        display_name="Spark Qwen vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="Qwen/Qwen3.6-35B-A3B-FP8",
        launch_command=(
            "uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm "
            "--solo --no-rm --ensure --no-follow "
            "--label mc.runtime.slug=qwen-general"
        ),
        container_name=None,  # CLI-started Ornith left this empty — RC-1
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    return rt


@pytest.fixture
async def patched_redis(fake_redis):
    async def _fake_get_redis():
        return fake_redis

    with patch("app.services.runtime_model_resolver.get_redis", _fake_get_redis):
        yield fake_redis


@pytest.mark.asyncio
async def test_switch_evicts_before_start(
    async_session: AsyncSession, spark_runtime: Runtime, patched_redis
):
    """Order matters: eviction must complete before start_runtime is invoked."""
    calls: list[str] = []

    async def _evict(slug, **kw):
        calls.append("evict")
        return {"ok": True, "message": "evicted", "stopped": ["sparkrun_x_solo"]}

    async def _start(rt, **_kw):  # **_kw: host= kwarg (ADR-048)
        calls.append("start")
        return {"ok": True, "message": "starting"}

    with (
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(side_effect=_evict),
        ),
        patch(
            "app.services.runtime_manager.start_runtime",
            AsyncMock(side_effect=_start),
        ),
        patch(
            "app.services.agent_runtime_switch.probe_runtime_model",
            AsyncMock(return_value=None),
        ),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session, spark_runtime, "@official/qwen3.6-27b-fp8-mtp-vllm"
        )

    assert result["ok"] is True
    assert calls == ["evict", "start"], f"expected evict before start, got {calls}"


@pytest.mark.asyncio
async def test_switch_aborts_when_eviction_fails(
    async_session: AsyncSession, spark_runtime: Runtime, patched_redis
):
    """A failed eviction (RAM never freed) must abort the switch — never start a
    second model on top of a full GPU."""
    start_mock = AsyncMock(return_value={"ok": True, "message": "starting"})
    with (
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={
                "ok": False,
                "message": "containers still running after 30s: sparkrun_stuck_solo",
            }),
        ),
        patch("app.services.runtime_manager.start_runtime", start_mock),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session, spark_runtime, "@official/qwen3.6-27b-fp8-mtp-vllm"
        )

    assert result["ok"] is False
    assert "still running" in result["message"].lower() or "evict" in result["message"].lower()
    start_mock.assert_not_called()


@pytest.mark.asyncio
async def test_switch_surfaces_log_path_on_start_failure(
    async_session: AsyncSession, spark_runtime: Runtime, patched_redis
):
    """On start failure the message should carry the launch-log path (P4)."""
    with (
        patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={"ok": True, "message": "evicted"}),
        ),
        patch(
            "app.services.runtime_manager.start_runtime",
            AsyncMock(return_value={
                "ok": False,
                "message": (
                    "Container erschien nicht. Logs: "
                    "~/.cache/mc/runtime-launch-qwen-general.log"
                ),
            }),
        ),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session, spark_runtime, "@official/qwen3.6-27b-fp8-mtp-vllm"
        )

    assert result["ok"] is False
    assert "runtime-launch-qwen-general.log" in result["message"]
