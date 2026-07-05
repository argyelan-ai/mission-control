"""CLI tool update orchestration (CLI-Tool-Updates, Task 6).

Drives the full update of a single CLI tool (openclaude/claude/omp) from a
newer upstream release into the running agent fleet. The flow, run as a
background asyncio task and reported phase-by-phase into Redis
(``mc:cli:update-progress``):

  manifest → bump ``docker/cli-versions.json`` to the target version
             (for omp: acquire the release SHA256 via the host bridge first)
  build    → tell the host cli-bridge to rebuild the agent image, then poll
             its build status until success/failure (or timeout)
  recreate → flag every cli-bridge agent on the tool's harness and recreate
             the idle ones so they pick up the rebuilt image
  done     → emit ``cli.updated``

Any failure rolls the manifest back to its captured entry, writes a ``failed``
progress record with a German reason, and emits ``cli.update_failed``.

A Redis lock (``mc:cli:update-lock``, TTL 1800s, ``set nx``) serializes
updates: a second start while one is running raises ``UpdateAlreadyRunning``.
The lock is always released in ``run_update``'s ``finally``.

Same self-contained-session pattern as ``cli_update_check.tick``: the router
acquires the lock synchronously via ``start_update`` and spawns
``run_update``, which fetches its own session via ``session_scope``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.cli_versions import (
    TOOLS,
    bump_manifest,
    fetch_latest,
    read_manifest,
    restore_manifest_entry,
)
from app.services.runtime_propagation import (
    mark_agents_for_recreate,
    recreate_pending_agents,
)

logger = logging.getLogger(__name__)

_LOCK_TTL = 1800  # 30 min — a stuck update self-heals via TTL expiry

# Build poll cadence + ceiling. Module-level so tests can shrink them.
POLL_INTERVAL = 5
BUILD_TIMEOUT = 900  # 15 min — an ARM image build + omp download is slow

# Tool → harness (== HARNESS_IMAGES keys); identical to the tool name today
# but kept explicit so the coupling is visible if the two ever diverge.
TOOL_HARNESS: dict[str, str] = {
    "openclaude": "openclaude",
    "claude": "claude",
    "omp": "omp",
}

_BRIDGE_UNREACHABLE = "Host-Bridge nicht erreichbar — läuft cli-bridge.py?"


class UnknownTool(Exception):
    """Raised when an update is requested for a tool not in ``TOOLS``."""


class UpdateAlreadyRunning(Exception):
    """Raised when the update lock is already held by another run."""


class UpdateError(Exception):
    """A recoverable phase failure — triggers manifest rollback + failed event."""


class BridgeUnreachable(UpdateError):
    """The host cli-bridge could not be reached over HTTP."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Async httpx client bound to the host bridge. Isolated in one helper so
    tests can patch it to inject an ``httpx.MockTransport``."""
    return httpx.AsyncClient(
        base_url=settings.free_code_bridge_url, timeout=timeout
    )


async def _bridge_post(path: str, body: dict, timeout: float = 30.0) -> httpx.Response:
    try:
        async with _client(timeout) as client:
            return await client.post(path, json=body)
    except httpx.HTTPError as e:
        raise BridgeUnreachable(_BRIDGE_UNREACHABLE) from e


async def _bridge_get(path: str, timeout: float = 15.0) -> httpx.Response:
    try:
        async with _client(timeout) as client:
            return await client.get(path)
    except httpx.HTTPError as e:
        raise BridgeUnreachable(_BRIDGE_UNREACHABLE) from e


async def _write_progress(
    redis,
    phase: str,
    tool: str,
    from_version: str | None,
    to_version: str | None,
    *,
    log_tail: str | None = None,
    error: str | None = None,
) -> None:
    payload: dict = {
        "phase": phase,
        "tool": tool,
        "from_version": from_version,
        "to_version": to_version,
        "updated_at": _utcnow_iso(),
    }
    if log_tail is not None:
        payload["log_tail"] = log_tail
    if error is not None:
        payload["error"] = error
    try:
        await redis.set(RedisKeys.cli_update_progress(), json.dumps(payload))
    except Exception:  # noqa: BLE001 — progress is advisory, never fail the run on it
        logger.warning("cli update: could not write progress (%s)", phase)


async def _resolve_latest(tool: str, redis) -> dict:
    """Target version + sha for the update. Prefers the update-check cache
    (avoids a redundant upstream call); falls back to ``fetch_latest``. The
    cache carries no sha256, so a cache hit for omp forces the TOFU path."""
    try:
        raw = await redis.get(RedisKeys.cli_versions_cache())
        if raw:
            entry = json.loads(raw).get(tool)
            if entry and entry.get("latest"):
                return {"version": entry["latest"], "sha256": None}
    except Exception:  # noqa: BLE001 — cache is optional
        pass
    return await fetch_latest(tool)


async def _fetch_omp_sha256(version: str) -> str:
    """TOFU: ask the host bridge to download the omp release asset and hash it."""
    resp = await _bridge_post("/agent-images/omp-sha256", {"version": version})
    if resp.status_code != 200:
        detail = _error_detail(resp)
        raise UpdateError(f"omp SHA256 konnte nicht ermittelt werden: {detail}")
    sha = resp.json().get("sha256")
    if not sha:
        raise UpdateError("omp SHA256 konnte nicht ermittelt werden: leere Antwort")
    return sha


def _error_detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", resp.status_code))
    except Exception:  # noqa: BLE001
        return str(resp.status_code)


async def start_update(session: AsyncSession, tool: str) -> None:
    """Synchronous guard for the router: validates the tool, acquires the
    update lock, and spawns the background ``run_update`` task.

    Raises ``UnknownTool`` / ``UpdateAlreadyRunning`` so the caller can return
    a 4xx immediately. The spawned task owns lock release.
    """
    if tool not in TOOLS:
        raise UnknownTool(tool)
    redis = await get_redis()
    acquired = await redis.set(
        RedisKeys.cli_update_lock(), tool, nx=True, ex=_LOCK_TTL
    )
    if not acquired:
        raise UpdateAlreadyRunning()
    asyncio.create_task(run_update(tool))


async def run_update(tool: str, session: AsyncSession | None = None) -> None:
    """Background entry point. Uses the passed session (tests) or a
    self-contained one (``session_scope``). Assumes the update lock is held;
    always releases it in ``finally``."""
    if session is not None:
        await _run_update(session, tool)
        return
    from app.services.runtime_model_resolver import session_scope

    async with session_scope() as own_session:
        await _run_update(own_session, tool)


async def _run_update(session: AsyncSession, tool: str) -> None:
    redis = await get_redis()
    harness = TOOL_HARNESS.get(tool, tool)
    from_version = read_manifest().get(tool, {}).get("version")
    to_version: str | None = None
    manifest_bumped = False
    old_entry: dict = {}

    try:
        # ── Phase: manifest ──────────────────────────────────────────────
        latest = await _resolve_latest(tool, redis)
        to_version = latest["version"]
        sha256 = latest.get("sha256")
        # omp images are pinned by content hash (TOFU). Get the sha BEFORE
        # bumping the manifest so a bridge failure leaves the manifest clean.
        if tool == "omp" and not sha256:
            sha256 = await _fetch_omp_sha256(to_version)

        old_entry = bump_manifest(tool, to_version, sha256=sha256)
        manifest_bumped = True
        await _write_progress(redis, "manifest", tool, from_version, to_version)

        # ── Phase: build ─────────────────────────────────────────────────
        await _write_progress(redis, "build", tool, from_version, to_version)
        resp = await _bridge_post(
            "/agent-images/build",
            {"tool": tool, "version": to_version, "sha256": sha256},
        )
        if resp.status_code == 409:
            raise UpdateError("Ein Image-Build läuft bereits (Bridge meldet 409).")
        if resp.status_code != 200:
            raise UpdateError(f"Bridge lehnte den Build ab: {_error_detail(resp)}")

        await _poll_build(redis, tool, from_version, to_version)

        # ── Phase: recreate ──────────────────────────────────────────────
        await _write_progress(redis, "recreate", tool, from_version, to_version)
        await mark_agents_for_recreate(session, harness)
        await recreate_pending_agents(session)

        # ── Phase: done ──────────────────────────────────────────────────
        await _write_progress(redis, "done", tool, from_version, to_version)
        await emit_event(
            session,
            "cli.updated",
            f"{tool}: aktualisiert auf {to_version}",
            severity="info",
            detail={
                "tool": tool,
                "from_version": from_version,
                "to_version": to_version,
            },
        )

    except Exception as exc:  # noqa: BLE001 — every failure path is uniform
        reason = str(exc) if isinstance(exc, UpdateError) else f"Unerwarteter Fehler: {exc}"
        if manifest_bumped:
            try:
                restore_manifest_entry(tool, old_entry)
            except Exception:  # noqa: BLE001 — rollback best-effort
                logger.exception("cli update: manifest rollback failed for %s", tool)
        await _write_progress(
            redis, "failed", tool, from_version, to_version, error=reason
        )
        logger.warning("cli update failed for %s: %s", tool, reason)
        try:
            await emit_event(
                session,
                "cli.update_failed",
                f"{tool}: Update fehlgeschlagen — {reason}",
                severity="error",
                detail={
                    "tool": tool,
                    "from_version": from_version,
                    "to_version": to_version,
                    "error": reason,
                },
            )
        except Exception:  # noqa: BLE001 — never mask the original failure
            logger.exception("cli update: could not emit cli.update_failed")
    finally:
        try:
            await redis.delete(RedisKeys.cli_update_lock())
        except Exception:  # noqa: BLE001 — lock self-heals via TTL
            logger.warning("cli update: could not release update lock")


async def _poll_build(
    redis, tool: str, from_version: str | None, to_version: str | None
) -> None:
    """Poll the bridge build status until success. Mirrors ``log_tail`` into
    the progress record each tick. Raises ``UpdateError`` on build failure or
    after ``BUILD_TIMEOUT`` seconds."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + BUILD_TIMEOUT
    while True:
        resp = await _bridge_get("/agent-images/build/status")
        data = resp.json() if resp.status_code == 200 else {}
        state = data.get("state")
        await _write_progress(
            redis, "build", tool, from_version, to_version,
            log_tail=data.get("log_tail"),
        )
        if state == "success":
            return
        if state == "failed":
            rc = data.get("returncode")
            raise UpdateError(f"Image-Build fehlgeschlagen (returncode={rc}).")
        if loop.time() >= deadline:
            raise UpdateError(f"Image-Build Timeout nach {BUILD_TIMEOUT}s.")
        await asyncio.sleep(POLL_INTERVAL)
