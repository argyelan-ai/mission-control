"""Power-managed runtime auto-lifecycle: idle-stop + auto-start-on-demand.

Closes the demand-driven loop for power_managed runtimes (PORSCHE unsloth):
  - idle-stop: a periodic monitor stops the model when the runtime has been
    unused for settings.runtime_idle_stop_minutes → frees the GPU/VRAM.
  - auto-start: when the readiness gate holds a task because the box is asleep
    or the model isn't loaded, MC wakes the box / starts the model (debounced)
    so the held task runs without manual intervention.

GUARANTEE: only ever touches power_managed runtimes. Best-effort + fail-safe —
any error is logged and never blocks dispatch or the loop.
"""
import asyncio
import datetime as dt
import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import engine
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import get_redis

logger = logging.getLogger("mc.runtime_lifecycle")

_LAST_USED_KEY = "mc:runtime:{slug}:last_used"
_AUTOSTART_KEY = "mc:runtime:{slug}:autostart_inflight"
_LOCK_KEY = "mc:runtime_lifecycle:lock"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def mark_runtime_used(slug: str) -> None:
    """Reset the idle timer for a runtime (called when it is actively used)."""
    try:
        r = await get_redis()
        await r.set(_LAST_USED_KEY.format(slug=slug), _now().isoformat())
    except Exception as e:
        logger.debug("mark_runtime_used(%s) skipped: %s", slug, e)


async def _last_used(slug: str) -> dt.datetime | None:
    try:
        r = await get_redis()
        v = await r.get(_LAST_USED_KEY.format(slug=slug))
        if v is None:
            return None
        v = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        return dt.datetime.fromisoformat(v)
    except Exception:
        return None


async def maybe_autostart(runtime: dict) -> None:
    """Wake the box / start the model for a held power-managed runtime.

    Called (fire-and-forget) by the readiness gate when it holds a task because
    the runtime is not ready. Debounced so the 5s poll loop triggers at most one
    action per ~60s; each call performs the next needed step (wake → start).
    `runtime` is a plain dict (to_registry_dict) so it survives outside the
    request's DB session.
    """
    if not settings.enable_runtime_auto_start:
        return
    slug = runtime.get("slug") or runtime.get("id") or "runtime"
    try:
        r = await get_redis()
        if not await r.set(_AUTOSTART_KEY.format(slug=slug), "1", nx=True, ex=60):
            return  # an autostart step already fired recently
        from app.services import runtime_manager

        state = await runtime_manager.get_runtime_state(runtime)
        cs = state.get("container_status")
        if cs == "asleep":
            res = await runtime_manager.wake_runtime(runtime)
            logger.info("auto-start: woke '%s' — %s", slug, res.get("message"))
        elif state.get("state") != "ready":  # booted but model not serving
            res = await runtime_manager.start_runtime(runtime)
            logger.info("auto-start: starting model on '%s' — %s", slug, res.get("message"))
            if res.get("ok"):
                await mark_runtime_used(slug)
    except Exception as e:
        logger.warning("maybe_autostart('%s') error: %s", slug, e)


async def check_and_stop_idle(session: AsyncSession) -> None:
    """Stop the model of any power-managed runtime idle past the threshold."""
    mins = settings.runtime_idle_stop_minutes
    if not mins or mins <= 0:
        return
    runtimes = (
        await session.exec(
            select(Runtime)
            .where(Runtime.power_managed == True)  # noqa: E712
            .where(Runtime.enabled == True)  # noqa: E712
        )
    ).all()
    if not runtimes:
        return

    from app.services import runtime_manager
    from app.services.runtime_readiness import invalidate_readiness

    now = _now()
    for rt in runtimes:
        try:
            bound = (await session.exec(select(Agent).where(Agent.runtime_id == rt.id))).all()
            if any(getattr(a, "current_task_id", None) for a in bound):
                await mark_runtime_used(rt.slug)  # actively in use → reset timer
                continue
            last = await _last_used(rt.slug)
            if last is None:
                await mark_runtime_used(rt.slug)  # establish baseline, grace one cycle
                continue
            idle_min = (now - last).total_seconds() / 60.0
            if idle_min < mins:
                continue
            state = await runtime_manager.get_runtime_state(rt.to_registry_dict())
            if state.get("state") == "ready":
                res = await runtime_manager.stop_runtime(rt.to_registry_dict())
                await invalidate_readiness(rt.slug)
                logger.info(
                    "idle-stop: stopped '%s' after %.0f min idle — %s",
                    rt.slug, idle_min, res.get("message"),
                )
            # reset baseline so a stopped (or unreachable) runtime isn't re-probed
            # every cycle — next probe only after another `mins` of idle.
            await mark_runtime_used(rt.slug)
        except Exception as e:
            logger.warning("idle check error for '%s': %s", getattr(rt, "slug", "?"), e)


class RuntimeLifecycleService:
    """Singleton background loop — idle-stop monitor for power-managed runtimes.
    Multi-worker safe via Redis lock (mirrors task_runner / intelligence)."""

    def __init__(self, interval: int = 120):
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Runtime Lifecycle monitor started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_loop(self) -> None:
        await asyncio.sleep(20)  # grace period after boot
        while self._running:
            try:
                if await self._acquire_lock():
                    async with AsyncSession(engine, expire_on_commit=False) as session:
                        await check_and_stop_idle(session)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Runtime Lifecycle check error: %s", e)
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        try:
            r = await get_redis()
            return bool(await r.set(_LOCK_KEY, "1", nx=True, ex=self._interval))
        except Exception:
            return True


runtime_lifecycle_service = RuntimeLifecycleService()
