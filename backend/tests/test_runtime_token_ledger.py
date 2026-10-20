"""Box token ledger — the boxes' own prompt/cached/generated counters, booked
as hourly deltas so Insights can show tokens that never went through MC
(requests sent straight to a box's API from another machine).

Covers: counter delta incl. engine restart (counter drops → the new value IS
the delta since the restart, never dropped, never clamped to 0), first sight
of a box books nothing (no history before the feature), idempotent booking
(same counters twice = no change), a backend restart neither double counts
nor loses (state is in the DB), the pulse-side scheduler (flush interval,
pre-restart rescue, hour boundary) and the pulse integration.
"""
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import select

from app.models.host import Host
from app.models.runtime import Runtime
from app.models.runtime_token import RuntimeTokenCounter, RuntimeTokenHour
from app.redis_client import RedisKeys
from app.services.runtime_pulse import RuntimePulse
from app.services.runtime_token_ledger import (
    TokenLedger,
    book_counters,
    counter_delta,
    parse_box_counters,
)

HOST = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
T0 = datetime(2026, 10, 11, 10, 0, 30, tzinfo=timezone.utc)

_TENSORFOLD = """
# TYPE tensorfold:prompt_tokens_total counter
tensorfold:prompt_tokens_total 16000
tensorfold:generation_tokens_total 300
tensorfold_health:completion_tokens_total 310
tensorfold_health:cached_tokens_total 14000
tensorfold_health:pool_tokens 1099776
"""

# TensorFold's native (Zig) server (the DeepSeek-V4.1-Flash two-Spark recipe):
# lines of a live scrape (20.10.2026), with one reply in flight. No
# tensorfold_health:* family, no prompt cache counter (that build keeps none).
_TENSORFOLD_NATIVE = """
# TYPE tensorfold:generation_tokens_running gauge
tensorfold:generation_tokens_running 5904
# TYPE tensorfold:prompt_tokens_total counter
tensorfold:prompt_tokens_total 7957562
# TYPE tensorfold:generation_tokens_total counter
tensorfold:generation_tokens_total 216401
"""

_VLLM = """
vllm:prompt_tokens_total{model_name="m"} 900.0
vllm:generation_tokens_total{model_name="m",finished_reason="stop"} 40.0
vllm:generation_tokens_total{model_name="m",finished_reason="length"} 2.0
"""


def _c(prompt, cached, generated):
    return {"prompt": prompt, "cached": cached, "generated": generated}


async def _hours(session):
    return (await session.exec(select(RuntimeTokenHour).order_by(RuntimeTokenHour.hour))).all()


async def _state(session):
    return (await session.exec(select(RuntimeTokenCounter))).first()


# ── pure helpers ──────────────────────────────────────────────────────────


def test_counter_delta_first_sight_books_nothing():
    assert counter_delta(None, 5000) == 0


def test_counter_delta_growth():
    assert counter_delta(1000, 1500) == 500
    assert counter_delta(1000, 1000) == 0


def test_counter_delta_reset_counts_the_new_value():
    # Engine restarted: the counter began at 0 again and is now at 300 —
    # those 300 tokens are real, they must not be dropped or clamped to 0.
    assert counter_delta(1000, 300) == 300
    assert counter_delta(1000, 0) == 0


def test_counter_delta_missing_value_books_nothing():
    assert counter_delta(1000, None) == 0


def test_parse_tensorfold_prefers_streaming_completion_counter():
    assert parse_box_counters(_TENSORFOLD, "tensorfold") == _c(16000, 14000, 310)


def test_parse_tensorfold_native_books_finished_tokens_only():
    """The in-flight gauge is left out on purpose: the ledger reads every drop
    as an engine restart and books the new value in full, so a gauge that
    falls when a reply ends would book the whole counter again. Finished
    tokens are booked when the reply ends — in that hour."""
    assert parse_box_counters(_TENSORFOLD_NATIVE, "tensorfold") == _c(7957562, None, 216401)


def test_parse_tensorfold_native_prompt_cache_counter_is_the_cached_column():
    # Native builds that keep prompt states export it (engine source,
    # server/metrics.zig: only "if (keeps)").
    text = _TENSORFOLD_NATIVE + "tensorfold:prompt_tokens_cached_total 4096\n"
    assert parse_box_counters(text, "tensorfold") == _c(7957562, 4096, 216401)


def test_parse_vllm_sums_series_and_marks_missing_cache_as_none():
    assert parse_box_counters(_VLLM, "vllm") == _c(900, None, 42)


def test_parse_unknown_engine_is_all_none():
    assert parse_box_counters(_VLLM, "nope") == _c(None, None, None)


# ── booking (DB) ──────────────────────────────────────────────────────────


async def test_first_sight_stores_baseline_without_history(session):
    deltas = await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(16000, 14000, 310), at=T0)
    assert deltas == _c(0, 0, 0)
    assert await _hours(session) == []
    state = await _state(session)
    assert (state.last_prompt, state.last_cached, state.last_generated) == (16000, 14000, 310)
    assert state.first_seen_at.replace(tzinfo=timezone.utc) == T0


async def test_growth_lands_in_the_utc_hour_and_is_idempotent(session):
    await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(1000, 800, 50), at=T0)
    at = T0 + timedelta(minutes=5)
    assert await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(1600, 1300, 90), at=at) == _c(600, 500, 40)
    # Same counters again (a second worker, a retried flush): nothing changes.
    assert await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(1600, 1300, 90), at=at) == _c(0, 0, 0)
    rows = await _hours(session)
    assert len(rows) == 1
    assert rows[0].hour.replace(tzinfo=timezone.utc) == datetime(2026, 10, 11, 10, tzinfo=timezone.utc)
    assert (rows[0].prompt_tokens, rows[0].cached_tokens, rows[0].generated_tokens) == (600, 500, 40)


async def test_engine_restart_counts_the_new_value_as_delta(session):
    await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(10_000, 9000, 700), at=T0)
    await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(300, 200, 20), at=T0 + timedelta(minutes=5))
    rows = await _hours(session)
    assert (rows[0].prompt_tokens, rows[0].cached_tokens, rows[0].generated_tokens) == (300, 200, 20)
    # And the next growth is measured from the post-restart value.
    await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(500, 300, 30), at=T0 + timedelta(minutes=10))
    rows = await _hours(session)
    assert (rows[0].prompt_tokens, rows[0].cached_tokens, rows[0].generated_tokens) == (500, 300, 30)


async def test_engines_on_one_host_keep_their_own_counters(session):
    await book_counters(session, host_id=HOST, engine="vllm", counters=_c(5000, None, 100), at=T0)
    await book_counters(session, host_id=HOST, engine="tensorfold", counters=_c(100, 50, 10), at=T0)
    await book_counters(session, host_id=HOST, engine="vllm", counters=_c(5200, None, 110), at=T0 + timedelta(minutes=5))
    rows = await _hours(session)
    assert [(r.engine, r.prompt_tokens, r.cached_tokens, r.generated_tokens) for r in rows] == [("vllm", 200, 0, 10)]


# ── scheduler on the pulse side ───────────────────────────────────────────


def _ledger(session, interval=300):
    @asynccontextmanager
    async def factory():
        yield session
    return TokenLedger(flush_interval=interval, session_factory=factory)


async def test_ledger_books_at_first_sight_then_only_after_the_interval(session):
    ledger = _ledger(session)
    await ledger.observe(HOST, "tensorfold", _c(1000, 800, 50), T0)  # baseline
    await ledger.observe(HOST, "tensorfold", _c(1100, 850, 60), T0 + timedelta(seconds=5))
    assert await _hours(session) == []  # not due yet
    await ledger.observe(HOST, "tensorfold", _c(1300, 900, 80), T0 + timedelta(seconds=301))
    rows = await _hours(session)
    assert (rows[0].prompt_tokens, rows[0].cached_tokens, rows[0].generated_tokens) == (300, 100, 30)


async def test_ledger_rescues_the_tokens_seen_just_before_a_restart(session):
    ledger = _ledger(session)
    await ledger.observe(HOST, "tensorfold", _c(1000, 800, 50), T0)  # baseline
    # Grows, not yet booked (inside the interval) ...
    await ledger.observe(HOST, "tensorfold", _c(4000, 3000, 250), T0 + timedelta(seconds=60))
    # ... then the engine restarts. The 3000 before the restart AND the 100
    # after it are both real.
    await ledger.observe(HOST, "tensorfold", _c(100, 40, 5), T0 + timedelta(seconds=120))
    rows = await _hours(session)
    assert (rows[0].prompt_tokens, rows[0].cached_tokens, rows[0].generated_tokens) == (3100, 2240, 205)


async def test_ledger_books_the_old_hour_before_crossing_into_the_new_one(session):
    ledger = _ledger(session)
    start = datetime(2026, 10, 11, 10, 58, tzinfo=timezone.utc)
    await ledger.observe(HOST, "tensorfold", _c(1000, 0, 0), start)
    await ledger.observe(HOST, "tensorfold", _c(1500, 0, 0), start + timedelta(seconds=115))  # 10:59:55
    await ledger.observe(HOST, "tensorfold", _c(1600, 0, 0), start + timedelta(seconds=125))  # 11:00:05
    rows = await _hours(session)
    assert [(r.hour.replace(tzinfo=timezone.utc).hour, r.prompt_tokens) for r in rows] == [(10, 500), (11, 100)]


async def test_backend_restart_neither_double_counts_nor_loses(session):
    first = _ledger(session)
    await first.observe(HOST, "tensorfold", _c(1000, 0, 10), T0)
    await first.observe(HOST, "tensorfold", _c(2000, 0, 20), T0 + timedelta(seconds=400))
    # New process: empty memory, books at first sight against the DB state.
    second = _ledger(session)
    await second.observe(HOST, "tensorfold", _c(2000, 0, 20), T0 + timedelta(seconds=405))
    await second.observe(HOST, "tensorfold", _c(2500, 0, 25), T0 + timedelta(seconds=900))
    rows = await _hours(session)
    assert (rows[0].prompt_tokens, rows[0].generated_tokens) == (1500, 15)


async def test_ledger_disabled_books_nothing(session):
    ledger = _ledger(session, interval=0)
    await ledger.observe(HOST, "tensorfold", _c(1000, 0, 0), T0)
    assert await _state(session) is None


# ── pulse integration ─────────────────────────────────────────────────────


async def test_pulse_tick_feeds_the_ledger_and_keeps_the_tps_ring(async_session, fake_redis):
    host = Host(id=uuid.uuid4(), slug="gpu-box", display_name="gpu-box", kind="ssh", ssh_host="192.0.2.10")
    async_session.add(host)
    await async_session.commit()
    rt = Runtime(slug="slot", display_name="slot", runtime_type="vllm_docker", endpoint="http://192.0.2.10:8000/v1",
                 enabled=True, is_slot=True, host_id=host.id)
    async_session.add(rt)
    await async_session.commit()
    await fake_redis.set(RedisKeys.runtime_live("slot"), '{"reachable": true}')

    resp = MagicMock(status_code=200, text=_TENSORFOLD)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get = AsyncMock(return_value=resp)

    ledger = _ledger(async_session)
    poller = RuntimePulse(interval=5)

    async def _get():
        return fake_redis

    with patch("app.services.runtime_pulse.get_redis", _get), \
         patch("app.services.runtime_pulse.token_ledger", ledger), \
         patch("httpx.AsyncClient", return_value=client):
        await poller.tick(session=async_session)

    state = (await async_session.exec(select(RuntimeTokenCounter))).first()
    assert state is not None and state.host_id == host.id and state.engine == "tensorfold"
    assert (state.last_prompt, state.last_cached, state.last_generated) == (16000, 14000, 310)
    assert await fake_redis.llen(RedisKeys.host_pulse(str(host.id))) == 1


async def test_pulse_tick_feeds_the_ledger_for_a_native_tensorfold_box(async_session, fake_redis):
    """Before the fix the pulse found no speed counter on the native build and
    returned before the ledger — the box's tokens were never booked."""
    host = Host(id=uuid.uuid4(), slug="gpu-box", display_name="gpu-box", kind="ssh", ssh_host="192.0.2.10")
    async_session.add(host)
    await async_session.commit()
    async_session.add(Runtime(slug="slot", display_name="slot", runtime_type="openai_compatible",
                              endpoint="http://192.0.2.10:8000/v1", enabled=True, is_slot=True, host_id=host.id))
    await async_session.commit()
    await fake_redis.set(RedisKeys.runtime_live("slot"), '{"reachable": true}')
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get = AsyncMock(return_value=MagicMock(status_code=200, text=_TENSORFOLD_NATIVE))
    ledger = _ledger(async_session)

    async def _get():
        return fake_redis

    with patch("app.services.runtime_pulse.get_redis", _get), \
         patch("app.services.runtime_pulse.token_ledger", ledger), \
         patch("httpx.AsyncClient", return_value=client):
        await RuntimePulse(interval=5).tick(session=async_session)

    state = (await async_session.exec(select(RuntimeTokenCounter))).first()
    assert state is not None and state.host_id == host.id and state.engine == "tensorfold"
    assert (state.last_prompt, state.last_cached, state.last_generated) == (7957562, None, 216401)


async def test_pulse_survives_a_broken_ledger(async_session, fake_redis):
    host = Host(id=uuid.uuid4(), slug="gpu-box", display_name="gpu-box", kind="ssh", ssh_host="192.0.2.10")
    async_session.add(host)
    await async_session.commit()
    async_session.add(Runtime(slug="slot", display_name="slot", runtime_type="vllm_docker",
                              endpoint="http://192.0.2.10:8000/v1", enabled=True, is_slot=True, host_id=host.id))
    await async_session.commit()
    await fake_redis.set(RedisKeys.runtime_live("slot"), '{"reachable": true}')
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get = AsyncMock(return_value=MagicMock(status_code=200, text=_TENSORFOLD))
    broken = MagicMock()
    broken.observe = AsyncMock(side_effect=RuntimeError("db down"))

    async def _get():
        return fake_redis

    with patch("app.services.runtime_pulse.get_redis", _get), \
         patch("app.services.runtime_pulse.token_ledger", broken), \
         patch("httpx.AsyncClient", return_value=client):
        await RuntimePulse(interval=5).tick(session=async_session)

    meta = await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id)))
    assert '"available": true' in meta
