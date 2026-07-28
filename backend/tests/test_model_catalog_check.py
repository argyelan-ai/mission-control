"""Provider Model Catalog background check (services/model_catalog_check.py).

No network: every provider probe runs through an httpx.MockTransport, exactly
like tests/test_provider_model_catalog.py. Redis is fakeredis (conftest), the DB
is the in-memory async session — nothing here touches Docker.

The four properties that matter operationally:
  * a genuinely new model produces EXACTLY one event,
  * a second tick produces none (dedup survives restarts via Redis),
  * a model that has a runtime is not new,
  * an unreachable provider is silent, does not crash, and does not stop the
    healthy providers from being checked.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import select

from app.config import settings
from app.models.activity import ActivityEvent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import model_catalog
from app.services import model_catalog_check
from app.services import sse as sse_mod
from app.services.model_catalog_check import (
    EVENT_NEW_MODEL,
    ModelCatalogChecker,
    run_check_once,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def mock_httpx(monkeypatch, handler):
    """Route every model_catalog HTTP call through `handler`; collect requests."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(model_catalog, "_RETRY_BACKOFF", 0)
    return seen


def use_fake_redis(monkeypatch, fake_redis):
    """Both the catalog cache and the notification dedup must hit the SAME fake."""

    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(model_catalog, "get_redis", _get_redis)
    monkeypatch.setattr(model_catalog_check, "get_redis", _get_redis)
    # emit_event fans out over SSE, which opens its own Redis connection.
    monkeypatch.setattr(sse_mod, "get_redis", _get_redis)


def patch_creds(monkeypatch, creds: dict):
    async def fake_creds(*_args, **_kwargs):
        return creds

    monkeypatch.setattr(model_catalog, "resolve_provider_credentials", fake_creds)


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """This dev machine has REAL ~/.grok and ~/.mc credential files — a test
    must never read them (same guard as tests/test_provider_model_catalog.py)."""
    monkeypatch.setattr(model_catalog.settings, "home_host", "/nonexistent-test-home")


async def add_runtime(session, **kwargs) -> Runtime:
    defaults = {
        "display_name": kwargs.get("slug", "rt"),
        "runtime_type": "cloud",
        "endpoint": "https://example.invalid/v1",
    }
    defaults.update(kwargs)
    rt = Runtime(**defaults)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def add_anthropic_runtime(session, model_identifier="claude-opus-4-8") -> Runtime:
    return await add_runtime(
        session,
        slug=f"anthropic-{model_identifier}",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com/v1/messages",
        model_identifier=model_identifier,
    )


def anthropic_body(*model_ids: str) -> dict:
    ids = model_ids or ("claude-opus-5", "claude-opus-4-8")
    return {"data": [{"id": i, "display_name": i.upper()} for i in ids]}


async def new_model_events(session) -> list[ActivityEvent]:
    return list(
        (
            await session.exec(
                select(ActivityEvent).where(ActivityEvent.event_type == EVENT_NEW_MODEL)
            )
        ).all()
    )


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_setting_and_redis_keys_exist():
    # Hourly: the catalog changes a handful of times per YEAR, so latency is
    # irrelevant while every tick costs one HTTP call per provider.
    assert settings.model_catalog_check_interval == 3600
    assert RedisKeys.model_catalog_check_lock() == "mc:model-catalog:check-lock"
    assert (
        RedisKeys.model_catalog_notified("anthropic", "claude-opus-5")
        == "mc:model-catalog:notified:anthropic:claude-opus-5"
    )


# ── New model → exactly one event, once ──────────────────────────────────────


@pytest.mark.asyncio
async def test_new_model_emits_exactly_one_event(session, monkeypatch, fake_redis):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await add_anthropic_runtime(session)  # bound to claude-opus-4-8

    summary = await run_check_once(session)

    assert summary["new_models"] == 1
    assert summary["notified"] == ["anthropic:claude-opus-5"]

    events = await new_model_events(session)
    assert len(events) == 1
    assert events[0].severity == "info"  # info → no Discord push (anti-storm)
    assert events[0].detail["models"] == ["claude-opus-5"]
    assert events[0].detail["provider_key"] == "anthropic"
    assert "claude-opus-5" in events[0].title


@pytest.mark.asyncio
async def test_second_tick_does_not_re_emit(session, monkeypatch, fake_redis):
    """The dedup key lives in Redis, so this also holds across a backend
    restart — the whole point of not using in-process state."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await add_anthropic_runtime(session)

    await run_check_once(session)
    second = await run_check_once(session)

    assert second["new_models"] == 0
    assert len(await new_model_events(session)) == 1

    ttl = await fake_redis.ttl(
        RedisKeys.model_catalog_notified("anthropic", "claude-opus-5")
    )
    assert 0 < ttl <= model_catalog_check._NOTIFIED_TTL


@pytest.mark.asyncio
async def test_bound_model_produces_no_event(session, monkeypatch, fake_redis):
    """"New" == in the catalog AND no runtime row carries this
    model_identifier. Bind it and it stops being news."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await add_anthropic_runtime(session, "claude-opus-4-8")
    await add_anthropic_runtime(session, "claude-opus-5")  # operator bound it

    summary = await run_check_once(session)

    assert summary["providers_ok"] == 1  # provider WAS probed
    assert summary["new_models"] == 0
    assert await new_model_events(session) == []


# ── Failure isolation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_provider_is_silent_and_others_are_still_checked(
    session, monkeypatch, fake_redis
):
    """Anthropic is down (500 → manifest_fallback), Kimi is healthy.

    Two things must hold. No event for Anthropic — its fallback models come
    from the hand-maintained manifest, which ships claude-opus-5; announcing
    those would fire "new model!" on every provider hiccup. And Kimi must still
    be probed and still notify: a single broken provider may not abort the pass.
    """

    def route(request: httpx.Request) -> httpx.Response:
        if "anthropic" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"data": [{"id": "k3-256k"}]})

    mock_httpx(monkeypatch, route)
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    monkeypatch.setattr(model_catalog, "read_kimi_token", lambda: "kimi-token")

    await add_anthropic_runtime(session)
    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1", model_identifier="kimi-for-coding",
    )

    summary = await run_check_once(session)

    assert summary["providers_checked"] == 2
    assert summary["providers_ok"] == 1  # only kimi answered live
    assert summary["notified"] == ["kimi:k3-256k"]

    events = await new_model_events(session)
    assert len(events) == 1
    assert events[0].detail["provider_key"] == "kimi"
    # Nothing about the down provider leaked into the notifications.
    assert not await fake_redis.exists(
        RedisKeys.model_catalog_notified("anthropic", "claude-opus-5")
    )


@pytest.mark.asyncio
async def test_credential_missing_provider_is_silent(session, monkeypatch, fake_redis):
    """No credential → no live answer → nothing may be announced, ever."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {})  # vault key gone
    await add_anthropic_runtime(session)

    summary = await run_check_once(session)

    assert summary["providers_ok"] == 0
    assert await new_model_events(session) == []


@pytest.mark.asyncio
async def test_connection_error_does_not_crash_the_pass(session, monkeypatch, fake_redis):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_httpx(monkeypatch, boom)
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {})
    await add_runtime(
        session, slug="qwen-general", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
    )

    summary = await run_check_once(session)  # must not raise

    assert summary["providers_checked"] == 1
    assert summary["new_models"] == 0
    assert await new_model_events(session) == []


# ── Anti-storm ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_burst_collapses_into_one_summary_event(session, monkeypatch, fake_redis):
    """First tick on a fresh install: every unbound model looks new at once.
    That must be one line, not a wall — but nothing may be lost either."""
    ids = [f"claude-model-{n}" for n in range(6)]
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body(*ids)))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await add_anthropic_runtime(session)

    await run_check_once(session)

    events = await new_model_events(session)
    assert len(events) == 1
    assert events[0].detail["count"] == 6
    assert sorted(events[0].detail["models"]) == sorted(ids)
    # All six are marked notified — the summary replaces them, it does not
    # postpone them into the next tick.
    for model_id in ids:
        assert await fake_redis.exists(
            RedisKeys.model_catalog_notified("anthropic", model_id)
        )
    assert (await run_check_once(session))["new_models"] == 0


@pytest.mark.asyncio
async def test_redis_dedup_unavailable_stays_silent(session, monkeypatch, fake_redis):
    """Without the dedup store there is no way to tell "new" from "announced an
    hour ago" — staying silent beats repeating on every tick."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await add_anthropic_runtime(session)

    class _Broken:
        async def set(self, *_a, **_kw):
            raise ConnectionError("redis down")

    async def _broken_redis():
        return _Broken()

    monkeypatch.setattr(model_catalog_check, "get_redis", _broken_redis)

    summary = await run_check_once(session)

    assert summary["new_models"] == 0
    assert await new_model_events(session) == []


# ── Loop lifecycle ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_disabled_when_interval_zero(monkeypatch, fake_redis):
    checker = ModelCatalogChecker(interval=0)

    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(model_catalog_check, "get_redis", _get_redis)
    await checker.start()

    assert checker._task is None
    await checker.stop()  # no-op, must not raise


@pytest.mark.asyncio
async def test_lock_prevents_concurrent_tick(monkeypatch, fake_redis):
    checker = ModelCatalogChecker(interval=3600)

    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(model_catalog_check, "get_redis", _get_redis)

    assert await checker._acquire_lock() is True
    assert await checker._acquire_lock() is False
