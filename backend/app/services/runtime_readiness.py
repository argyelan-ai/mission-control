"""Runtime-readiness dispatch gate for power-managed runtimes (unsloth_porsche).

See docs/plans/2026-06-24-porsche-unsloth-runtime-design.md §9.

Power-managed runtimes (the PORSCHE box) sleep when idle. A task must not be
injected into an agent's session while that agent's LLM backend is unreachable,
so this gate is consulted at both dispatch entry points:
  - operations.check_dispatch_allowed  (the push dispatch sites)
  - agents.agent_poll                  (the poll-pull claim path)

GUARANTEE — the "don't break the 24/7 fleet" criterion:
  The gate ONLY affects an agent bound to a power_managed runtime. Agents with
  runtime_id NULL or a non-power_managed runtime (DGX vLLM/LMStudio/unsloth,
  cloud, hermes, ...) return (True, None) immediately — no DB-heavy work, no HTTP
  probe, no behaviour change. settings.enable_runtime_readiness_gate is a global
  kill-switch. Any unexpected error fails OPEN (allows dispatch) so a gate bug can
  never stall the fleet.
"""
import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime

logger = logging.getLogger("mc.runtime_readiness")

_READY_KEY = "mc:runtime:{slug}:ready"


async def _probe_ready(rt: Runtime) -> bool:
    """Live readiness probe via runtime_manager (control plane + OpenAI /v1)."""
    from app.services import runtime_manager

    state = await runtime_manager.get_runtime_state(rt.to_registry_dict())
    return state.get("state") == "ready" and bool(state.get("http_reachable"))


async def is_runtime_ready(rt: Runtime) -> bool:
    """Cached readiness (Redis, short TTL) so the 5s poll loop doesn't hammer the
    control plane. Falls back to a live probe if Redis is unavailable."""
    key = _READY_KEY.format(slug=rt.slug)
    try:
        from app.redis_client import get_redis

        r = await get_redis()
        cached = await r.get(key)
        if cached is not None:
            val = cached.decode() if isinstance(cached, (bytes, bytearray)) else str(cached)
            return val == "1"
        ready = await _probe_ready(rt)
        await r.set(key, "1" if ready else "0", ex=max(1, settings.runtime_readiness_cache_ttl))
        return ready
    except Exception as e:  # redis down / misconfigured → don't fail, just probe live
        logger.debug("runtime readiness cache unavailable (%s) — live probe", e)
        return await _probe_ready(rt)


async def invalidate_readiness(slug: str) -> None:
    """Drop the cached readiness for a runtime. Call on every lifecycle action
    (start/stop/restart/wake) so a state change is reflected on the next poll
    instead of after the TTL — prevents a stale '1' from injecting a task into a
    backend that was just stopped. Best-effort: a Redis outage is non-fatal."""
    try:
        from app.redis_client import get_redis

        r = await get_redis()
        await r.delete(_READY_KEY.format(slug=slug))
    except Exception as e:
        logger.debug("invalidate_readiness(%s) skipped: %s", slug, e)


async def _notify_asleep(session: AsyncSession, rt: Runtime, agent) -> None:
    """One debounced ops event when a task is first held for a sleeping
    power-managed runtime (design §9.1 visibility) — so it is not a silent stall.
    Debounced per-runtime (~5 min) via a Redis marker so the 5s poll loop doesn't
    spam. Fully best-effort: never affects the gate decision."""
    try:
        from app.redis_client import get_redis

        r = await get_redis()
        marker = f"mc:runtime:{rt.slug}:asleep_notified"
        if await r.set(marker, "1", ex=300, nx=True):
            from app.services.activity import emit_event

            await emit_event(
                session,
                "runtime.power_managed_asleep",
                f"Runtime '{rt.display_name}' schläft — ein Task für Agent "
                f"{getattr(agent, 'name', '?')} wartet. Box wecken (Wake-on-LAN).",
                severity="info",
                agent_id=getattr(agent, "id", None),
            )
    except Exception as e:
        logger.debug("asleep notify skipped for %s: %s", getattr(rt, "slug", "?"), e)


async def runtime_ready_for_agent(
    agent, session: AsyncSession
) -> tuple[bool, str | None]:
    """Gate decision for an agent. Returns (allowed, reason).

    (True, None)  → dispatch / poll-claim may proceed (the common case).
    (False, msg)  → the agent's power-managed backend is asleep/not serving;
                    hold the task until the box is woken + the model is loaded.
    """
    if not settings.enable_runtime_readiness_gate:
        return True, None
    rid = getattr(agent, "runtime_id", None)
    if not rid:
        return True, None
    try:
        rt = await session.get(Runtime, rid)
        if rt is None or not getattr(rt, "power_managed", False):
            return True, None
        if await is_runtime_ready(rt):
            return True, None
        await _notify_asleep(session, rt, agent)
        return (
            False,
            f"Runtime '{rt.display_name}' schläft (power-managed) — Box wecken, "
            "bevor Tasks injiziert werden",
        )
    except Exception as e:  # fail OPEN — a gate bug must never stall the fleet
        logger.warning("runtime_ready_for_agent error (fail-open): %s", e)
        return True, None
