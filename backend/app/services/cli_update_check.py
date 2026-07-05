"""CLI Tool Update Check (CLI-Tool-Updates, Task 3).

Periodic background check comparing, for each tool in
``cli_versions.TOOLS`` (openclaude/claude/omp):
  - ``installed``: version baked into the locally built Docker image
    (``mc.cli.version`` label)
  - ``target``: version pinned in ``docker/cli-versions.json``
  - ``latest``: latest upstream release (npm registry / GitHub releases)

Result is cached in Redis (``mc:cli:versions``) for the frontend cockpit and,
on a newly available update, emits ``cli.update_available`` once per
tool+version (deduped via ``mc:cli:notified:<tool>:<version>``).

Same lifecycle pattern as RuntimeWatcher/IntelligenceService: singleton,
asyncio loop, Redis lock for multi-worker dedup. ``cli_update_check_interval
= 0`` disables the loop entirely.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.cli_versions import TOOLS, fetch_latest, installed_version, read_manifest

logger = logging.getLogger(__name__)

_NOTIFIED_TTL = 60 * 60 * 24 * 30  # 30 days — long enough to not re-fire on restarts


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in version.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _version_gt(latest: str, target: str | None) -> bool:
    """True if `latest` is strictly newer than `target`.

    target=None (tool missing from manifest, or manifest transiently
    unreadable) is NOT an update: flagging it would fire
    ``cli.update_available`` for every tool on a mere manifest hiccup.
    """
    if not target:
        return False
    try:
        from packaging.version import Version

        return Version(latest) > Version(target)
    except Exception:  # noqa: BLE001 — malformed version string, fall back
        return _version_tuple(latest) > _version_tuple(target)


def _read_manifest_safe() -> dict:
    """``read_manifest()`` raises on missing/corrupt manifest — a single tool
    misconfiguration must not blank out every other tool's `target`."""
    try:
        return read_manifest()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("cli update check: manifest unreadable: %s", e)
        return {}


async def _load_cache(redis) -> dict:
    raw = await redis.get(RedisKeys.cli_versions_cache())
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def _maybe_notify(session: AsyncSession, redis, tool: str, latest: str) -> None:
    key = RedisKeys.cli_update_notified(tool, latest)
    is_new = await redis.set(key, "1", nx=True, ex=_NOTIFIED_TTL)
    if not is_new:
        return
    await emit_event(
        session,
        "cli.update_available",
        f"{tool}: update available ({latest})",
        severity="info",
        detail={"tool": tool, "latest": latest},
    )


async def run_check_once(session: AsyncSession) -> dict:
    """One pass over all CLI tools. Returns the combined view that was
    (partially) written to the Redis cache. Never raises — per-tool
    network/manifest errors are logged and fall back to the previous
    cached entry (or a null-latest stub if there is none yet)."""
    redis = await get_redis()
    manifest = _read_manifest_safe()
    cache = await _load_cache(redis)
    now = _utcnow_iso()
    result: dict = {}

    for tool in TOOLS:
        installed = installed_version(tool)
        target = manifest.get(tool, {}).get("version")

        try:
            latest_data = await fetch_latest(tool)
        except Exception as e:  # noqa: BLE001 — httpx/ValueError/etc, loop must survive
            logger.warning("cli update check: %s fetch_latest failed: %s", tool, e)
            existing = cache.get(tool)
            if existing is not None:
                result[tool] = existing
            else:
                stub = {
                    "installed": installed,
                    "target": target,
                    "latest": None,
                    "update_available": False,
                    "checked_at": now,
                }
                cache[tool] = stub
                result[tool] = stub
            continue

        latest = latest_data["version"]
        update_available = _version_gt(latest, target)
        entry = {
            "installed": installed,
            "target": target,
            "latest": latest,
            "update_available": update_available,
            "checked_at": now,
        }
        cache[tool] = entry
        result[tool] = entry

        if update_available:
            await _maybe_notify(session, redis, tool, latest)

    await redis.set(RedisKeys.cli_versions_cache(), json.dumps(cache))
    return result


class CLIUpdateChecker:
    def __init__(self, interval: int | None = None) -> None:
        self._interval = interval if interval is not None else settings.cli_update_check_interval
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if not self._interval:
            logger.info("cli update checker disabled (interval=0)")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("cli update checker started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if await self._acquire_lock():
                    await self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("cli update checker tick failed")
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """One worker per tick. Redis down → run anyway (single-worker default)."""
        try:
            redis = await get_redis()
            return bool(
                await redis.set(
                    RedisKeys.cli_update_check_lock(), "1",
                    nx=True, ex=max(self._interval - 5, 10),
                )
            )
        except Exception:  # noqa: BLE001
            return True

    async def tick(self, session: AsyncSession | None = None) -> None:
        if session is not None:
            await run_check_once(session)
            return
        from app.services.runtime_model_resolver import session_scope

        async with session_scope() as own_session:
            await run_check_once(own_session)


cli_update_checker = CLIUpdateChecker()
