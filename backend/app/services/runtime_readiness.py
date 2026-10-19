"""Runtime-readiness dispatch gate — power-managed and local runtimes.

ADR-097 (19.10.2026) extends the gate below to every LOCAL runtime (a runtime
bound to one of our own boxes, ``heads.pairs.is_local``) — but only for the
poll claim (``include_local=True`` from ``agents.agent_poll``): a task for an
agent whose model is switched off waits in the inbox until the model answers
again, instead of being delivered into a session that can never start (live:
DGX Spark off, task 4de17c6a "in progress" for 20 minutes). Push sites
(delegation, manual re-dispatch) are not gated for local runtimes, so a lead
can still queue work while the model is down. The probe is one cached HTTP
GET on the runtime's models endpoint; any answer below 500 counts as up
(auth or a wrong health path must never stall a running box), and every
unexpected error still fails OPEN. Kill switch:
``settings.enable_local_runtime_readiness_gate``.

Original scope (power-managed runtimes, flask_wol):

See ADR-042 (power-managed runtime) and its design plan §9.

Power-managed runtimes (a Wake-on-LAN box) sleep when idle. A task must not be
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

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime

logger = logging.getLogger("mc.runtime_readiness")

_READY_KEY = "mc:runtime:{slug}:ready"
_SERVING_KEY = "mc:runtime:{slug}:serving"


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


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=settings.local_runtime_probe_timeout)


async def _probe_serving(rt: Runtime) -> bool:
    """Is the model server of a local runtime there at all? One GET on its
    models endpoint (same URL rule as runtime_manager). Any HTTP answer below
    500 = up: a 401 (key required) or 404 (unusual health path) still proves
    the engine is listening, and the gate must never hold tasks for a box
    that works. Connection refused / timeout / 5xx (engine warming or
    crashed) = down."""
    from app.services.runtime_manager import join_probe_url

    url = join_probe_url(rt.endpoint, rt.healthcheck_path or "/v1/models")
    try:
        async with _http_client() as client:
            resp = await client.get(url)
    except httpx.HTTPError:
        return False
    return resp.status_code < 500


async def is_local_runtime_serving(rt: Runtime) -> bool:
    """Cached :func:`_probe_serving` (same TTL as the power-managed cache)."""
    key = _SERVING_KEY.format(slug=rt.slug)
    try:
        from app.redis_client import get_redis

        r = await get_redis()
        cached = await r.get(key)
        if cached is not None:
            val = cached.decode() if isinstance(cached, (bytes, bytearray)) else str(cached)
            return val == "1"
        serving = await _probe_serving(rt)
        await r.set(key, "1" if serving else "0", ex=max(1, settings.runtime_readiness_cache_ttl))
        return serving
    except Exception as e:  # redis down → probe live
        logger.debug("local runtime serving cache unavailable (%s) — live probe", e)
        return await _probe_serving(rt)


async def _notify_local_down(session: AsyncSession, rt: Runtime, agent, task) -> None:
    """One event on the held task's timeline (debounced per task, ~10 min) so
    the card says WHY it is still in the inbox. Best-effort."""
    try:
        from app.redis_client import get_redis
        from app.services.activity import emit_event

        task_id = getattr(task, "id", None)
        r = await get_redis()
        marker = f"mc:runtime:{rt.slug}:held:{task_id or getattr(agent, 'id', '?')}"
        if await r.set(marker, "1", ex=600, nx=True):
            await emit_event(
                session,
                "task.dispatch_held_runtime_down",
                f"'{getattr(task, 'title', 'Task')}' wartet — Runtime "
                f"'{rt.display_name}' antwortet nicht. Zustellung, sobald das "
                "Modell läuft.",
                severity="warning",
                agent_id=getattr(agent, "id", None),
                task_id=task_id,
                board_id=getattr(task, "board_id", None),
                detail={"runtime": rt.slug, "endpoint": rt.endpoint},
            )
    except Exception as e:
        logger.debug("held-task notify skipped for %s: %s", getattr(rt, "slug", "?"), e)


async def invalidate_readiness(slug: str) -> None:
    """Drop the cached readiness for a runtime. Call on every lifecycle action
    (start/stop/restart/wake) so a state change is reflected on the next poll
    instead of after the TTL — prevents a stale '1' from injecting a task into a
    backend that was just stopped. Best-effort: a Redis outage is non-fatal."""
    try:
        from app.redis_client import get_redis

        r = await get_redis()
        await r.delete(_READY_KEY.format(slug=slug), _SERVING_KEY.format(slug=slug))
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
    agent, session: AsyncSession, *, include_local: bool = False, task=None,
) -> tuple[bool, str | None]:
    """Gate decision for an agent. Returns (allowed, reason).

    (True, None)  → dispatch / poll-claim may proceed (the common case).
    (False, msg)  → the agent's power-managed backend is asleep/not serving,
                    or (``include_local``, ADR-097) its local model server
                    does not answer; hold the task until it does.
    ``task`` only feeds the timeline note of a held task.
    """
    if not settings.enable_runtime_readiness_gate:
        return True, None
    rid = getattr(agent, "runtime_id", None)
    if not rid:
        return True, None
    try:
        rt = await session.get(Runtime, rid)
        if rt is None:
            return True, None
        if not getattr(rt, "power_managed", False):
            if not (include_local and settings.enable_local_runtime_readiness_gate):
                return True, None
            from app.services.heads.pairs import is_local

            if not is_local(rt):
                return True, None
            if await is_local_runtime_serving(rt):
                return True, None
            await _notify_local_down(session, rt, agent, task)
            return (
                False,
                f"Runtime '{rt.display_name}' antwortet nicht ({rt.endpoint}) — "
                "Modell starten; der Task wird zugestellt, sobald es läuft",
            )
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
