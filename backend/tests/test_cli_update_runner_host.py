"""Host-Tool-Update-Pfad (grok) + kimi-sha-Auflösung im cli_update_runner.

Host-Tools ("host": True in TOOLS) haben kein Docker-Image: der Runner ruft
`POST /host-cli/update` auf der Bridge (brew upgrade) und pollt
`/host-cli/update/status` — keine Recreate-Phase, kein Image-Build.
"""
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.redis_client import RedisKeys
from app.services import cli_update_runner as runner
from app.services import sse as sse_mod

from tests.test_cli_update_runner import (  # noqa: F401 — reuse the harness
    FakeManifest,
    _fake_get_redis,
    _harness,
    _mock_transport,
)

pytestmark = pytest.mark.asyncio


async def _read_progress(fake_redis) -> dict:
    raw = await fake_redis.get(RedisKeys.cli_update_progress())
    return json.loads(raw)


async def test_grok_host_update_happy_path(async_session, fake_redis):
    manifest = FakeManifest({"grok": {"version": "0.2.93"}})
    phases: list = []
    called_paths: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_paths.append(request.url.path)
        if request.url.path == "/host-cli/update":
            body = json.loads(request.content)
            assert body["tool"] == "grok"
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/host-cli/update/status":
            return httpx.Response(200, json={
                "state": "success", "tool": "grok",
                "returncode": 0, "log_tail": "==> Upgrading grok-build",
            })
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "0.2.111", "sha256": None},
        handler=handler, phases=phases,
    ) as mocks:
        await runner.run_update("grok", session=async_session)

    # Phasen: manifest → build (brew) → done — KEIN recreate.
    assert phases == ["manifest", "build", "build", "done"] or phases[0] == "manifest"
    assert "recreate" not in phases
    assert phases[-1] == "done"
    # Kein Image-Build-Endpoint berührt, keine Agent-Recreates.
    assert all(not p.startswith("/agent-images") for p in called_paths)
    mocks["mark"].assert_not_awaited()
    mocks["recreate"].assert_not_awaited()
    # Manifest wurde auf die brew-Version gebumpt.
    assert manifest.read()["grok"]["version"] == "0.2.111"

    progress = await _read_progress(fake_redis)
    assert progress["phase"] == "done"
    assert progress["to_version"] == "0.2.111"

    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "cli.updated")
        )
    ).all()
    assert len(events) == 1
    assert events[0].detail["host"] is True


async def test_grok_host_update_failure_rolls_manifest_back(async_session, fake_redis):
    manifest = FakeManifest({"grok": {"version": "0.2.93"}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/host-cli/update":
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/host-cli/update/status":
            return httpx.Response(200, json={
                "state": "failed", "tool": "grok", "returncode": 1,
                "log_tail": "Error: Cask 'grok-build' is not installed.",
            })
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "0.2.111", "sha256": None},
        handler=handler,
    ):
        await runner.run_update("grok", session=async_session)

    # brew scheiterte VOR dem Binary-Tausch → Manifest zurückgerollt.
    assert manifest.read()["grok"]["version"] == "0.2.93"
    progress = await _read_progress(fake_redis)
    assert progress["phase"] == "failed"


async def test_kimi_sha_refetched_when_cache_has_none(async_session, fake_redis):
    """Cache-Hit liefert keine sha — der Runner muss sie fürs kimi-Manifest
    upstream nachladen (offizieller Manifest-Pin, kein TOFU)."""
    manifest = FakeManifest({"kimi": {"version": "0.29.1"}})

    # Update-Check-Cache mit latest, aber ohne sha (so schreibt ihn der Checker).
    await fake_redis.set(
        RedisKeys.cli_versions_cache(),
        json.dumps({"kimi": {"latest": "0.30.0"}}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-images/build":
            body = json.loads(request.content)
            assert body["sha256"] == "abc123"
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/agent-images/build/status":
            return httpx.Response(200, json={
                "state": "success", "tool": "kimi", "returncode": 0, "log_tail": "",
            })
        return httpx.Response(404)

    async with _harness(
        fake_redis, manifest=manifest,
        latest={"version": "0.30.0", "sha256": "abc123"},
        handler=handler,
    ):
        await runner.run_update("kimi", session=async_session)

    entry = manifest.read()["kimi"]
    assert entry["version"] == "0.30.0"
    assert entry["sha256"] == "abc123"
