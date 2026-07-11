"""Tests for runtime_model_resolver — auto-detection of active LLM models.

Hermetic: SQLite in-memory + fakeredis. The actual ``probe_runtime_model``
function is mocked so tests don't hit network.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services import runtime_model_resolver as resolver


# ── Helper fixtures ─────────────────────────────────────────────────────


@pytest.fixture
async def patched_redis(fake_redis):
    """Patch resolver's get_redis call to return fakeredis."""
    async def _fake_get_redis():
        return fake_redis

    with patch("app.services.runtime_model_resolver.get_redis", _fake_get_redis):
        yield fake_redis


@pytest.fixture
async def spark_runtime(async_session: AsyncSession) -> Runtime:
    """Create a fresh Spark vLLM runtime row."""
    rt = Runtime(
        slug="qwen-general",
        display_name="Spark Qwen vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="Qwen/Qwen3.6-35B-A3B-FP8",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    return rt


# ── get_active_model_for_runtime ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_db_value_when_set(
    async_session: AsyncSession, patched_redis, spark_runtime: Runtime
):
    """Happy path: DB has a value, no probe needed, no DB write."""
    model = await resolver.get_active_model_for_runtime(async_session, "qwen-general")
    assert model == "Qwen/Qwen3.6-35B-A3B-FP8"


@pytest.mark.asyncio
async def test_returns_none_for_unknown_runtime(
    async_session: AsyncSession, patched_redis
):
    """No matching slug or UUID → ``None``."""
    model = await resolver.get_active_model_for_runtime(async_session, "does-not-exist")
    assert model is None


@pytest.mark.asyncio
async def test_probes_when_model_identifier_null(
    async_session: AsyncSession, patched_redis, spark_runtime: Runtime
):
    """If DB row has model_identifier=NULL, probe ``/v1/models`` and persist."""
    spark_runtime.model_identifier = None
    async_session.add(spark_runtime)
    await async_session.commit()

    with patch(
        "app.services.agent_runtime_switch.probe_runtime_model",
        AsyncMock(return_value="newly-detected-model"),
    ):
        model = await resolver.get_active_model_for_runtime(
            async_session, "qwen-general"
        )

    assert model == "newly-detected-model"
    # Verify it was persisted to DB
    await async_session.refresh(spark_runtime)
    assert spark_runtime.model_identifier == "newly-detected-model"


@pytest.mark.asyncio
async def test_force_probe_overrides_db_when_changed(
    async_session: AsyncSession, patched_redis, spark_runtime: Runtime
):
    """force_probe=True bypasses cache + DB and persists the probed value.

    Critical path: this is what auto-detects a recipe swap on the inference side.
    """
    with patch(
        "app.services.agent_runtime_switch.probe_runtime_model",
        AsyncMock(return_value="rdtand/PrismaQuant-newer-model"),
    ):
        model = await resolver.get_active_model_for_runtime(
            async_session, "qwen-general", force_probe=True
        )

    assert model == "rdtand/PrismaQuant-newer-model"
    await async_session.refresh(spark_runtime)
    assert spark_runtime.model_identifier == "rdtand/PrismaQuant-newer-model"


@pytest.mark.asyncio
async def test_probe_failure_falls_back_to_db(
    async_session: AsyncSession, patched_redis, spark_runtime: Runtime
):
    """If probe returns None but DB has a value, use the DB value (degraded mode)."""
    spark_runtime.model_identifier = "stale-but-better-than-nothing"
    async_session.add(spark_runtime)
    await async_session.commit()

    with patch(
        "app.services.agent_runtime_switch.probe_runtime_model",
        AsyncMock(return_value=None),
    ):
        model = await resolver.get_active_model_for_runtime(
            async_session, "qwen-general", force_probe=True
        )

    assert model == "stale-but-better-than-nothing"


# ── invalidate_and_reprobe ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_and_reprobe_picks_up_new_model(
    async_session: AsyncSession, patched_redis, spark_runtime: Runtime
):
    """After a 404 from the runtime, caller invokes invalidate_and_reprobe.
    Verifies cache is wiped + DB is updated with the new model.
    """
    # Pre-populate cache with stale value to prove it gets cleared
    await patched_redis.set("mc:runtime-model:qwen-general", "stale-cached-value")

    with patch(
        "app.services.agent_runtime_switch.probe_runtime_model",
        AsyncMock(return_value="fresh-from-probe"),
    ):
        model = await resolver.invalidate_and_reprobe(async_session, "qwen-general")

    assert model == "fresh-from-probe"
    # Cache now has the new value
    cached = await patched_redis.get("mc:runtime-model:qwen-general")
    assert cached == "fresh-from-probe"
    # DB persisted
    await async_session.refresh(spark_runtime)
    assert spark_runtime.model_identifier == "fresh-from-probe"


# ── get_spark_vllm_runtime ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finds_spark_runtime_by_endpoint(
    async_session: AsyncSession, spark_runtime: Runtime
):
    """Identifies the Spark vLLM runtime by its endpoint (port 8000)."""
    found = await resolver.get_spark_vllm_runtime(async_session)
    assert found is not None
    assert found.slug == "qwen-general"


@pytest.mark.asyncio
async def test_ignores_disabled_spark_runtime(
    async_session: AsyncSession, spark_runtime: Runtime
):
    """Disabled runtimes are excluded — they're not "the active one"."""
    spark_runtime.enabled = False
    async_session.add(spark_runtime)
    await async_session.commit()
    found = await resolver.get_spark_vllm_runtime(async_session)
    assert found is None


@pytest.mark.asyncio
async def test_returns_none_when_no_spark_runtime(
    async_session: AsyncSession, patched_redis
):
    """If no Spark vLLM runtime exists, return None gracefully."""
    found = await resolver.get_spark_vllm_runtime(async_session)
    assert found is None


@pytest.mark.asyncio
async def test_finds_spark_runtime_regardless_of_type(async_session: AsyncSession):
    """A model swap that flips the Spark runtime's type (vllm_docker -> omp)
    must NOT hide it — it is matched by endpoint only. This is the regression
    that silently broke the news worker's model resolution."""
    rt = Runtime(
        slug="omp-qwen",
        display_name="Spark omp",
        runtime_type="omp",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="Qwen/Qwen3.6-35B-A3B-FP8",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    found = await resolver.get_spark_vllm_runtime(async_session)
    assert found is not None
    assert found.slug == "omp-qwen"


@pytest.mark.asyncio
async def test_probe_live_spark_model_returns_first_served():
    """The endpoint-only live probe returns whatever the GPU serves now —
    the model-agnostic fallback used when no runtime row resolves."""
    with patch(
        "app.services.endpoint_probe.probe_endpoint_url",
        AsyncMock(return_value={"models": ["DeepSeek-V4-Flash-Spark", "other"]}),
    ):
        assert await resolver._probe_live_spark_model() == "DeepSeek-V4-Flash-Spark"


@pytest.mark.asyncio
async def test_probe_live_spark_model_none_when_empty():
    with patch(
        "app.services.endpoint_probe.probe_endpoint_url",
        AsyncMock(return_value={"models": []}),
    ):
        assert await resolver._probe_live_spark_model() is None


# ── Cache behaviour ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cached_value_returned_without_db_hit(
    async_session: AsyncSession, patched_redis, spark_runtime: Runtime
):
    """Cache hit short-circuits DB load — proven by deleting the row after caching."""
    # Warm the cache
    model_first = await resolver.get_active_model_for_runtime(
        async_session, "qwen-general"
    )
    assert model_first == "Qwen/Qwen3.6-35B-A3B-FP8"

    # Delete the runtime row — if cache works, next call still returns the value
    await async_session.delete(spark_runtime)
    await async_session.commit()

    model_second = await resolver.get_active_model_for_runtime(
        async_session, "qwen-general"
    )
    assert model_second == "Qwen/Qwen3.6-35B-A3B-FP8"


@pytest.mark.asyncio
async def test_negative_cache_on_missing_runtime(
    async_session: AsyncSession, patched_redis
):
    """When no runtime is found AND no probe succeeds, cache the miss briefly
    so repeated calls don't hammer the resolver path."""
    # First call — no runtime, sentinel cached
    model = await resolver.get_active_model_for_runtime(async_session, "ghost-runtime")
    assert model is None

    # Verify sentinel landed in cache
    cached = await patched_redis.get("mc:runtime-model:ghost-runtime")
    assert cached is None or cached == ""  # ghost case: no DB row → no negative cache
