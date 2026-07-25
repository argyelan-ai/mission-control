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
The lock value is a per-run uuid token; ``run_update``'s ``finally`` releases
it only if the stored token is still ours (a run whose lock TTL-expired mid-way
must not delete a second run's lock). The build poll loop refreshes the TTL
every ~60s so a build longer than 1800s doesn't drop its own lock.

Same self-contained-session pattern as ``cli_update_check.tick``: the router
acquires the lock synchronously via ``start_update`` and spawns
``run_update``, which fetches its own session via ``session_scope``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
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
_LOCK_RENEW_EVERY = 60  # refresh the lock TTL at most this often while polling
_PROGRESS_TTL = 1800  # progress record expires so a hard crash can't pin "build"

# Build poll cadence + ceiling. Module-level so tests can shrink them.
POLL_INTERVAL = 5
BUILD_TIMEOUT = 900  # 15 min — an ARM image build + omp download is slow

# Tool → harness (== HARNESS_IMAGES keys); identical to the tool name today
# but kept explicit so the coupling is visible if the two ever diverge.
TOOL_HARNESS: dict[str, str] = {
    "openclaude": "openclaude",
    "claude": "claude",
    "omp": "omp",
    "kimi": "kimi",
    # grok is a HOST tool — no image/recreate phase, see _run_host_update.
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
        await redis.set(
            RedisKeys.cli_update_progress(), json.dumps(payload), ex=_PROGRESS_TTL
        )
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


async def start_update(tool: str) -> str:
    """Synchronous guard for the router: validates the tool, acquires the
    update lock with a per-run token, and spawns the background ``run_update``
    task. Returns the lock token (handed to ``run_update`` for owner-checked
    release).

    Raises ``UnknownTool`` / ``UpdateAlreadyRunning`` so the caller can return
    a 4xx immediately. The spawned task owns lock release.
    """
    if tool not in TOOLS:
        raise UnknownTool(tool)
    redis = await get_redis()
    token = uuid.uuid4().hex
    acquired = await redis.set(
        RedisKeys.cli_update_lock(), token, nx=True, ex=_LOCK_TTL
    )
    if not acquired:
        raise UpdateAlreadyRunning()
    asyncio.create_task(run_update(tool, token=token))
    return token


async def _release_lock(redis, token: str | None) -> None:
    """Owner-checked release: only delete the lock if it still holds our token.
    Without this a run whose lock already TTL-expired (and was re-acquired by a
    second run) would delete the second run's lock. ``token is None`` (direct
    ``run_update`` calls in tests, no lock acquired) falls back to a plain
    delete, which is a no-op when the key is absent."""
    try:
        if token is None:
            await redis.delete(RedisKeys.cli_update_lock())
            return
        current = await redis.get(RedisKeys.cli_update_lock())
        if current == token:
            await redis.delete(RedisKeys.cli_update_lock())
    except Exception:  # noqa: BLE001 — lock self-heals via TTL
        logger.warning("cli update: could not release update lock")


async def run_update(
    tool: str, token: str | None = None, session: AsyncSession | None = None
) -> None:
    """Background entry point. Uses the passed session (tests) or a
    self-contained one (``session_scope``). Assumes the update lock is held
    under ``token``; always releases it (owner-checked) in ``finally``."""
    if session is not None:
        await _run_update(session, tool, token)
        return
    from app.services.runtime_model_resolver import session_scope

    async with session_scope() as own_session:
        await _run_update(own_session, tool, token)


async def _run_update(
    session: AsyncSession, tool: str, token: str | None = None
) -> None:
    harness = TOOL_HARNESS.get(tool, tool)
    redis = None
    from_version: str | None = None
    to_version: str | None = None
    manifest_bumped = False
    rollback_enabled = True  # cleared once the build succeeds (image is new)
    old_entry: dict = {}

    try:
        # get_redis + read_manifest live INSIDE the try so an early failure
        # still writes a failed progress and releases the lock in finally.
        redis = await get_redis()
        from_version = read_manifest().get(tool, {}).get("version")

        # ── Phase: manifest ──────────────────────────────────────────────
        latest = await _resolve_latest(tool, redis)
        to_version = latest["version"]
        sha256 = latest.get("sha256")
        # omp images are pinned by content hash (TOFU). Get the sha BEFORE
        # bumping the manifest so a bridge failure leaves the manifest clean.
        if tool == "omp" and not sha256:
            sha256 = await _fetch_omp_sha256(to_version)
        # kimi ships an official per-platform checksum in its release
        # manifest — the update-check cache carries no sha, so a cache hit
        # must re-fetch it upstream (no TOFU download needed).
        if tool == "kimi" and not sha256:
            sha256 = (await fetch_latest("kimi")).get("sha256")
            if not sha256:
                raise UpdateError(
                    "kimi sha256 konnte nicht aus dem Release-Manifest gelesen werden."
                )

        old_entry = bump_manifest(tool, to_version, sha256=sha256)
        manifest_bumped = True
        await _write_progress(redis, "manifest", tool, from_version, to_version)

        # ── Host-Tool-Pfad (z.B. grok): brew upgrade statt Image-Build ──
        # Kein Docker-Image, kein Recreate — die Bridge führt
        # `brew upgrade --cask <cask>` auf dem Mac aus; laufende TUI-Sessions
        # behalten das alte Binary bis zum nächsten Session-Respawn.
        if TOOLS[tool].get("host"):
            await _write_progress(redis, "build", tool, from_version, to_version)
            resp = await _bridge_post(
                "/host-cli/update", {"tool": tool}, timeout=30.0
            )
            if resp.status_code == 409:
                raise UpdateError("Ein Host-CLI-Update läuft bereits (Bridge meldet 409).")
            if resp.status_code != 200:
                raise UpdateError(
                    f"Bridge lehnte das Host-Update ab: {_error_detail(resp)}"
                )
            await _poll_host_update(redis, tool, from_version, to_version, token=token)
            rollback_enabled = False  # brew hat das Binary bereits getauscht
            await _write_progress(redis, "done", tool, from_version, to_version)
            await emit_event(
                session,
                "cli.updated",
                f"{tool}: aktualisiert auf {to_version} (Host, brew)",
                severity="info",
                detail={
                    "tool": tool,
                    "from_version": from_version,
                    "to_version": to_version,
                    "host": True,
                },
            )
            return

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

        await _poll_build(redis, tool, from_version, to_version, token=token)

        # Build succeeded → the rebuilt image now matches the bumped manifest.
        # From here on a failure must NOT roll the manifest back.
        rollback_enabled = False

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
        if manifest_bumped and rollback_enabled:
            try:
                restore_manifest_entry(tool, old_entry)
            except Exception:  # noqa: BLE001 — rollback best-effort
                logger.exception("cli update: manifest rollback failed for %s", tool)
        elif not rollback_enabled:
            # Build already produced the new image; the manifest stays bumped.
            # Flag that the failure is in the post-build tail, not the update.
            reason = f"Build ok, Recreate/Abschluss fehlgeschlagen: {reason}"
        if redis is not None:
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
        if redis is not None:
            await _release_lock(redis, token)


async def _poll_build(
    redis,
    tool: str,
    from_version: str | None,
    to_version: str | None,
    *,
    token: str | None = None,
) -> None:
    """Poll the bridge build status until success. Mirrors ``log_tail`` into
    the progress record each tick and refreshes the update lock's TTL every
    ``_LOCK_RENEW_EVERY`` seconds (a build can outlast the initial 1800s lease).
    Raises ``UpdateError`` on build failure or after ``BUILD_TIMEOUT`` seconds."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + BUILD_TIMEOUT
    last_renew = start
    while True:
        resp = await _bridge_get("/agent-images/build/status")
        data = resp.json() if resp.status_code == 200 else {}
        state = data.get("state")
        await _write_progress(
            redis, "build", tool, from_version, to_version,
            log_tail=data.get("log_tail"),
        )
        now = loop.time()
        if now - last_renew >= _LOCK_RENEW_EVERY:
            await _renew_lock(redis, token)
            last_renew = now
        if state == "success":
            return
        if state == "failed":
            rc = data.get("returncode")
            raise UpdateError(f"Image-Build fehlgeschlagen (returncode={rc}).")
        if now >= deadline:
            raise UpdateError(f"Image-Build Timeout nach {BUILD_TIMEOUT}s.")
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_host_update(
    redis,
    tool: str,
    from_version: str | None,
    to_version: str | None,
    *,
    token: str | None = None,
) -> None:
    """Poll the bridge host-update status until success — mirrors ``_poll_build``
    (log_tail into progress, lock renewal), against ``/host-cli/update/status``."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + BUILD_TIMEOUT
    last_renew = start
    while True:
        resp = await _bridge_get("/host-cli/update/status")
        data = resp.json() if resp.status_code == 200 else {}
        state = data.get("state")
        await _write_progress(
            redis, "build", tool, from_version, to_version,
            log_tail=data.get("log_tail"),
        )
        now = loop.time()
        if now - last_renew >= _LOCK_RENEW_EVERY:
            await _renew_lock(redis, token)
            last_renew = now
        if state == "success":
            return
        if state == "failed":
            rc = data.get("returncode")
            raise UpdateError(f"Host-CLI-Update fehlgeschlagen (returncode={rc}).")
        if now >= deadline:
            raise UpdateError(f"Host-CLI-Update Timeout nach {BUILD_TIMEOUT}s.")
        await asyncio.sleep(POLL_INTERVAL)


async def _renew_lock(redis, token: str | None) -> None:
    """Owner-checked TTL refresh for the update lock during a long build."""
    if token is None:
        return
    try:
        current = await redis.get(RedisKeys.cli_update_lock())
        if current == token:
            await redis.expire(RedisKeys.cli_update_lock(), _LOCK_TTL)
    except Exception:  # noqa: BLE001 — advisory; the run continues regardless
        logger.warning("cli update: could not renew update lock TTL")
