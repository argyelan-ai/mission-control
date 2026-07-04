"""Runtime Watcher (ADR-053) — settings, keys, drift detection, propagation gates."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import sse as sse_mod
from app.services.runtime_watcher import RuntimeWatcher


def test_watcher_settings_and_keys_exist():
    assert settings.runtime_watcher_enabled is True
    assert settings.runtime_watcher_interval == 90
    assert RedisKeys.runtime_watcher_lock() == "mc:runtime-watcher:lock"
    assert RedisKeys.runtime_live("qwen-general") == "mc:runtime-live:qwen-general"
    assert RedisKeys.runtime_drift_candidate("x") == "mc:runtime-drift:x"
    assert RedisKeys.agent_switch_progress("abc") == "mc:agent:abc:runtime-switch-progress"
    assert RedisKeys.agent_model_sync_fails("abc") == "mc:agent:abc:model-sync-fails"


async def _mk_rt(session, *, slug="watch-rt", model="old-model"):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type="vllm_docker",
        endpoint="http://spark:8000/v1", model_identifier=model, enabled=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


@pytest.mark.asyncio
async def test_tick_writes_live_status_no_drift(async_session, fake_redis):
    rt = await _mk_rt(async_session, slug="live-rt")
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model",
        new=AsyncMock(return_value="old-model"),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ):
        await watcher.tick(session=async_session)

    raw = await fake_redis.get(RedisKeys.runtime_live("live-rt"))
    status = json.loads(raw)
    assert status["reachable"] is True
    assert status["served_model"] == "old-model"
    await async_session.refresh(rt)
    assert rt.model_identifier == "old-model"  # unchanged


@pytest.mark.asyncio
async def test_drift_requires_two_consecutive_probes(async_session, fake_redis):
    rt = await _mk_rt(async_session, slug="drift-rt")
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model",
        new=AsyncMock(return_value="new-model"),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ) as mock_mark:
        await watcher.tick(session=async_session)     # probe 1: candidate only
        await async_session.refresh(rt)
        assert rt.model_identifier == "old-model"

        await watcher.tick(session=async_session)     # probe 2: confirmed
        await async_session.refresh(rt)
        assert rt.model_identifier == "new-model"
        mock_mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreachable_engine_never_touches_row(async_session, fake_redis):
    rt = await _mk_rt(async_session, slug="down-rt")
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ):
        for _ in range(4):
            await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.model_identifier == "old-model"
    status = json.loads(await fake_redis.get(RedisKeys.runtime_live("down-rt")))
    assert status["reachable"] is False
    assert status["consecutive_failures"] >= 3
