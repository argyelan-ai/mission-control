"""CLI Tool Update Check (CLI-Tool-Updates Task 3) — cache assembly, dedup,
network/manifest error resilience."""
import json
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.config import settings
from app.models.activity import ActivityEvent
from app.redis_client import RedisKeys
from app.services import sse as sse_mod
from app.services.cli_update_check import CLIUpdateChecker, run_check_once


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


_MANIFEST = {
    "openclaude": {"version": "1.0.0"},
    "claude": {"version": "2.0.0"},
    "omp": {"version": "3.0.0"},
}


def _installed(tool):
    return _MANIFEST[tool]["version"]


def test_settings_and_keys_exist():
    assert settings.cli_update_check_interval == 21600
    assert RedisKeys.cli_update_check_lock() == "mc:cli:check-lock"
    assert RedisKeys.cli_versions_cache() == "mc:cli:versions"
    assert RedisKeys.cli_update_notified("claude", "1.2.3") == "mc:cli:notified:claude:1.2.3"


async def _fetch_latest_update_available(tool):
    bumped = {"openclaude": "1.1.0", "claude": "2.0.0", "omp": "3.0.0"}
    return {"version": bumped[tool], "sha256": None}


@pytest.mark.asyncio
async def test_update_available_writes_cache_and_emits_once(async_session, fake_redis):
    with patch(
        "app.services.cli_update_check.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.cli_update_check.read_manifest", return_value=_MANIFEST,
    ), patch(
        "app.services.cli_update_check.installed_version", side_effect=_installed,
    ), patch(
        "app.services.cli_update_check.fetch_latest",
        side_effect=_fetch_latest_update_available,
    ):
        result = await run_check_once(async_session)
        result_2 = await run_check_once(async_session)

    assert result["openclaude"]["update_available"] is True
    assert result["openclaude"]["latest"] == "1.1.0"
    assert result["openclaude"]["target"] == "1.0.0"
    assert result["openclaude"]["installed"] == "1.0.0"
    assert result["claude"]["update_available"] is False
    assert result["omp"]["update_available"] is False

    cached = json.loads(await fake_redis.get(RedisKeys.cli_versions_cache()))
    assert cached["openclaude"]["latest"] == "1.1.0"
    assert result_2["openclaude"]["update_available"] is True  # cache reflects both ticks

    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "cli.update_available")
        )
    ).all()
    assert len(events) == 1  # deduped across the two ticks — same tool+version
    assert events[0].detail["tool"] == "openclaude"
    assert events[0].detail["latest"] == "1.1.0"


async def _fetch_latest_no_update(tool):
    return {"version": _MANIFEST[tool]["version"], "sha256": None}


@pytest.mark.asyncio
async def test_no_update_available(async_session, fake_redis):
    with patch(
        "app.services.cli_update_check.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.cli_update_check.read_manifest", return_value=_MANIFEST,
    ), patch(
        "app.services.cli_update_check.installed_version", side_effect=_installed,
    ), patch(
        "app.services.cli_update_check.fetch_latest", side_effect=_fetch_latest_no_update,
    ):
        result = await run_check_once(async_session)

    assert all(not v["update_available"] for v in result.values())
    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "cli.update_available")
        )
    ).all()
    assert events == []


@pytest.mark.asyncio
async def test_network_error_falls_back_to_old_cache_entry(async_session, fake_redis):
    """First tick succeeds for all tools; second tick has omp fail over the
    network — omp's cache entry must stay as the last-known-good value while
    the other tools still update, and the loop must not raise."""
    seed_cache = {
        "omp": {
            "installed": "3.0.0", "target": "3.0.0", "latest": "3.0.0",
            "update_available": False, "checked_at": "2026-01-01T00:00:00+00:00",
        }
    }
    await fake_redis.set(RedisKeys.cli_versions_cache(), json.dumps(seed_cache))

    async def _fetch_flaky(tool):
        if tool == "omp":
            raise ConnectionError("network down")
        return await _fetch_latest_no_update(tool)

    with patch(
        "app.services.cli_update_check.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.cli_update_check.read_manifest", return_value=_MANIFEST,
    ), patch(
        "app.services.cli_update_check.installed_version", side_effect=_installed,
    ), patch(
        "app.services.cli_update_check.fetch_latest", side_effect=_fetch_flaky,
    ):
        result = await run_check_once(async_session)

    assert result["omp"] == seed_cache["omp"]  # untouched, stale-but-served
    assert result["claude"]["latest"] == "2.0.0"  # unaffected tools still update
    cached = json.loads(await fake_redis.get(RedisKeys.cli_versions_cache()))
    assert cached["omp"] == seed_cache["omp"]


@pytest.mark.asyncio
async def test_network_error_with_no_prior_cache_yields_null_latest(async_session, fake_redis):
    async def _fetch_flaky(tool):
        raise TimeoutError("upstream timeout")

    with patch(
        "app.services.cli_update_check.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.cli_update_check.read_manifest", return_value=_MANIFEST,
    ), patch(
        "app.services.cli_update_check.installed_version", side_effect=_installed,
    ), patch(
        "app.services.cli_update_check.fetch_latest", side_effect=_fetch_flaky,
    ):
        result = await run_check_once(async_session)

    for entry in result.values():
        assert entry["latest"] is None
        assert entry["update_available"] is False


@pytest.mark.asyncio
async def test_manifest_unreadable_does_not_crash_and_target_is_none(async_session, fake_redis):
    import json as json_mod

    with patch(
        "app.services.cli_update_check.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.cli_update_check.read_manifest",
        side_effect=FileNotFoundError("cli-versions.json missing"),
    ), patch(
        "app.services.cli_update_check.installed_version", side_effect=_installed,
    ), patch(
        "app.services.cli_update_check.fetch_latest", side_effect=_fetch_latest_no_update,
    ):
        result = await run_check_once(async_session)

    for entry in result.values():
        assert entry["target"] is None
        # any latest > None target counts as an update
        assert entry["update_available"] is True


@pytest.mark.asyncio
async def test_loop_disabled_when_interval_zero(fake_redis):
    checker = CLIUpdateChecker(interval=0)
    with patch(
        "app.services.cli_update_check.get_redis", _fake_get_redis(fake_redis),
    ):
        await checker.start()
    assert checker._task is None
    await checker.stop()  # no-op, must not raise


@pytest.mark.asyncio
async def test_lock_prevents_concurrent_tick(fake_redis):
    checker = CLIUpdateChecker(interval=90)
    with patch(
        "app.services.cli_update_check.get_redis", _fake_get_redis(fake_redis),
    ):
        first = await checker._acquire_lock()
        second = await checker._acquire_lock()
    assert first is True
    assert second is False
