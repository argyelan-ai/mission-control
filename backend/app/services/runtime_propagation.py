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
from app.services.agent_runtime_switch import (
    _acquire_lock,
    _lock_key,
    _release_lock,
    is_agent_busy,
)
from app.services.harness_compat import derive_harness
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

    Host agents (launchd-managed) are flagged too when their harness has an
    adapter registered (ADR-060) — ``_sync_one`` reloads them in place via the
    adapter. Host agents without an adapter are skipped — the model-changed
    activity event is their only notification. Returns the number of flagged
    agents.
    """
    from app.services.host_harness_adapter import get_adapter

    result = await session.exec(select(Agent).where(Agent.runtime_id == runtime.id))
    flagged = 0
    for agent in result.all():
        if agent.agent_runtime == "cli-bridge":
            pass
        elif agent.agent_runtime == "host" and get_adapter(
            agent.harness or derive_harness(runtime)
        ) is not None:
            pass
        else:
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

    if agent.agent_runtime == "host":
        # Host agents (launchd) DO go through agent_runtime_switch now
        # (ADR-060 in-place switch for host+adapter agents), so this branch
        # must hold the exact same switch-lock (mc:agent:{id}:runtime-switch,
        # see agent_runtime_switch._lock_key) before mutating agent.env /
        # reloading — otherwise a watcher auto-forward tick can race a manual
        # in-place switch and interleave two SSH session restarts of the same
        # worker. If the lock is already held, behave like the cli-bridge
        # busy/locked case below: skip this tick without bumping the failure
        # counter, stay flagged, and retry once the lock frees.
        from app.services.host_harness_adapter import get_adapter, sync_host_agent_model

        adapter = get_adapter(agent.harness or derive_harness(runtime))
        if adapter is None:
            agent.pending_runtime_sync = False
            session.add(agent)
            await session.commit()
            return
        if not await _acquire_lock(agent.id):
            logger.info(
                "host model sync for %s skipped — runtime-switch lock is held",
                agent.name,
            )
            return
        try:
            fail_key = RedisKeys.agent_model_sync_fails(str(agent.id))
            try:
                await sync_host_agent_model(agent, runtime, session=session)
                await adapter.reload(agent)
            except Exception as exc:  # noqa: BLE001 — surface via circuit breaker
                fails = await _bump_failures(fail_key)
                logger.warning(
                    "host model sync for %s failed (%s/%s): %s",
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


# ── CLI-Tool-Updates: rolling recreate propagation ──────────────────────────
#
# When a newer CLI-tool image is built for a harness, every cli-bridge agent on
# that harness must pick it up. Unlike a model change (a plain restart re-reads
# the DB row), a new image needs a full ``--force-recreate`` so the container
# runs the rebuilt binary. Same idle-flagging + circuit-breaker shape as the
# model-sync pass above; a separate flag and failure key keep the two passes
# independent (an agent can be pending on both at once).

_RECREATE_HEALTH_TIMEOUT = 90  # cold recreate (image pull + bootstrap) is slow


async def mark_agents_for_recreate(session: AsyncSession, harness: str) -> int:
    """Flag every cli-bridge agent on ``harness`` for a container recreate.

    Effective harness = ``agent.harness`` if set, else ``derive_harness`` from
    the bound runtime (legacy NULL rows, ADR-056). Host agents (launchd) are
    skipped — they don't run the cli-bridge image. Returns the flag count.
    """
    result = await session.exec(select(Agent))
    flagged = 0
    for agent in result.all():
        if agent.agent_runtime != "cli-bridge":
            continue
        effective = agent.harness
        if effective is None:
            runtime = (
                await session.get(Runtime, agent.runtime_id)
                if agent.runtime_id
                else None
            )
            effective = derive_harness(runtime)
        if effective != harness:
            continue
        agent.pending_recreate = True
        session.add(agent)
        flagged += 1
    if flagged:
        await session.commit()
    return flagged


async def recreate_pending_agents(
    session: AsyncSession,
    *,
    force: bool = False,
) -> None:
    """Recreate every flagged agent that is idle (or all flagged when ``force``).

    Busy agents stay flagged for the next tick. A held runtime-switch lock skips
    the agent without bumping its failure counter.
    """
    result = await session.exec(select(Agent).where(Agent.pending_recreate.is_(True)))
    for agent in result.all():
        if is_agent_busy(agent) and not force:
            continue
        await _recreate_one(session, agent)


async def _recreate_one(session: AsyncSession, agent: Agent) -> None:
    runtime = (
        await session.get(Runtime, agent.runtime_id) if agent.runtime_id else None
    )

    # Same lock guard as _sync_one: never recreate while a manual runtime switch
    # holds mc:agent:{id}:runtime-switch. Skip the tick WITHOUT bumping the
    # failure counter — the agent stays flagged and retries once the lock frees.
    if not await _acquire_lock(agent.id):
        logger.info(
            "recreate for %s skipped — runtime-switch lock is held", agent.name
        )
        return

    # The base lock TTL (120s) is shorter than a worst-case recreate
    # (force-recreate subprocess up to 90s + health wait up to 90s). Extend it
    # so a concurrently started manual runtime switch cannot grab the expired
    # lock while this container operation is still in flight.
    try:
        redis = await get_redis()
        await redis.expire(_lock_key(agent.id), 300)
    except Exception:  # noqa: BLE001 — best effort, base TTL still applies
        pass

    try:
        fail_key = RedisKeys.agent_recreate_fails(str(agent.id))
        try:
            # force_recreate=True → docker compose up --force-recreate (blocking
            # subprocess) → run off the event loop so the watcher loop isn't
            # stalled. The compose file is unchanged (same image tag, rebuilt
            # content), so no write_compose_agents() is needed here.
            # sync_docker_agent_files is deliberately NOT called (unlike
            # _sync_one): a CLI update changes no rendered config — the .env /
            # settings on disk are current; the recreate only swaps the image.
            result = await asyncio.to_thread(
                restart_docker_agent_container, agent, force_recreate=True
            )
            status = str(result.get("status", ""))
            if status.startswith("error"):
                raise RuntimeError(f"container recreate failed: {status}")
            ready = _OMP_READY_SIGNALS if (
                runtime and runtime.runtime_type == "omp"
            ) else None
            health = await wait_for_agent_healthy(
                agent,
                timeout=_RECREATE_HEALTH_TIMEOUT,
                respawn_mode=False,
                ready_signals=ready,
            )
            if not health.get("healthy"):
                raise RuntimeError(f"health check failed: {health.get('reason')}")
        except Exception as exc:  # noqa: BLE001 — every failure feeds the breaker
            fails = await _bump_failures(fail_key)
            logger.warning(
                "recreate for %s failed (%s/%s): %s",
                agent.name, fails, MAX_SYNC_ATTEMPTS, exc,
            )
            if fails >= MAX_SYNC_ATTEMPTS:
                agent.pending_recreate = False
                session.add(agent)
                await session.commit()
                await emit_event(
                    session,
                    "agent.recreate_failed",
                    f"{agent.name}: CLI-update recreate failed {fails}× — giving "
                    f"up (manual recreate required)",
                    severity="error",
                    agent_id=agent.id,
                    detail={"reason": str(exc)},
                )
            return

        await _clear_failures(fail_key)
        agent.pending_recreate = False
        session.add(agent)
        await session.commit()
        await emit_event(
            session,
            "agent.recreated",
            f"{agent.name}: container recreated — CLI update applied",
            severity="info",
            agent_id=agent.id,
            detail={"harness": agent.harness or (derive_harness(runtime) or "")},
        )
    finally:
        await _release_lock(agent.id)
