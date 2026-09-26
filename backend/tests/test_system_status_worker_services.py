"""System status must report background services honestly when they run in
the separate worker process.

Since the worker container split (ENABLE_BACKGROUND_SERVICES=false in the
API, true in the worker) the watchdog and task runner no longer run inside
the API process. ``/api/v1/system/status`` used to ask its own in-process
singletons and therefore always said "stopped". The services now write a
Redis heartbeat on every loop tick; the endpoint reads it:

- fresh heartbeat            -> "running", source "worker"
- heartbeat older than limit -> "stale"
- no heartbeat               -> "stopped"
- in-process loop running    -> "running", source "local"
"""

import json
from datetime import timedelta

import pytest

from app.redis_client import RedisKeys
from app.services import service_heartbeat
from app.services.task_runner import task_runner
from app.services.watchdog import watchdog
from app.utils import utcnow


@pytest.fixture(autouse=True)
def _api_process_without_local_loops(monkeypatch):
    # The API process in the split setup: the singletons never started here.
    monkeypatch.setattr(watchdog, "_running", False)
    monkeypatch.setattr(task_runner, "_running", False)


async def _write_beat(fake_redis, service: str, age_s: float, **extra):
    payload = {"at": (utcnow() - timedelta(seconds=age_s)).isoformat(), **extra}
    await fake_redis.set(RedisKeys.service_heartbeat(service), json.dumps(payload))


async def test_watchdog_running_in_worker_is_reported_running(auth_client, fake_redis):
    last_check = utcnow().isoformat()
    await _write_beat(
        fake_redis, "watchdog", age_s=5, checks_total=42, last_check_at=last_check,
    )

    resp = await auth_client.get("/api/v1/system/status")

    assert resp.status_code == 200
    wd = resp.json()["components"]["watchdog"]
    assert wd["status"] == "running"
    assert wd["source"] == "worker"
    assert wd["checks_total"] == 42
    assert wd["last_check"] == last_check


async def test_watchdog_old_heartbeat_is_stale(auth_client, fake_redis):
    await _write_beat(fake_redis, "watchdog", age_s=3600, checks_total=7)

    body = (await auth_client.get("/api/v1/system/status")).json()

    wd = body["components"]["watchdog"]
    assert wd["status"] == "stale"
    assert wd["last_seen"] is not None
    assert body["status"] == "degraded"


async def test_watchdog_without_heartbeat_is_stopped(auth_client):
    wd = (await auth_client.get("/api/v1/system/status")).json()["components"]["watchdog"]

    assert wd["status"] == "stopped"
    assert wd["source"] is None


async def test_task_runner_running_in_worker_is_reported_running(auth_client, fake_redis):
    await _write_beat(fake_redis, "task_runner", age_s=10)

    tr = (await auth_client.get("/api/v1/system/status")).json()["components"]["task_runner"]

    assert tr["status"] == "running"
    assert tr["source"] == "worker"


async def test_all_services_fresh_makes_overall_healthy(auth_client, fake_redis):
    await _write_beat(fake_redis, "watchdog", age_s=1, checks_total=1)
    await _write_beat(fake_redis, "task_runner", age_s=1)

    body = (await auth_client.get("/api/v1/system/status")).json()

    assert body["status"] == "healthy"


async def test_local_loop_counts_as_running(auth_client, monkeypatch):
    # Single-process setup (flag true in the API): no worker, loop runs here.
    monkeypatch.setattr(watchdog, "_running", True)

    wd = (await auth_client.get("/api/v1/system/status")).json()["components"]["watchdog"]

    assert wd["status"] == "running"
    assert wd["source"] == "local"


async def test_garbage_heartbeat_is_treated_as_missing(auth_client, fake_redis):
    await fake_redis.set(RedisKeys.service_heartbeat("watchdog"), "not json")

    wd = (await auth_client.get("/api/v1/system/status")).json()["components"]["watchdog"]

    assert wd["status"] == "stopped"


async def test_record_beat_round_trips():
    from app.redis_client import get_redis

    await service_heartbeat.record_beat("watchdog", checks_total=3)

    beat = await service_heartbeat.read_beat("watchdog")

    assert beat is not None
    assert beat["checks_total"] == 3
    assert beat["at"]
    # The key must expire eventually so a removed service reads as stopped.
    redis = await get_redis()
    assert await redis.ttl(RedisKeys.service_heartbeat("watchdog")) > 0


async def test_watchdog_loop_writes_heartbeat(monkeypatch):
    """One loop tick of the real watchdog loop must leave a heartbeat."""
    import asyncio

    from app.services.watchdog.core import WatchdogService

    wd = WatchdogService(interval=30)

    async def _no_checks(self):
        return None

    monkeypatch.setattr(WatchdogService, "_check_all", _no_checks)

    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def _fast_sleep(_s):
        calls["n"] += 1
        if calls["n"] >= 2:  # grace sleep, then stop after the first tick
            wd._running = False
        await real_sleep(0)

    monkeypatch.setattr("app.services.watchdog.core.asyncio.sleep", _fast_sleep)
    wd._running = True
    await wd._run_loop()

    beat = await service_heartbeat.read_beat("watchdog")
    assert beat is not None
    assert beat["checks_total"] == 1


async def test_task_runner_loop_writes_heartbeat(monkeypatch):
    import asyncio

    from app.services.task_runner import TaskRunnerService

    tr = TaskRunnerService(interval=60)

    async def _no_checks(self):
        return None

    monkeypatch.setattr(TaskRunnerService, "_check_tasks", _no_checks)

    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def _fast_sleep(_s):
        calls["n"] += 1
        if calls["n"] >= 2:
            tr._running = False
        await real_sleep(0)

    monkeypatch.setattr("app.services.task_runner.asyncio.sleep", _fast_sleep)
    tr._running = True
    await tr._run_loop()

    assert await service_heartbeat.read_beat("task_runner") is not None
