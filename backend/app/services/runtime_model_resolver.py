"""Runtime Model Resolver.

Central source of truth for the active LLM model identifier per runtime.
Replaces hard-coded constants like ``MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"`` in
services that talk to vLLM/LM Studio endpoints.

Strategy
--------
1. Find runtime in DB by slug or endpoint
2. Return ``runtime.model_identifier`` if set (warm cache via Redis, 5 min TTL)
3. Otherwise probe ``/v1/models`` via ``probe_runtime_model`` and persist
4. On 404 from downstream, callers call :func:`invalidate_and_reprobe`

Why this exists
---------------
Before this module, three places hard-coded ``"Qwen/Qwen3.6-35B-A3B-FP8"``:
``spark_client.SparkClient.LLM_MODEL``, ``news_ai_worker.MODEL``, and
``config.spark_llm_model``. When the sparkrun recipe changed (e.g. to
PrismaQuant), every caller silently 404'd because the model name in the
request body no longer matched what vLLM was serving. This resolver makes
model identity DB-driven and self-healing.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import UUID

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import engine
from app.models.runtime import Runtime
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Cache TTL — model_identifier rarely changes but we re-validate periodically
# so a recipe swap on the Spark side is picked up within this window even if
# nothing else triggered an explicit re-probe.
_CACHE_TTL = 300  # 5 min
# Brief negative-cache to avoid hammering a down endpoint on every call.
_NEGATIVE_TTL = 30
_CACHE_PREFIX = "mc:runtime-model"
_SENTINEL_NONE = "__none__"
# Live /v1/models lookup when several runtime rows share the Spark endpoint.
_LIVE_MODELS_TIMEOUT_S = 3.0
_LIVE_MODELS_TTL = 20


def _cache_key(slug: str) -> str:
    return f"{_CACHE_PREFIX}:{slug}"


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Create a self-contained session for callers without one (background tasks).

    Public on purpose — used by ``spark_client`` for
    its 404-retry path so they can call :func:`invalidate_and_reprobe`
    without dragging a request-bound session through their public APIs.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def _load_runtime(session: AsyncSession, slug_or_id: str) -> Runtime | None:
    """Look up runtime by slug or UUID string."""
    result = await session.exec(select(Runtime).where(Runtime.slug == slug_or_id))
    runtime = result.first()
    if runtime is not None:
        return runtime
    try:
        rt_id = UUID(slug_or_id)
    except (ValueError, AttributeError):
        return None
    result = await session.exec(select(Runtime).where(Runtime.id == rt_id))
    return result.first()


async def _cache_get(slug: str) -> str | None:
    """Returns cached value, ``_SENTINEL_NONE`` for negative cache, or ``None`` on miss."""
    try:
        client = await get_redis()
        cached = await client.get(_cache_key(slug))
        if cached is None:
            return None
        return cached.decode() if isinstance(cached, bytes) else cached
    except Exception as exc:  # noqa: BLE001 — cache is optional
        logger.debug("resolver cache read failed for %s: %s", slug, exc)
        return None


async def _cache_set(slug: str, value: str, *, ttl: int = _CACHE_TTL) -> None:
    try:
        client = await get_redis()
        await client.setex(_cache_key(slug), ttl, value)
    except Exception as exc:  # noqa: BLE001 — cache is optional
        logger.debug("resolver cache write failed for %s: %s", slug, exc)


async def _cache_delete(slug: str) -> None:
    try:
        client = await get_redis()
        await client.delete(_cache_key(slug))
    except Exception as exc:  # noqa: BLE001 — cache is optional
        logger.debug("resolver cache delete failed for %s: %s", slug, exc)


async def invalidate_cached_model(slug: str) -> None:
    """Public cache invalidation — used by the runtime watcher after it
    persists a drift-confirmed model change, so resolver consumers stop
    serving the stale identifier within the same tick."""
    await _cache_delete(slug)


async def get_active_model_for_runtime(
    session: AsyncSession,
    slug_or_id: str,
    *,
    force_probe: bool = False,
) -> str | None:
    """Return the active model_identifier for a runtime.

    Args:
        session: AsyncSession (caller-supplied — typical for request handlers)
        slug_or_id: runtime slug (e.g. ``"qwen-general"``) or UUID string
        force_probe: skip cache + DB hint, probe ``/v1/models`` immediately

    Returns:
        Model identifier string or ``None`` if the runtime row is missing
        and the endpoint probe also failed.
    """
    if not force_probe:
        cached = await _cache_get(slug_or_id)
        if cached == _SENTINEL_NONE:
            return None
        if cached:
            return cached

    runtime = await _load_runtime(session, slug_or_id)
    if runtime is None:
        logger.warning("resolver: runtime %s not found", slug_or_id)
        return None

    # Trust the DB row if it has a value and we're not forcing a probe.
    if runtime.model_identifier and not force_probe:
        await _cache_set(slug_or_id, runtime.model_identifier)
        return runtime.model_identifier

    # Probe and persist
    from app.services.agent_runtime_switch import probe_runtime_model

    probed = await probe_runtime_model(runtime)
    if probed:
        if runtime.model_identifier != probed:
            old = runtime.model_identifier
            runtime.model_identifier = probed
            session.add(runtime)
            await session.commit()
            await session.refresh(runtime)
            logger.info(
                "resolver auto-detected change for %s: %r → %r",
                runtime.slug, old, probed,
            )
        await _cache_set(slug_or_id, probed)
        return probed

    # Probe failed — fall back to whatever the DB knew (may be ``None``) and
    # cache the miss briefly so we don't probe-storm a down endpoint.
    if runtime.model_identifier:
        await _cache_set(slug_or_id, runtime.model_identifier, ttl=_NEGATIVE_TTL)
        return runtime.model_identifier
    await _cache_set(slug_or_id, _SENTINEL_NONE, ttl=_NEGATIVE_TTL)
    return None


async def resolve_agent_model(session: AsyncSession, agent) -> str | None:
    """The model an agent actually runs — from its bound runtime, nothing else.

    ``runtime.model_identifier`` is the single source of truth (model-sanitation
    2026-07-25). The legacy ``agent.model`` free-text column is NOT consulted:
    it is write-only display data (set by the propagation paths AFTER a sync,
    surfaced in the UI) and using it as an input is exactly how stale model
    names leaked back into provisioning payloads.

    Returns ``None`` when the agent has no runtime, the runtime row is gone, or
    the runtime has no model_identifier. Callers must fail loudly on ``None``
    instead of substituting a guess — a wrong model name is a silent
    misconfiguration, a missing one is a fixable error message.

    Deliberately does NOT probe ``/v1/models`` or touch Redis: this runs inside
    request handlers where a network round-trip to a down endpoint would turn a
    fast 400 into a timeout. Use ``get_active_model_for_runtime`` when a probe
    and cache are actually wanted.
    """
    runtime_id = getattr(agent, "runtime_id", None)
    if not runtime_id:
        return None
    runtime = await session.get(Runtime, runtime_id)
    if runtime is None:
        return None
    return runtime.model_identifier or None


async def invalidate_and_reprobe(
    session: AsyncSession,
    slug_or_id: str,
) -> str | None:
    """Wipe cache + re-probe + persist. Use after a 404 from the runtime.

    Auto-recovery for the case where a recipe swap on the inference side
    changed the model identifier and downstream callers were still using
    the cached old name.
    """
    await _cache_delete(slug_or_id)
    return await get_active_model_for_runtime(session, slug_or_id, force_probe=True)


# ── Standalone helpers (background tasks / services without a session) ──────


async def spark_runtime_candidates(session: AsyncSession) -> list[Runtime]:
    """Every enabled runtime row whose endpoint targets the Spark host:port.

    Match by endpoint only — the netloc uniquely identifies the physical GPU
    serving on Spark:8000. Deliberately NOT filtered by runtime_type: the
    fleet's Spark runtime has been vllm_docker, omp, ssh_process and
    vllm_node at various times, and a type filter silently returns nothing
    whenever it changes (which broke the news worker's model resolution —
    it then fell back to a stale config default). The host:port comes from
    settings.spark_llm_url (env-configurable) instead of a hardcoded LAN IP.

    Several rows match in practice: one per recipe that was ever started on
    the box plus the box's slot row — all enabled, only one of them serving.
    """
    from urllib.parse import urlparse

    spark_netloc = urlparse(settings.spark_llm_url).netloc
    stmt = (
        select(Runtime)
        .where(Runtime.endpoint.like(f"%{spark_netloc}%"))  # type: ignore[union-attr]
        .where(Runtime.enabled.is_(True))  # type: ignore[union-attr]
    )
    return list((await session.exec(stmt)).all())


async def _live_spark_model_ids() -> list[str] | None:
    """Model ids ``settings.spark_llm_url`` serves right now (``/v1/models``),
    or ``None`` when the box does not answer. Short timeout, cached briefly
    in Redis — this runs on every model resolution while several runtime
    rows share the endpoint."""
    import json

    import httpx

    key = f"{_CACHE_PREFIX}:spark-live-models"
    try:
        client = await get_redis()
        cached = await client.get(key)
        if cached is not None:
            return json.loads(cached.decode() if isinstance(cached, bytes) else cached)
    except Exception as exc:  # noqa: BLE001 — cache is optional
        logger.debug("resolver live-models cache read failed: %s", exc)

    base = settings.spark_llm_url.rstrip("/")
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=_LIVE_MODELS_TIMEOUT_S) as cli:
            resp = await cli.get(url)
            resp.raise_for_status()
            ids = [
                m["id"] for m in (resp.json().get("data") or [])
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.debug("resolver: live /v1/models of the Spark failed: %s", exc)
        return None
    try:
        client = await get_redis()
        await client.setex(key, _LIVE_MODELS_TTL, json.dumps(ids))
    except Exception as exc:  # noqa: BLE001 — cache is optional
        logger.debug("resolver live-models cache write failed: %s", exc)
    return ids


def _newest_first(rows: list[Runtime]) -> list[Runtime]:
    """Recipe rows before the slot row (the recipe row carries the real
    limits, e.g. max_context_len), then the most recently started/updated."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _aware(value: datetime | None) -> datetime:
        if value is None:
            return epoch
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    return sorted(
        rows,
        key=lambda r: (
            bool(getattr(r, "is_slot", False)),
            -_aware(r.serving_since).timestamp(),
            -_aware(r.updated_at).timestamp(),
            r.slug,
        ),
    )


async def get_spark_vllm_runtime(session: AsyncSession) -> Runtime | None:
    """The runtime row of what the Spark GPU serves right now.

    One enabled row at the Spark endpoint: that one. Several (one per recipe
    ever started there, plus the slot row — round-1 user test 23.10.2026:
    ``first()`` picked the STOPPED GLM row while the box served DeepSeek, so
    every ``get_active_spark_model`` caller named the wrong model):

    1. the rows whose ``model_identifier`` the box lists in ``/v1/models``;
    2. box silent: the rows matching the slot row's model — the slot row is
       written on every confirmed recipe start, so it names the recipe that
       was switched to last;
    3. otherwise all rows.

    Within the pick: recipe rows before the slot row, newest first.
    """
    rows = await spark_runtime_candidates(session)
    if len(rows) <= 1:
        return rows[0] if rows else None

    live = await _live_spark_model_ids()
    pool: list[Runtime] = []
    if live:
        pool = [r for r in rows if r.model_identifier and r.model_identifier in live]
    if not pool:
        slot_models = {
            r.model_identifier for r in rows
            if getattr(r, "is_slot", False) and r.model_identifier
        }
        pool = [r for r in rows if r.model_identifier in slot_models]
    if not pool:
        pool = rows
    return _newest_first(pool)[0]


async def get_active_spark_model(*, force_probe: bool = False) -> str | None:
    """Standalone shortcut: returns active model for the Spark vLLM runtime.

    Self-contained — creates its own DB session. Use this from background
    tasks (e.g. vault backfill) that don't have a request-bound
    session in scope.
    """
    async with session_scope() as session:
        runtime = await get_spark_vllm_runtime(session)
        if runtime is not None:
            model = await get_active_model_for_runtime(
                session, runtime.slug, force_probe=force_probe
            )
            if model:
                return model
        # No runtime row matched, or it couldn't resolve a model. Probe whatever
        # spark_llm_url is actually serving right now so callers stay fully
        # model-agnostic ("whatever GPU is running"), independent of DB
        # bookkeeping. This is the omp/Sparky-style self-healing path.
        probed = await _probe_live_spark_model()
        if probed:
            return probed
        logger.warning(
            "get_active_spark_model: no runtime match and live probe of %s failed",
            settings.spark_llm_url,
        )
        return None


async def _probe_live_spark_model() -> str | None:
    """Return the first model id served at ``settings.spark_llm_url`` right now.

    Endpoint-only probe (no runtime row required) — the ultimate fallback that
    keeps Spark-backed callers working across any model/recipe swap.
    """
    from app.services.endpoint_probe import probe_endpoint_url

    try:
        info = await probe_endpoint_url(settings.spark_llm_url)
        models = info.get("models") or []
        return models[0] if models else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolver: live Spark probe failed: %s", exc)
        return None


async def reprobe_spark_model() -> str | None:
    """Standalone shortcut: force re-probe of the Spark vLLM runtime."""
    return await get_active_spark_model(force_probe=True)
