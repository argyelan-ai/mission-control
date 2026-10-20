"""Runtime watcher tick 30 s instead of 90 s (operator decision 2026-10-20).

The tick is one number, but several lifetimes were derived from it. Each one
is pinned here — at the new default, at the old 90 s (nothing may change
there) and at a slower 120 s (the tick-count meaning must still hold):

- the tick itself and the loop cadence: 30 s;
- the multi-worker lock: follows the tick and must stay BELOW it, or the
  worker's own next tick would find its own lock and skip;
- what one tick writes for readers between ticks (live snapshot, both drift
  candidates, the members mark): 3 ticks, but never under 270 s — a tick
  that does real work (an auto-recovery start waits for free memory up to
  180 s) must not make the cards go blank or a drift unconfirmable;
- the outage counter: 10 ticks, but never under 900 s;
- the alarm threshold stays 3 PROBES (ADR-054 point 5, ADR-096 §11.2, the
  stage card's "failed"): it now fires 60–90 s after a death, not 180–270 s;
- the windows that were already wall-clock (crash-loop window, recovery
  cooldown and failure memory, agent-model guard, members notice) do not
  move with the tick.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.redis_client import RedisKeys
from app.services import runtime_watcher as rw
from app.services.agent_runtime_switch import FAIL_REFUSED, FAIL_TIMEOUT, ProbedModel
from app.services.runtime_membership import AuditFinding
from app.services.runtime_watcher import UNREACHABLE_EVENT_THRESHOLD, RuntimeWatcher
from tests.test_runtime_engine_death import (
    RUNNING,
    FakeBox,
    Probe,
    _deaths,
    _events,
    _live,
    _rt,
    _stack,
)

# (tick, state TTL, outage-counter TTL)
SCHEDULES = [
    pytest.param(30, 270, 900, id="new-default-30s"),
    pytest.param(90, 270, 900, id="old-default-90s-unchanged"),
    pytest.param(120, 360, 1200, id="slower-120s-scales"),
]


def _about(ttl: int, expected: int) -> bool:
    """A Redis TTL read back a moment after it was set (it counts down)."""
    return expected - 2 <= ttl <= expected


def _redis_getter(fake_redis):
    async def _get():
        return fake_redis

    return _get


# ── The tick ─────────────────────────────────────────────────────────────


def test_default_tick_is_30_seconds():
    assert settings.runtime_watcher_interval == 30
    assert RuntimeWatcher()._interval == 30


@pytest.mark.asyncio
async def test_the_loop_waits_one_tick_between_two_ticks():
    """Startup grace once, then exactly the configured tick after each pass."""
    watcher = RuntimeWatcher()
    sleeps: list[float] = []
    ticks = 0

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            watcher._running = False

    async def fake_tick(session=None):
        nonlocal ticks
        ticks += 1

    watcher._running = True
    with patch.object(rw, "asyncio", SimpleNamespace(sleep=fake_sleep)), \
            patch.object(watcher, "_acquire_lock", AsyncMock(return_value=True)), \
            patch.object(watcher, "tick", fake_tick):
        await watcher._run_loop()

    assert sleeps == [rw._STARTUP_GRACE, 30, 30]
    assert ticks == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [30, 90, 120])
async def test_the_lock_expires_before_the_same_workers_next_tick(fake_redis, interval):
    """One tick per window across workers, but never longer than the tick:
    a lock that outlives it makes the watcher skip its own next tick."""
    watcher = RuntimeWatcher(interval=interval)
    with patch("app.services.runtime_watcher.get_redis", _redis_getter(fake_redis)):
        assert await watcher._acquire_lock() is True
        assert await watcher._acquire_lock() is False  # a second worker, same window
    ttl = await fake_redis.ttl(RedisKeys.runtime_watcher_lock())
    assert _about(ttl, interval - 5)
    assert 0 < ttl < interval


# ── What one tick writes for the readers between ticks ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,state_ttl,_fails_ttl", SCHEDULES)
async def test_live_snapshot_lifetime(async_session, fake_redis, interval, state_ttl, _fails_ttl):
    """Read by the runtimes page, the readiness gate, the pulse and the
    auto-recovery sibling check. At 3 × 30 s = 90 s it would expire during
    one auto-recovery tick (memory-prep wait up to 180 s) and the cards of
    every other runtime would go blank."""
    rt = await _rt(async_session, slug="snap", model="m-1")
    probe = Probe(served="m-1")
    with _stack(FakeBox(), fake_redis, probe):
        await RuntimeWatcher(interval=interval).tick(session=async_session)
    assert (await _live(fake_redis, rt.slug))["reachable"] is True
    assert _about(await fake_redis.ttl(RedisKeys.runtime_live(rt.slug)), state_ttl)
    # Gap between two writes = the tick's own run time + the pause after it.
    assert state_ttl >= settings.memory_prep_wait_timeout_seconds + interval


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,state_ttl,_fails_ttl", SCHEDULES)
async def test_drift_candidate_lifetime(async_session, fake_redis, interval, state_ttl, _fails_ttl):
    """The first sighting of a new model must still be there at the next
    probe, even when a long tick sits between the two."""
    rt = await _rt(async_session, slug="slot-drift", model="old", is_slot=True,
                   runtime_type="openai_compatible")
    probe = Probe(served="new")
    with _stack(FakeBox(), fake_redis, probe):
        await RuntimeWatcher(interval=interval).tick(session=async_session)
    key = RedisKeys.runtime_drift_candidate(rt.slug)
    assert await fake_redis.get(key) == "new"
    assert _about(await fake_redis.ttl(key), state_ttl)
    await async_session.refresh(rt)
    assert rt.model_identifier == "old"  # one probe is still only a candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,state_ttl,_fails_ttl", SCHEDULES)
async def test_context_drift_candidate_lifetime(async_session, fake_redis, interval, state_ttl, _fails_ttl):
    rt = await _rt(async_session, slug="slot-ctx", model="m", is_slot=True,
                   runtime_type="openai_compatible")

    async def probe(runtime):
        return ProbedModel("m", 131072)

    with _stack(FakeBox(), fake_redis, Probe()), \
            patch("app.services.runtime_watcher.probe_runtime_model_info", new=probe):
        await RuntimeWatcher(interval=interval).tick(session=async_session)
    key = RedisKeys.runtime_context_drift_candidate(rt.slug)
    assert await fake_redis.get(key) == "131072"
    assert _about(await fake_redis.ttl(key), state_ttl)


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,state_ttl,_fails_ttl", SCHEDULES)
async def test_members_mark_lifetime(async_session, fake_redis, interval, state_ttl, _fails_ttl):
    """Refreshed every tick while the members are wrong; gone a few ticks
    after the fix — but it must not blink off during one long tick."""
    finding = AuditFinding(runtime_id=uuid.uuid4(), slug="duo",
                           code="worker_missing", sentence="duo: worker missing")
    with patch("app.services.runtime_watcher.get_redis", _redis_getter(fake_redis)), \
            patch("app.services.runtime_membership.membership_audit",
                  new=AsyncMock(return_value=[finding])), \
            patch("app.services.runtime_watcher.emit_event", new=AsyncMock()):
        await RuntimeWatcher(interval=interval)._mark_membership(async_session)
    key = RedisKeys.runtime_members_issue("duo")
    assert await fake_redis.get(key) is not None
    assert _about(await fake_redis.ttl(key), state_ttl)


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,_state_ttl,fails_ttl", SCHEDULES)
async def test_outage_counter_lifetime(async_session, fake_redis, interval, _state_ttl, fails_ttl):
    """The counter must outlast the longest gap between two probes of one
    outage (a long tick, a switch window that reads it without bumping),
    or the card falls back from "failed" (3) to "unreachable (1)"."""
    rt = await _rt(async_session, slug="fails", model="m-1")
    probe = Probe(failure=FAIL_TIMEOUT)
    with _stack(FakeBox(), fake_redis, probe):
        await RuntimeWatcher(interval=interval).tick(session=async_session)
    key = RuntimeWatcher._fail_key(rt.slug)
    assert await fake_redis.get(key) == "1"
    assert _about(await fake_redis.ttl(key), fails_ttl)


# ── The alarm threshold stays in probes ─────────────────────────────────


@pytest.mark.asyncio
async def test_plain_alarm_still_at_the_third_probe_now_90_seconds_after_the_death(
    async_session, fake_redis,
):
    """Deliberately NOT converted to seconds: ADR-054 point 5 and ADR-096
    §11.2 define it as 3 consecutive probes, and the stage card turns
    "failed" at the same count (Stage.tsx, consecutive_failures >= 3).
    Worst case (death right after a healthy probe): 3 ticks = 90 s, was 270 s."""
    assert UNREACHABLE_EVENT_THRESHOLD == 3
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(served="DeepSeek-V4.1")
    watcher = RuntimeWatcher()
    elapsed = 0
    alarms_after: list[int] = []
    with _stack(FakeBox(inspect=RUNNING), fake_redis, probe):
        await watcher.tick(session=async_session)  # healthy, own answer
        probe.served, probe.failure = None, FAIL_TIMEOUT
        for _ in range(4):
            elapsed += watcher._interval
            await watcher.tick(session=async_session)
            plain = [e for e in await _events(async_session, "runtime.unreachable")
                     if not (e.detail or {}).get("engine_death")]
            alarms_after.append(len(plain))
        live = await _live(fake_redis, rt.slug)
    assert alarms_after == [0, 0, 1, 1]  # once, at the third probe
    assert UNREACHABLE_EVENT_THRESHOLD * watcher._interval == 90
    assert live["consecutive_failures"] == 4
    assert await _deaths(async_session) == []  # anchor running: no death


@pytest.mark.asyncio
async def test_a_crash_is_declared_one_tick_after_it_happened(async_session, fake_redis):
    """Goal of the decision: port closed + engine gone → declared at the next
    probe, i.e. at most one tick (30 s) plus the bounded check."""
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(served="DeepSeek-V4.1")
    watcher = RuntimeWatcher()
    with _stack(FakeBox(), fake_redis, probe):  # anchor exited 137
        await watcher.tick(session=async_session)  # t=0 healthy; dies right after
        probe.served, probe.failure = None, FAIL_REFUSED
        await watcher.tick(session=async_session)  # t=30
        live = await _live(fake_redis, rt.slug)
    deaths = await _deaths(async_session)
    assert len(deaths) == 1
    assert deaths[0].detail["consecutive_failures"] == 1
    assert live["status"] == "failed"
    assert watcher._interval == 30
    assert rw._ANCHOR_CHECK_TIMEOUT + rw._EXPLAIN_TIMEOUT < watcher._interval


# ── Windows that were already wall-clock do not move with the tick ───────


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [30, 90])
async def test_wall_clock_windows_do_not_follow_the_tick(fake_redis, interval):
    watcher = RuntimeWatcher(interval=interval)
    # Crash-loop: restarts are counted inside 10 minutes, whatever the tick.
    await watcher._restart_baseline(fake_redis, "x", 4)
    assert _about(await fake_redis.ttl(RedisKeys.runtime_restart_baseline("x")), 600)
    # Auto-recovery: one attempt per 15 minutes, attempts forgotten after 6 h.
    assert await watcher._claim_recovery_cooldown(fake_redis, "x") is True
    assert _about(await fake_redis.ttl(RedisKeys.runtime_recovery_cooldown("x")), 900)
    await watcher._bump_recovery_failures(fake_redis, "x")
    assert _about(await fake_redis.ttl(RedisKeys.runtime_recovery_failures("x")), 6 * 3600)
    # Constants that were never derived from the tick.
    assert rw.CRASH_LOOP_WINDOW_SECONDS == 600
    assert rw.AGENT_MODEL_GUARD_TTL == 600
    assert rw.MEMBERS_NOTICE_TTL == 24 * 3600
    assert rw._STARTUP_GRACE == 20
