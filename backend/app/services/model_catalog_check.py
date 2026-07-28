"""Provider Model Catalog — background check + "new model" notification.

``services/model_catalog.py`` answers "which models does this provider offer?",
but only when somebody GETs ``/api/v1/models/catalog``. That leaves the original
problem half-solved: Anthropic started shipping ``claude-opus-5`` while MC still
pointed at ``claude-opus-4-8``, and nobody found out — because nobody happened to
open the page.

This module is the missing half: a periodic pass over the same catalog that
raises ``model.new_available`` the first time a model shows up that MC has no
runtime for.

Deliberately the SAME idiom as ``services/cli_update_check.py`` (singleton,
asyncio loop, Redis lock for multi-worker dedup, one long-lived
"already-notified" key per subject). The operator already knows that pattern
from the CLI cockpit — a second idiom would be a second thing to learn.
``model_catalog_check_interval = 0`` disables the loop entirely.


Three rules that keep this quiet (Marks Ärgernis: Notification-Sturm)
---------------------------------------------------------------------
1. **Only ``status == ok`` providers can notify.** A provider that is down or
   whose credential is missing degrades to ``manifest_fallback`` — and the
   manifest is a hand-maintained file that ships e.g. ``claude-opus-5``. Firing
   off that would announce "new models" every time a provider hiccups, which is
   exactly backwards. No live evidence, no notification.
2. **One key per provider+model, 180 days.** Longer than the CLI's 30 days on
   purpose: a pinned CLI version goes stale and a monthly re-nudge is useful,
   whereas a model the operator has consciously chosen NOT to bind stays
   un-bound forever — re-announcing it every month would be pure noise.
3. **A burst collapses into one event.** First tick after a deploy (or after a
   Redis flush) every unbound model looks new at once; a provider that suddenly
   exposes 40 models would do the same. Above ``_MAX_INDIVIDUAL_EVENTS`` the
   models are still all marked notified, but the operator gets a single summary
   line instead of a wall of them. Nothing is silently swallowed.

Severity is ``info`` on purpose: ``emit_event`` pushes warning+ to Discord, and
"a provider added a model" is cockpit information, not an alert.
"""

from __future__ import annotations

import asyncio
import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.model_catalog import STATUS_OK, build_catalog

logger = logging.getLogger(__name__)

EVENT_NEW_MODEL = "model.new_available"

# See rule 2 in the module docstring — deliberately longer than the CLI check's
# 30 days, because an unbound model is a permanent state, not a stale pin.
_NOTIFIED_TTL = 60 * 60 * 24 * 180  # 180 days

# Above this many new models in ONE provider tick, emit a single summary event
# instead of one per model (rule 3).
_MAX_INDIVIDUAL_EVENTS = 3


async def _claim_unnotified(redis, provider_key: str, model_ids: list[str]) -> list[str]:
    """Mark models as notified and return only those that were not already.

    ``SET nx`` is the claim: it both tests and records in one round trip, so two
    workers racing the same tick cannot both win the same model. Claiming
    happens BEFORE the event is emitted — an event lost to a crash is better
    than the same event on every tick forever.

    Redis unreachable → returns ``[]``: without the dedup store there is no way
    to tell "new" from "announced an hour ago", and repeating is the worse
    failure mode.
    """
    fresh: list[str] = []
    for model_id in model_ids:
        try:
            claimed = await redis.set(
                RedisKeys.model_catalog_notified(provider_key, model_id),
                "1",
                nx=True,
                ex=_NOTIFIED_TTL,
            )
        except Exception as exc:  # noqa: BLE001 — Redis down must not spam
            logger.warning("model catalog check: dedup unavailable (%s)", exc)
            return []
        if claimed:
            fresh.append(model_id)
    return fresh


async def _notify(session: AsyncSession, provider: dict, model_ids: list[str]) -> None:
    label = provider.get("label") or provider["key"]
    detail = {
        "provider_key": provider["key"],
        "protocol": provider.get("protocol"),
        "label": label,
        "models": model_ids,
        "count": len(model_ids),
        "runtime_slugs": provider.get("runtime_slugs") or [],
    }
    if len(model_ids) <= _MAX_INDIVIDUAL_EVENTS:
        for model_id in model_ids:
            await emit_event(
                session,
                EVENT_NEW_MODEL,
                f"{label}: new model available ({model_id})",
                severity="info",
                detail={**detail, "models": [model_id], "count": 1},
            )
        return
    # Burst — one line, full list in the detail.
    await emit_event(
        session,
        EVENT_NEW_MODEL,
        f"{label}: {len(model_ids)} new models available",
        severity="info",
        detail=detail,
    )


async def run_check_once(session: AsyncSession) -> dict:
    """One pass over every provider. Never raises.

    Reuses ``build_catalog`` verbatim — it already derives ``bound`` from the
    runtime rows, which IS the definition of "new" (in the catalog, but no
    runtime carries this ``model_identifier``). Re-deriving it here would give
    the page and the notification two different truths.

    ``force=True`` bypasses the 15-minute UI cache: this loop is the freshness
    source, not a reader of stale data — and its probe warms that cache for the
    next page view as a side effect.

    ``build_catalog`` is per-provider resilient by construction: every adapter
    failure is translated into a status inside ``discover_provider``, so one
    unreachable provider cannot abort the pass for the others.
    """
    summary = {"providers_checked": 0, "providers_ok": 0, "new_models": 0, "notified": []}

    try:
        providers = await build_catalog(session, force=True)
    except Exception:  # noqa: BLE001 — DB/Redis hiccup must not kill the loop
        logger.exception("model catalog check: catalog build failed")
        return summary

    try:
        redis = await get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.warning("model catalog check: redis unavailable, no notifications (%s)", exc)
        return summary

    for provider in providers:
        summary["providers_checked"] += 1
        # Rule 1: only a live, authenticated answer may announce anything.
        # manifest_fallback / unreachable / credential_missing stay silent.
        if provider.get("status") != STATUS_OK:
            continue
        summary["providers_ok"] += 1

        new_ids = [m["id"] for m in provider.get("models", []) if not m.get("bound")]
        if not new_ids:
            continue

        fresh = await _claim_unnotified(redis, provider["key"], new_ids)
        if not fresh:
            continue

        try:
            await _notify(session, provider, fresh)
        except Exception:  # noqa: BLE001 — one bad provider must not stop the rest
            logger.exception(
                "model catalog check: emitting event for %s failed", provider["key"]
            )
            continue

        summary["new_models"] += len(fresh)
        summary["notified"].extend(f"{provider['key']}:{mid}" for mid in fresh)

    if summary["new_models"]:
        logger.info(
            "model catalog check: %s new model(s): %s",
            summary["new_models"],
            ", ".join(summary["notified"]),
        )
    return summary


class ModelCatalogChecker:
    """Same lifecycle contract as ``CLIUpdateChecker`` — start/stop from the
    app lifespan, one Redis-locked tick per interval."""

    def __init__(self, interval: int | None = None) -> None:
        self._interval = (
            interval if interval is not None else settings.model_catalog_check_interval
        )
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if not self._interval:
            logger.info("model catalog checker disabled (interval=0)")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("model catalog checker started (interval=%ss)", self._interval)

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
                logger.exception("model catalog checker tick failed")
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """One worker per tick. Redis down → run anyway (single-worker default);
        the notification dedup has its own Redis guard and stays silent then."""
        try:
            redis = await get_redis()
            return bool(
                await redis.set(
                    RedisKeys.model_catalog_check_lock(), "1",
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


model_catalog_checker = ModelCatalogChecker()
