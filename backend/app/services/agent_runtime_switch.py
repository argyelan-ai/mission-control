"""Atomic agent ↔ runtime switch service (Phase 15 Wave 2).

Wraps the moving parts that PATCH /agents/{id} previously did inline (DB
update, file render, container restart) into a single transaction-shaped
flow with full rollback on failure, optional in-progress override, and an
explicit Redis lock so two concurrent switches on the same agent can't
race each other.

Flow on success:
  1. Validate runtime exists, enabled, agent-type allows switching, compatibility.
  2. Check `current_task_id` busy state (raise unless force_when_in_progress).
  3. Snapshot old state (runtime_id + image).
  4. Acquire Redis lock `mc:agent:{id}:runtime-switch` (TTL 120s).
  5. If image-switch is required: render the new docker-compose.agents.yml
     overlay BEFORE we touch the container.
  6. Update `agent.runtime_id` in the DB (commit).
  7. Re-render claude-config files (sync_docker_agent_files).
  8. Restart the container (force_recreate=image_change).
  9. Wait for the container to be reachable.
 10. On any failure between (5) and (9): full rollback (DB + files + image
     overlay + container) and raise SwitchHealthCheckFailed.
 11. Publish `mc:agent:{id}:terminal:remount` so the Sessions WebSocket re-mounts.
 12. Emit `agent.runtime_switched` activity event.
 13. Release the lock.

Dry-run short-circuits after compatibility validation and returns the
preview payload without mutating anything (used by the UI to surface
warnings + image-switch flag before the user confirms).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.discord import send_discord_notification
from app.services.compose_renderer import (
    detect_image_change,
    write_compose_agents,
)
from app.services.docker_agent_sync import (
    restart_docker_agent_container,
    sync_docker_agent_files,
    wait_for_agent_healthy,
)
from app.utils import utcnow

logger = logging.getLogger("mc.agent_runtime_switch")

LOCK_TTL_SECONDS = 120
HEALTH_TIMEOUT_RECREATE = 90
HEALTH_TIMEOUT_RESTART = 30

# OpenAI-compatible runtime types where a `/models` probe is meaningful.
# Cloud (Anthropic, Ollama) already ship a model_identifier from the seed.
_PROBEABLE_RUNTIME_TYPES = {"vllm_docker", "lmstudio", "openai_compatible", "unsloth", "unsloth_porsche"}


async def probe_runtime_model(runtime: Runtime) -> str | None:
    """Best-effort probe of an OpenAI-compatible `/models` endpoint.

    Returns the first model id reported by the runtime, or None on failure.
    Caller is responsible for persisting the value if desired.
    """
    if not runtime.endpoint:
        return None
    base = runtime.endpoint.rstrip("/")
    # Normalise: vLLM/LM Studio typically have `/v1` baked into the endpoint;
    # bare base URLs are also valid. Both `/v1/models` and `/models` paths are
    # tried so one config style covers all current rows.
    candidates = []
    if base.endswith("/v1"):
        candidates.append(f"{base}/models")
    else:
        candidates.append(f"{base}/v1/models")
        candidates.append(f"{base}/models")
    try:
        import httpx  # local import — already a project dep
    except ImportError:
        logger.warning("probe_runtime_model: httpx unavailable")
        return None
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url in candidates:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                items = data.get("data") if isinstance(data, dict) else None
                if isinstance(items, list) and items:
                    first = items[0]
                    mid = first.get("id") if isinstance(first, dict) else None
                    if isinstance(mid, str) and mid.strip():
                        return mid.strip()
            except Exception as e:
                logger.debug("probe_runtime_model %s failed: %s", url, e)
                continue
    return None


async def ensure_runtime_model_identifier(
    session: AsyncSession, runtime: Runtime
) -> Runtime:
    """If `runtime.model_identifier` is NULL and the type is OpenAI-compatible,
    probe `/models` and persist the result. Returns the (possibly updated) row.
    """
    if runtime.model_identifier:
        return runtime
    if runtime.runtime_type not in _PROBEABLE_RUNTIME_TYPES:
        return runtime
    if not runtime.enabled:
        return runtime
    probed = await probe_runtime_model(runtime)
    if probed:
        runtime.model_identifier = probed
        runtime.updated_at = utcnow() if hasattr(runtime, "updated_at") else None
        session.add(runtime)
        await session.commit()
        await session.refresh(runtime)
        logger.info(
            "ensure_runtime_model_identifier: %s → %s", runtime.slug, probed
        )
    return runtime


# ── Custom exceptions ─────────────────────────────────────────────────────


class RuntimeSwitchError(Exception):
    """Base class so callers can grep one type if they want."""


class RuntimeNotFoundError(RuntimeSwitchError):
    """Target runtime row does not exist."""


class RuntimeIncompatibleError(RuntimeSwitchError):
    """Hard-block: runtime is disabled or otherwise unfit for this agent."""


class AgentNotSwitchableError(RuntimeSwitchError):
    """Host / openclaw agents can't have their runtime switched via MC."""


class AgentBusyError(RuntimeSwitchError):
    """Agent has a `current_task_id` and caller did not force."""

    def __init__(self, message: str, *, current_task_id: uuid.UUID | None = None):
        super().__init__(message)
        self.current_task_id = current_task_id


class SwitchHealthCheckFailed(RuntimeSwitchError):
    """Post-restart health check timed out — rollback was applied."""


class RuntimeSwitchLockTimeout(RuntimeSwitchError):
    """Concurrent switch in flight; we did not acquire the lock."""


# ── Result shape ──────────────────────────────────────────────────────────


@dataclass
class SwitchResult:
    old_runtime: dict[str, Any] | None
    new_runtime: dict[str, Any]
    image_switched: bool
    duration_ms: int
    warnings: list[str]
    dry_run: bool = False
    health: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_runtime": self.old_runtime,
            "new_runtime": self.new_runtime,
            "image_switched": self.image_switched,
            "duration_ms": self.duration_ms,
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
            "health": self.health or None,
        }


def _runtime_summary(rt: Runtime | None) -> dict[str, Any] | None:
    if rt is None:
        return None
    return {
        "id": str(rt.id),
        "slug": rt.slug,
        "display_name": rt.display_name,
        "runtime_type": rt.runtime_type,
        "model_identifier": rt.model_identifier,
        "single_instance": getattr(rt, "single_instance", False),
    }


# ── Public helpers (also used by validators / UI dry-run) ─────────────────


def is_agent_busy(agent: Agent) -> bool:
    """Truthy when the agent has an active task assignment."""
    return getattr(agent, "current_task_id", None) is not None


# Plugins that imply tool-use → if the runtime can't do tool-calls, warn.
# Conservative list; expand as patterns emerge. See PLAN.md T2.3.
_TOOL_USING_PLUGIN_HINTS = ("coding-agent", "github", "search", "bash", "tools")


def _agent_uses_tools(agent: Agent) -> bool:
    """Heuristic: does this agent need tool-calling support?

    cli_plugins == None  → all plugins enabled (default) → tool-rich, return True
    cli_plugins == []    → explicit empty allowlist → no plugins → return False
    cli_plugins == [..]  → True iff any name contains a tool-hint keyword.
    """
    raw = getattr(agent, "cli_plugins", None)
    if raw is None:
        return True
    if not raw:
        return False
    return any(hint in p.lower() for p in raw for hint in _TOOL_USING_PLUGIN_HINTS)


async def validate_compatibility(
    session: AsyncSession,
    agent: Agent,
    runtime: Runtime,
) -> list[str]:
    """Return list of soft-warnings. Hard incompatibilities raise.

    Hard rules (raise RuntimeIncompatibleError):
      - runtime.enabled is False
    Soft rules (warn-only, returned for UI display):
      - agent uses tools, runtime.supports_tools is False
      - runtime is vllm_docker and not ready (state read via runtime_state, best effort)
    """
    if not runtime.enabled:
        raise RuntimeIncompatibleError(
            f"Runtime '{runtime.slug}' ist disabled — zuerst aktivieren."
        )

    warnings: list[str] = []

    if _agent_uses_tools(agent) and not runtime.supports_tools:
        warnings.append(
            f"Agent nutzt Tools — Runtime '{runtime.slug}' unterstuetzt aber kein "
            f"tool-calling. Tool-using prompts werden vermutlich fehlschlagen."
        )

    if runtime.runtime_type == "vllm_docker":
        try:
            from app.services.runtime_state import get_runtime_state_dict  # type: ignore[import-not-found]
            state = await get_runtime_state_dict(runtime)
            if isinstance(state, dict) and state.get("state") not in (None, "ready", "running"):
                warnings.append(
                    f"vLLM-Container '{runtime.slug}' ist aktuell "
                    f"'{state.get('state')}' — Health-Check kann fehlschlagen."
                )
        except ImportError:
            # No runtime_state helper available — fail open.
            pass
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("validate_compatibility runtime-state probe failed: %s", e)

    return warnings


def _ensure_agent_switchable(agent: Agent) -> None:
    rt = getattr(agent, "agent_runtime", None)
    if rt != "cli-bridge":
        raise AgentNotSwitchableError(
            f"Runtime-Switch nicht unterstuetzt fuer Agent-Typ '{rt}'. "
            f"Nur 'cli-bridge' Agents koennen einen Runtime via MC waehlen."
        )


# ── Lock helpers ──────────────────────────────────────────────────────────


def _lock_key(agent_id: uuid.UUID) -> str:
    return f"mc:agent:{agent_id}:runtime-switch"


async def _acquire_lock(agent_id: uuid.UUID) -> bool:
    redis = await get_redis()
    return bool(await redis.set(_lock_key(agent_id), "1", nx=True, ex=LOCK_TTL_SECONDS))


async def _release_lock(agent_id: uuid.UUID) -> None:
    try:
        redis = await get_redis()
        await redis.delete(_lock_key(agent_id))
    except Exception as e:  # pragma: no cover
        logger.warning("release_lock failed for %s: %s", agent_id, e)


async def publish_switch_progress(
    agent_id: uuid.UUID, step: str, *, error: str | None = None
) -> None:
    """Best-effort progress breadcrumbs for the switch modal (TTL 5 min).

    Steps: rendering → restarting → waiting_healthy → done | rolled_back.
    Redis failures are swallowed — progress is cosmetic, never load-bearing.
    """
    try:
        redis = await get_redis()
        payload = json.dumps({"step": step, "error": error, "ts": time.time()})
        await redis.setex(
            RedisKeys.agent_switch_progress(str(agent_id)), 300, payload
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("switch progress publish failed: %s", exc)


def terminal_remount_channel(agent_id: uuid.UUID) -> str:
    """Per-agent Redis pub/sub channel name for terminal remount signals."""
    return f"mc:agent:{agent_id}:terminal:remount"


async def _publish_terminal_remount(agent_id: uuid.UUID, *, image_changed: bool) -> None:
    """Tell the Sessions page that the underlying tmux container changed.

    Routed through `services.sse.broadcast` so the SSE generator can decode
    the payload (it expects `{id, event, data}` shape) without changes.
    """
    try:
        from app.services.sse import broadcast  # local import — avoids cycle
        await broadcast(
            terminal_remount_channel(agent_id),
            "terminal_remount",
            {
                "reason": "runtime_switched",
                "image_changed": image_changed,
                "ts": int(time.time()),
            },
        )
    except Exception as e:  # pragma: no cover
        logger.warning("terminal:remount publish failed for %s: %s", agent_id, e)


# ── Main entrypoint ───────────────────────────────────────────────────────


async def switch_agent_runtime(
    session: AsyncSession,
    agent: Agent,
    new_runtime_id: uuid.UUID,
    *,
    force_when_in_progress: bool = False,
    dry_run: bool = False,
) -> SwitchResult:
    """Atomically switch ``agent`` to ``new_runtime_id``.

    Raises:
        AgentNotSwitchableError: agent is host/openclaw.
        RuntimeNotFoundError: new_runtime_id does not exist.
        RuntimeIncompatibleError: target runtime is disabled.
        AgentBusyError: agent has current_task_id and force is False.
        RuntimeSwitchLockTimeout: another switch is currently running.
        SwitchHealthCheckFailed: post-restart health check timed out (rollback applied).
    """
    started_at = time.monotonic()
    _ensure_agent_switchable(agent)

    new_runtime = await session.get(Runtime, new_runtime_id)
    if new_runtime is None:
        raise RuntimeNotFoundError(f"Runtime {new_runtime_id} not found.")

    # HERM-04 / D-08 / D-09: single_instance hard-block (Phase 24 plan 03).
    # Some runtimes (e.g. Hermes) own their own session lifecycle outside
    # MC's compose-managed agent fleet — switching INTO or OUT OF such a
    # runtime would leave MC and the underlying process in inconsistent
    # state. Generic mechanism: any runtime row flagged single_instance is
    # opaque to the switch service. ``getattr`` keeps this resilient until
    # plan 24-01's migration lands (column defaults to False either way).
    if getattr(new_runtime, "single_instance", False):
        raise AgentNotSwitchableError(
            f"Runtime '{new_runtime.slug}' ist als single_instance markiert "
            f"und kann nicht via Switch gewechselt werden."
        )

    old_runtime: Runtime | None = None
    if agent.runtime_id is not None:
        old_runtime = await session.get(Runtime, agent.runtime_id)
        if old_runtime is not None and getattr(old_runtime, "single_instance", False):
            raise AgentNotSwitchableError(
                f"Agent ist an single_instance Runtime '{old_runtime.slug}' "
                f"gebunden — Switch nicht erlaubt."
            )

    # Auto-fill model_identifier for OpenAI-compatible runtimes that landed in
    # the registry without one (vLLM rows seeded with NULL). Without this the
    # bootstrap omits OPENAI_MODEL and the container falls through to the
    # IMAGE-baked default (glm-5.1:cloud), which silently mismatches the
    # endpoint the agent is actually pointed at.
    new_runtime = await ensure_runtime_model_identifier(session, new_runtime)

    warnings = await validate_compatibility(session, agent, new_runtime)

    if is_agent_busy(agent) and not force_when_in_progress:
        raise AgentBusyError(
            f"Agent {agent.name} hat eine aktive Task ({agent.current_task_id}). "
            f"Force-Toggle aktivieren um trotzdem zu switchen.",
            current_task_id=agent.current_task_id,
        )

    image_change = detect_image_change(old_runtime, new_runtime)

    if dry_run:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        return SwitchResult(
            old_runtime=_runtime_summary(old_runtime),
            new_runtime=_runtime_summary(new_runtime) or {},
            image_switched=image_change,
            duration_ms=elapsed_ms,
            warnings=warnings,
            dry_run=True,
            health={},
        )

    acquired = await _acquire_lock(agent.id)
    if not acquired:
        raise RuntimeSwitchLockTimeout(
            f"Switch fuer Agent {agent.name} laeuft bereits. "
            f"Bitte warten oder Lock manuell loeschen."
        )

    snapshot_old_runtime_id = agent.runtime_id
    await publish_switch_progress(agent.id, "rendering")

    try:
        # Step 5 — render new compose overlay BEFORE touching the container.
        if image_change:
            try:
                # We need the new runtime_id reflected in the DB so the
                # renderer picks the correct image for this agent. Apply
                # the DB change first, then render.
                agent.runtime_id = new_runtime.id
                if new_runtime.model_identifier:
                    agent.model = new_runtime.model_identifier
                agent.updated_at = utcnow()
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                await write_compose_agents(session)
            except Exception as e:
                # Roll back DB and re-raise as health-check failure (cleanest
                # path for the caller — user sees rollback semantics).
                logger.error("compose render failed for %s: %s", agent.name, e)
                agent.runtime_id = snapshot_old_runtime_id
                agent.updated_at = utcnow()
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                await _emit_failure_event(
                    session, agent, old_runtime, new_runtime,
                    reason=f"compose render failed: {e}", elapsed_ms=int((time.monotonic() - started_at) * 1000),
                )
                await publish_switch_progress(
                    agent.id, "rolled_back", error=f"compose render failed: {e}"
                )
                raise SwitchHealthCheckFailed(
                    f"Compose-Render fehlgeschlagen — kein Switch ausgefuehrt: {e}"
                ) from e
        else:
            # Same-image switch: update DB now, no compose change needed.
            agent.runtime_id = new_runtime.id
            if new_runtime.model_identifier:
                agent.model = new_runtime.model_identifier
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()
            await session.refresh(agent)

        # Step 7 — re-render claude-config files with new runtime context.
        try:
            await sync_docker_agent_files(session, agent)
        except Exception as e:
            logger.warning("sync_docker_agent_files during switch failed: %s", e)
            warnings.append(f"sync_docker_agent_files warned: {e}")

        # Step 8 — restart / recreate container.
        # D-11: same-image switches use tmux respawn-window (15-30s saved);
        # cross-image switches still need force_recreate to pull the new image.
        await publish_switch_progress(agent.id, "restarting")
        restart_result = restart_docker_agent_container(
            agent,
            force_recreate=image_change,
            respawn_window_only=(not image_change),
        )
        status = restart_result.get("status", "")
        if status.startswith("error"):
            await _rollback(session, agent, snapshot_old_runtime_id, image_change)
            await _emit_failure_event(
                session, agent, old_runtime, new_runtime,
                reason=f"container restart failed: {status}", elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
            await publish_switch_progress(
                agent.id, "rolled_back", error=f"container restart failed: {status}"
            )
            raise SwitchHealthCheckFailed(
                f"Container-Neustart fehlgeschlagen ({status}) — Rollback ausgefuehrt."
            )

        # Step 9 — wait for container to be reachable.
        # D-12: respawn_mode delegates to tmux capture-pane polling instead of
        # docker inspect, matching the respawn restart path above.
        timeout = HEALTH_TIMEOUT_RECREATE if image_change else HEALTH_TIMEOUT_RESTART
        # ADR-049: the omp runtime now runs omp's native TUI in Window 0 (not the
        # headless bridge print). Anchor readiness on the TUI's prompt glyphs via
        # pane scrape regardless of image_change — the initial openclaude→omp
        # switch is cross-image (respawn_mode=False), whose docker-inspect check
        # would report healthy before the TUI is up. The glyphs match the omp
        # chat prompt box ("╭─" frame + "❯" input) shown after setup-wizard skip.
        is_omp = new_runtime.runtime_type == "omp"
        await publish_switch_progress(agent.id, "waiting_healthy")
        health = await wait_for_agent_healthy(
            agent,
            timeout=timeout,
            respawn_mode=(not image_change),
            ready_signals=("╭─", "❯") if is_omp else None,
        )
        if not health.get("healthy"):
            await _rollback(session, agent, snapshot_old_runtime_id, image_change)
            await _emit_failure_event(
                session, agent, old_runtime, new_runtime,
                reason=f"health check failed: {health.get('reason')}",
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
            await publish_switch_progress(
                agent.id,
                "rolled_back",
                error=f"health check failed: {health.get('reason')}",
            )
            raise SwitchHealthCheckFailed(
                f"Health-Check nach Restart fehlgeschlagen "
                f"({health.get('reason')}) — Rollback ausgefuehrt."
            )

        # Step 11 — broadcast for Sessions auto-remount BEFORE the activity event.
        await _publish_terminal_remount(agent.id, image_changed=image_change)
        await publish_switch_progress(agent.id, "done")

        # Step 12 — success event.
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        await emit_event(
            session,
            "agent.runtime_switched",
            f"{agent.name}: "
            f"{old_runtime.slug if old_runtime else 'n/a'} → {new_runtime.slug}",
            severity="info",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail={
                "old_runtime": _runtime_summary(old_runtime),
                "new_runtime": _runtime_summary(new_runtime),
                "image_switched": image_change,
                "duration_ms": elapsed_ms,
                "warnings": warnings,
            },
        )

        return SwitchResult(
            old_runtime=_runtime_summary(old_runtime),
            new_runtime=_runtime_summary(new_runtime) or {},
            image_switched=image_change,
            duration_ms=elapsed_ms,
            warnings=warnings,
            dry_run=False,
            health=dict(health),
        )

    finally:
        await _release_lock(agent.id)


# ── Internal helpers ──────────────────────────────────────────────────────


async def _rollback(
    session: AsyncSession,
    agent: Agent,
    old_runtime_id: uuid.UUID | None,
    image_change: bool,
) -> None:
    """Restore DB + files + image overlay + container to the pre-switch state."""
    try:
        agent.runtime_id = old_runtime_id
        agent.updated_at = utcnow()
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
    except Exception as e:  # pragma: no cover — defensive
        logger.error("rollback DB step failed for %s: %s", agent.name, e)

    if image_change:
        try:
            await write_compose_agents(session)
        except Exception as e:  # pragma: no cover
            logger.error("rollback compose render failed: %s", e)

    try:
        await sync_docker_agent_files(session, agent)
    except Exception as e:  # pragma: no cover
        logger.error("rollback sync failed: %s", e)

    try:
        restart_docker_agent_container(agent, force_recreate=image_change)
    except Exception as e:
        logger.error("rollback restart failed for %s: %s", agent.name, e)
        # The container may be down while DB shows old (pre-switch) runtime.
        # Surface this broken state through three channels so the operator can act:
        #   1. Activity event (severity=error) — visible in MC UI activity feed.
        #   2. Discord ops notification — pings the operator even if not watching MC.
        #   3. provision_status = "error" — AgentCard shows red error badge in UI.
        try:
            await emit_event(
                session,
                "agent.runtime_rollback_failed",
                f"{agent.name}: Rollback-Neustart fehlgeschlagen — Container manuell prüfen",
                severity="error",
                agent_id=agent.id,
                board_id=agent.board_id,
                detail={
                    "rollback_status": "container_unreachable",
                    "error": str(e),
                    "old_runtime_id": str(old_runtime_id) if old_runtime_id else None,
                },
            )
        except Exception as emit_err:  # pragma: no cover
            logger.error("emit rollback_failed event failed for %s: %s", agent.name, emit_err)

        try:
            await send_discord_notification(
                title=f"🚨 Agent {agent.name} container DOWN after rollback",
                description=(
                    f"Runtime rollback restart failed — container may be unreachable.\n"
                    f"Agent ID: `{agent.id}`\n"
                    f"Error: `{e}`"
                ),
                severity="error",
            )
        except Exception as discord_err:  # pragma: no cover
            logger.error("discord rollback notification failed for %s: %s", agent.name, discord_err)

        try:
            agent.provision_status = "error"
            session.add(agent)
            await session.commit()
        except Exception as db_err:  # pragma: no cover
            logger.error("set provision_status=error failed for %s: %s", agent.name, db_err)


async def _emit_failure_event(
    session: AsyncSession,
    agent: Agent,
    old_runtime: Runtime | None,
    new_runtime: Runtime,
    *,
    reason: str,
    elapsed_ms: int,
) -> None:
    try:
        await emit_event(
            session,
            "agent.runtime_switch_failed",
            f"{agent.name}: Switch fehlgeschlagen ({new_runtime.slug}) — {reason}",
            severity="warning",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail={
                "old_runtime": _runtime_summary(old_runtime),
                "attempted_runtime": _runtime_summary(new_runtime),
                "reason": reason,
                "duration_ms": elapsed_ms,
                "rolled_back": True,
            },
        )
    except Exception as e:  # pragma: no cover
        logger.error("emit failure event failed for %s: %s", agent.name, e)
