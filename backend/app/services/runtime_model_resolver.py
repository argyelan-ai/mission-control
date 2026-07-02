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


def _cache_key(slug: str) -> str:
    return f"{_CACHE_PREFIX}:{slug}"


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Create a self-contained session for callers without one (background tasks).

    Public on purpose — used by ``spark_client`` and ``news_ai_worker`` for
    their 404-retry paths so they can call :func:`invalidate_and_reprobe`
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


async def get_spark_vllm_runtime(session: AsyncSession) -> Runtime | None:
    """Locate the primary Spark vLLM runtime by endpoint.

    Returns the first enabled vLLM runtime whose endpoint targets the Spark
    host on port 8000. This is the canonical "the GPU is serving X" runtime.
    The host:port to match comes from settings.spark_llm_url (env-configurable)
    instead of a hardcoded LAN IP.
    """
    from urllib.parse import urlparse

    spark_netloc = urlparse(settings.spark_llm_url).netloc
    stmt = (
        select(Runtime)
        .where(Runtime.runtime_type == "vllm_docker")
        .where(Runtime.endpoint.like(f"%{spark_netloc}%"))  # type: ignore[union-attr]
        .where(Runtime.enabled.is_(True))  # type: ignore[union-attr]
    )
    result = await session.exec(stmt)
    return result.first()


async def get_active_spark_model(*, force_probe: bool = False) -> str | None:
    """Standalone shortcut: returns active model for the Spark vLLM runtime.

    Self-contained — creates its own DB session. Use this from background
    tasks (news worker, vault backfill) that don't have a request-bound
    session in scope.
    """
    async with session_scope() as session:
        runtime = await get_spark_vllm_runtime(session)
        if runtime is None:
            logger.warning("get_active_spark_model: no Spark vLLM runtime found")
            return None
        return await get_active_model_for_runtime(
            session, runtime.slug, force_probe=force_probe
        )


async def reprobe_spark_model() -> str | None:
    """Standalone shortcut: force re-probe of the Spark vLLM runtime."""
    return await get_active_spark_model(force_probe=True)
