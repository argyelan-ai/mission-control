"""Runtime → agent model propagation (Runtime & Model Management v1, ADR-054).

When the runtime watcher confirms that an engine serves a different model,
every cli-bridge agent bound to that runtime must reload it. The reload is a
plain ``docker restart``: the container entrypoint re-runs the
``/internal/bootstrap`` call and receives the fresh ``OPENAI_MODEL`` from the
DB row (for the omp image this also re-renders ``models.yml`` and the model
selector). ``respawn_window_only`` is deliberately NOT used here — a window
respawn inherits the stale tmux environment and would keep the old model.

Busy agents are only flagged (``pending_runtime_sync``); the watcher's next
tick retries until the agent is idle. A Redis failure counter trips a circuit
breaker after MAX_SYNC_ATTEMPTS so a broken container cannot restart-loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.agent_runtime_switch import _acquire_lock, _release_lock, is_agent_busy
from app.services.docker_agent_sync import (
    restart_docker_agent_container,
    sync_docker_agent_files,
    wait_for_agent_healthy,
)

logger = logging.getLogger(__name__)

MAX_SYNC_ATTEMPTS = 3
_FAIL_TTL = 3600  # failure counter window (seconds)
_OMP_READY_SIGNALS = ("╭─", "❯")  # omp TUI prompt glyphs (ADR-049)


async def mark_agents_for_sync(session: AsyncSession, runtime: Runtime) -> int:
    """Flag every cli-bridge agent bound to ``runtime`` for a model re-sync.

    Host agents (launchd-managed) are skipped — the model-changed activity
    event is their only notification. Returns the number of flagged agents.
    """
    result = await session.exec(select(Agent).where(Agent.runtime_id == runtime.id))
    flagged = 0
    for agent in result.all():
        if agent.agent_runtime != "cli-bridge":
            continue
        agent.pending_runtime_sync = True
        session.add(agent)
        flagged += 1
    if flagged:
        await session.commit()
    return flagged


async def sync_pending_agents(
    session: AsyncSession,
    *,
    force: bool = False,
    runtime_id: uuid.UUID | None = None,
) -> None:
    """Sync every flagged agent that is idle (or all flagged when ``force``).

    ``runtime_id`` scopes the sync to agents bound to that runtime — used by
    the force-sync-agents endpoint so a manual per-runtime force-sync cannot
    reach (and restart) busy agents bound to unrelated runtimes.
    """
    query = select(Agent).where(Agent.pending_runtime_sync.is_(True))
    if runtime_id is not None:
        query = query.where(Agent.runtime_id == runtime_id)
    result = await session.exec(query)
    for agent in result.all():
        if is_agent_busy(agent) and not force:
            continue
        await _sync_one(session, agent)


async def _sync_one(session: AsyncSession, agent: Agent) -> None:
    runtime = (
        await session.get(Runtime, agent.runtime_id) if agent.runtime_id else None
    )
    if runtime is None:
        # Binding vanished — nothing to sync against.
        agent.pending_runtime_sync = False
        session.add(agent)
        await session.commit()
        return

    # Guard against racing a manual runtime switch in flight: agent_runtime_switch
    # holds this exact key (see agent_runtime_switch._lock_key) for the duration
    # of switch_agent_runtime(). If the lock is already held, skip this tick
    # entirely — do NOT bump the failure counter, the agent stays flagged and
    # the watcher's next tick retries once the switch has released the lock.
    if not await _acquire_lock(agent.id):
        logger.info(
            "model sync for %s skipped — runtime-switch lock is held", agent.name
        )
        return

    try:
        fail_key = RedisKeys.agent_model_sync_fails(str(agent.id))
        try:
            await sync_docker_agent_files(session, agent)
            # Restart is a blocking subprocess call — run off the event loop so
            # the watcher's asyncio loop isn't stalled for seconds per agent.
            result = await asyncio.to_thread(restart_docker_agent_container, agent)
            status = str(result.get("status", ""))
            if status.startswith("error"):
                raise RuntimeError(f"container restart failed: {status}")
            ready = _OMP_READY_SIGNALS if runtime.runtime_type == "omp" else None
            health = await wait_for_agent_healthy(
                agent, timeout=60, respawn_mode=False, ready_signals=ready
            )
            if not health.get("healthy"):
                raise RuntimeError(f"health check failed: {health.get('reason')}")
        except Exception as exc:  # noqa: BLE001 — every failure feeds the breaker
            fails = await _bump_failures(fail_key)
            logger.warning(
                "model sync for %s failed (%s/%s): %s",
                agent.name, fails, MAX_SYNC_ATTEMPTS, exc,
            )
            if fails >= MAX_SYNC_ATTEMPTS:
                agent.pending_runtime_sync = False
                session.add(agent)
                await session.commit()
                await emit_event(
                    session,
                    "agent.model_sync_failed",
                    f"{agent.name}: model sync failed {fails}× — giving up "
                    f"(manual restart required)",
                    severity="error",
                    agent_id=agent.id,
                    detail={"runtime": runtime.slug, "reason": str(exc)},
                )
            return

        await _clear_failures(fail_key)
        agent.pending_runtime_sync = False
        if runtime.model_identifier:
            agent.model = runtime.model_identifier
        session.add(agent)
        await session.commit()
        await emit_event(
            session,
            "agent.model_synced",
            f"{agent.name}: now running "
            f"{runtime.model_identifier or runtime.slug} ({runtime.slug})",
            severity="info",
            agent_id=agent.id,
            detail={"runtime": runtime.slug, "model": runtime.model_identifier},
        )
    finally:
        await _release_lock(agent.id)


async def _bump_failures(key: str) -> int:
    try:
        redis = await get_redis()
        fails = int(await redis.incr(key))
        await redis.expire(key, _FAIL_TTL)
        return fails
    except Exception:  # noqa: BLE001 — Redis optional; worst case we retry forever
        return 1


async def _clear_failures(key: str) -> None:
    try:
        redis = await get_redis()
        await redis.delete(key)
    except Exception:  # noqa: BLE001
        pass
