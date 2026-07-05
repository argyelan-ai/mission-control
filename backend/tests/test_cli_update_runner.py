"""CLI tool update orchestration (CLI-Tool-Updates, Task 6).

Covers the phase machine (manifest → build → recreate → done), manifest
rollback on failure, the double-start lock, unknown-tool guard, the omp TOFU
SHA path, and German bridge-unreachable messaging. httpx is driven with a
MockTransport (respx is not available); propagation is mocked.
"""
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.redis_client import RedisKeys
from app.services import cli_update_runner as runner
from app.services import sse as sse_mod

pytestmark = pytest.mark.asyncio


# ── helpers ───────────────────────────────────────────────────────────────

def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


class FakeManifest:
    """In-memory stand-in for cli_versions manifest read/bump/restore."""

    def __init__(self, data: dict):
        self.data = {k: dict(v) for k, v in data.items()}

    def read(self) -> dict:
        return {k: dict(v) for k, v in self.data.items()}

    def bump(self, tool: str, version: str, sha256=None) -> dict:
        old = self.data.get(tool, {})
        entry: dict = {"version": version}
        if sha256 is not None:
            entry["sha256"] = sha256
        self.data[tool] = entry
        return dict(old)

    def restore(self, tool: str, entry: dict) -> None:
        if entry:
            self.data[tool] = dict(entry)
        else:
            self.data.pop(tool, None)


def _mock_transport(handler):
    """Wrap a request→Response handler in a patched runner._client."""
    transport = httpx.MockTransport(handler)

    def _client(timeout: float = 30.0):
        return httpx.AsyncClient(
            base_url="http://bridge.local", transport=transport, timeout=timeout
        )

    return _client


@asynccontextmanager
async def _harness(
    fake_redis,
    *,
    manifest: FakeManifest,
    latest: dict,
    handler,
    phases: list | None = None,
):
    """Patch out redis, manifest, upstream, bridge, propagation and sleep for a
    run_update call. Records phase order into ``phases`` if provided."""
    mark = AsyncMock(return_value=1)
    recreate = AsyncMock(return_value=None)

    orig_write = runner._write_progress

    async def _spy_write(redis, phase, *a, **k):
        if phases is not None:
            phases.append(phase)
        return await orig_write(redis, phase, *a, **k)

    async def _fetch_latest(tool):
        return dict(latest)

    with patch.object(runner, "get_redis", _fake_get_redis(fake_redis)), \
            patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)), \
            patch.object(runner, "read_manifest", manifest.read), \
            patch.object(runner, "bump_manifest", manifest.bump), \
            patch.object(runner, "restore_manifest_entry", manifest.restore), \
            patch.object(runner, "fetch_latest", _fetch_latest), \
            patch.object(runner, "mark_agents_for_recreate", mark), \
            patch.object(runner, "recreate_pending_agents", recreate), \
            patch.object(runner, "_client", _mock_transport(handler)), \
            patch.object(runner, "_write_progress", _spy_write), \
            patch.object(runner.asyncio, "sleep", AsyncMock()):
        yield {"mark": mark, "recreate": recreate}


async def _read_progress(fake_redis) -> dict:
    raw = await fake_redis.get(RedisKeys.cli_update_progress())
    return json.loads(raw)


async def _events(session) -> list[ActivityEvent]:
    result = await session.exec(select(ActivityEvent))
    return list(result.all())


# ── happy path (npm tool) ─────────────────────────────────────────────────

async def test_happy_path_writes_phases_and_emits(async_session, fake_redis):
    manifest = FakeManifest({"claude": {"version": "2.0.0"}})
    phases: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-images/build":
            body = json.loads(request.content)
            assert body["tool"] == "claude"
            assert body["version"] == "2.1.0"
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/agent-images/build/status":
            return httpx.Response(200, json={
                "state": "success", "tool": "claude",
                "returncode": 0, "log_tail": "done",
            })
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "2.1.0", "sha256": None},
        handler=handler, phases=phases,
    ) as mocks:
        await runner.run_update("claude", session=async_session)

    assert phases[0] == "manifest"
    assert "build" in phases
    assert "recreate" in phases
    assert phases[-1] == "done"

    prog = await _read_progress(fake_redis)
    assert prog["phase"] == "done"
    assert prog["from_version"] == "2.0.0"
    assert prog["to_version"] == "2.1.0"

    # manifest bumped and left in place on success
    assert manifest.data["claude"]["version"] == "2.1.0"

    mocks["mark"].assert_awaited_once()
    mocks["recreate"].assert_awaited_once()

    events = await _events(async_session)
    assert any(e.event_type == "cli.updated" for e in events)
    # lock released
    assert await fake_redis.get(RedisKeys.cli_update_lock()) is None


# ── build failure → rollback ──────────────────────────────────────────────

async def test_build_failure_rolls_back_manifest(async_session, fake_redis):
    manifest = FakeManifest({"claude": {"version": "2.0.0"}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-images/build":
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/agent-images/build/status":
            return httpx.Response(200, json={
                "state": "failed", "tool": "claude",
                "returncode": 1, "log_tail": "boom",
            })
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "2.1.0", "sha256": None},
        handler=handler,
    ) as mocks:
        await runner.run_update("claude", session=async_session)

    prog = await _read_progress(fake_redis)
    assert prog["phase"] == "failed"
    assert "returncode=1" in prog["error"]

    # manifest restored to the original entry
    assert manifest.data["claude"]["version"] == "2.0.0"
    # recreate never reached
    mocks["recreate"].assert_not_awaited()

    events = await _events(async_session)
    assert any(e.event_type == "cli.update_failed" for e in events)
    assert await fake_redis.get(RedisKeys.cli_update_lock()) is None


# ── build 409 (concurrent build) → failed + rollback ──────────────────────

async def test_build_409_fails_and_rolls_back(async_session, fake_redis):
    manifest = FakeManifest({"claude": {"version": "2.0.0"}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-images/build":
            return httpx.Response(409, json={"error": "build läuft bereits"})
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "2.1.0", "sha256": None},
        handler=handler,
    ):
        await runner.run_update("claude", session=async_session)

    prog = await _read_progress(fake_redis)
    assert prog["phase"] == "failed"
    assert "409" in prog["error"]
    assert manifest.data["claude"]["version"] == "2.0.0"


# ── bridge unreachable → German message ───────────────────────────────────

async def test_bridge_unreachable_german_message(async_session, fake_redis):
    manifest = FakeManifest({"claude": {"version": "2.0.0"}})

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "2.1.0", "sha256": None},
        handler=handler,
    ):
        await runner.run_update("claude", session=async_session)

    prog = await _read_progress(fake_redis)
    assert prog["phase"] == "failed"
    assert prog["error"] == "Host-Bridge nicht erreichbar — läuft cli-bridge.py?"
    # bridge died during build POST → manifest was already bumped → rolled back
    assert manifest.data["claude"]["version"] == "2.0.0"


# ── omp TOFU path ─────────────────────────────────────────────────────────

async def test_omp_tofu_fetches_sha_before_bump(async_session, fake_redis):
    manifest = FakeManifest({"omp": {"version": "3.0.0", "sha256": "old"}})
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-images/omp-sha256":
            return httpx.Response(200, json={"sha256": "abc123"})
        if request.url.path == "/agent-images/build":
            seen["build_sha"] = json.loads(request.content)["sha256"]
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/agent-images/build/status":
            return httpx.Response(200, json={
                "state": "success", "returncode": 0, "log_tail": "ok",
            })
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "3.1.0", "sha256": None},
        handler=handler,
    ):
        await runner.run_update("omp", session=async_session)

    # sha256 flowed from the TOFU bridge call into both the build and manifest
    assert seen["build_sha"] == "abc123"
    assert manifest.data["omp"] == {"version": "3.1.0", "sha256": "abc123"}

    prog = await _read_progress(fake_redis)
    assert prog["phase"] == "done"


async def test_omp_sha_failure_fails_before_bump(async_session, fake_redis):
    manifest = FakeManifest({"omp": {"version": "3.0.0", "sha256": "old"}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-images/omp-sha256":
            return httpx.Response(502, json={"error": "asset not found"})
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "3.1.0", "sha256": None},
        handler=handler,
    ) as mocks:
        await runner.run_update("omp", session=async_session)

    prog = await _read_progress(fake_redis)
    assert prog["phase"] == "failed"
    assert "SHA256" in prog["error"]
    # manifest untouched — the failure happened before the bump
    assert manifest.data["omp"] == {"version": "3.0.0", "sha256": "old"}
    mocks["mark"].assert_not_awaited()


# ── build timeout ─────────────────────────────────────────────────────────

async def test_build_timeout_fails_and_rolls_back(async_session, fake_redis):
    manifest = FakeManifest({"claude": {"version": "2.0.0"}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-images/build":
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/agent-images/build/status":
            return httpx.Response(200, json={"state": "running", "log_tail": "…"})
        return httpx.Response(404)

    with patch.object(runner, "BUILD_TIMEOUT", 0):
        async with _harness(
            fake_redis, manifest=manifest,
            latest={"version": "2.1.0", "sha256": None},
            handler=handler,
        ):
            await runner.run_update("claude", session=async_session)

    prog = await _read_progress(fake_redis)
    assert prog["phase"] == "failed"
    assert "Timeout" in prog["error"]
    assert manifest.data["claude"]["version"] == "2.0.0"


# ── start_update guards ───────────────────────────────────────────────────

async def test_start_update_unknown_tool(async_session, fake_redis):
    with patch.object(runner, "get_redis", _fake_get_redis(fake_redis)):
        with pytest.raises(runner.UnknownTool):
            await runner.start_update(async_session, "nope")


async def test_start_update_double_start_raises(async_session, fake_redis):
    created: list = []

    def _fake_create_task(coro):
        # don't actually run the background update; just close the coroutine
        coro.close()
        created.append(coro)
        return None

    with patch.object(runner, "get_redis", _fake_get_redis(fake_redis)), \
            patch.object(runner.asyncio, "create_task", _fake_create_task):
        await runner.start_update(async_session, "claude")
        # lock is now held → second start rejected
        with pytest.raises(runner.UpdateAlreadyRunning):
            await runner.start_update(async_session, "claude")

    assert len(created) == 1
    assert await fake_redis.get(RedisKeys.cli_update_lock()) == "claude"


async def test_start_update_spawns_task_and_holds_lock(async_session, fake_redis):
    spawned: list = []

    def _fake_create_task(coro):
        coro.close()
        spawned.append(coro)
        return None

    with patch.object(runner, "get_redis", _fake_get_redis(fake_redis)), \
            patch.object(runner.asyncio, "create_task", _fake_create_task):
        await runner.start_update(async_session, "omp")

    assert len(spawned) == 1
    assert await fake_redis.get(RedisKeys.cli_update_lock()) == "omp"
