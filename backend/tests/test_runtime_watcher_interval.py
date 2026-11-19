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
- the death check keeps its WALL-CLOCK window of 270 s (probes 1..3 at the
  old tick): on connection refused it asks the row's anchor up to
  max(3, ceil(270 s / tick)) probes into an outage — 9 at 30 s — so an engine
  whose container outlives its closed port, or a wedged engine killed later,
  is still declared with its cause; a node-agent "running" counts only when
  it was reported after the port was already seen dark;
- the model-sync and CLI-recreate circuit breakers (3 attempts) run once per
  tick: a failed attempt waits at least 90 s for the next one, so 3 attempts
  still span 3 minutes and a fast-failing cause does not give up in one;
- the windows that were already wall-clock (crash-loop window, recovery
  cooldown and failure memory, agent-model guard, members notice) do not
  move with the tick.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.models.host import Host
from app.redis_client import RedisKeys
from app.services import agent_runtime_switch as switch_mod
from app.services import runtime_grace
from app.services import runtime_propagation as rp
from app.services import runtime_watcher as rw
from app.services import sse as sse_mod
from app.services.agent_runtime_switch import FAIL_REFUSED, FAIL_TIMEOUT, ProbedModel
from app.services.runtime_membership import AuditFinding
from app.services.runtime_watcher import UNREACHABLE_EVENT_THRESHOLD, RuntimeWatcher
from tests.test_runtime_engine_death import (
    EXITED_137,
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
@pytest.mark.parametrize("interval,lock_ttl", [
    pytest.param(5, 2, id="5s-half-the-tick"),
    pytest.param(10, 5, id="10s-half-the-tick"),
    pytest.param(30, 25, id="30s-tick-minus-5"),
    pytest.param(90, 85, id="90s-unchanged"),
    pytest.param(120, 115, id="120s-tick-minus-5"),
])
async def test_the_lock_expires_before_the_same_workers_next_tick(fake_redis, interval, lock_ttl):
    """One tick per window across workers, but never longer than the tick:
    a lock that outlives it makes the watcher skip its own next tick. The old
    10 s floor broke that for a tick of 10 s or less (the setting has no lower
    bound): the lock is now tick − 5 s, but at least half the tick."""
    watcher = RuntimeWatcher(interval=interval)
    with patch("app.services.runtime_watcher.get_redis", _redis_getter(fake_redis)):
        assert await watcher._acquire_lock() is True
        assert await watcher._acquire_lock() is False  # a second worker, same window
    ttl = await fake_redis.ttl(RedisKeys.runtime_watcher_lock())
    assert _about(ttl, lock_ttl)
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


# ── The death check keeps its 270 s window (counted in probes before) ─────


async def _outage(session, fake_redis, *, interval, box, probe, until, world,
                  rt_slug="dsv", model="DeepSeek-V4.1", host_id=None):
    """Healthy probe at t=0 (own answer), then one probe per tick up to
    ``until`` seconds; ``world(t)`` sets what port and box look like at t.
    Returns (time of the death or None, times of the plain alarms)."""
    rt = await _rt(session, slug=rt_slug, model=model)
    if host_id is not None:
        rt.host_id = host_id
        session.add(rt)
        await session.commit()
    watcher = RuntimeWatcher(interval=interval)
    death_at = None
    plain_at: list[int] = []
    with _stack(box, fake_redis, probe):
        probe.served = model                     # failure kind kept for later
        await watcher.tick(session=session)
        probe.served = None
        t = 0
        while t + interval <= until:
            t += interval
            world(t)
            await watcher.tick(session=session)
            plain = [e for e in await _events(session, "runtime.unreachable")
                     if not (e.detail or {}).get("engine_death")]
            plain_at += [t] * (len(plain) - len(plain_at))
            if death_at is None and await _deaths(session):
                death_at = t
    return death_at, plain_at


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,death_at,plain_at", [
    pytest.param(30, 120, [90], id="30s-death-follows-the-alarm"),
    pytest.param(90, 180, [], id="90s-unchanged"),
])
async def test_an_engine_that_outlives_its_closed_port_is_still_declared_dead(
    async_session, fake_redis, interval, death_at, plain_at,
):
    """Port closed at t=0, the container keeps running 100 s (slow teardown,
    a duo shutting down) and then exits 137. At the old tick the anchor was
    asked at probes 1..3 = up to 270 s: death at 180 s. Counted in probes, the
    30 s tick would stop asking at 90 s and the death would never get its
    cause (critic repro 2026-10-20). Now: declared at the first probe after
    the exit, at both ticks; at 30 s the plain alarm at the third probe comes
    first — the error with the cause follows it."""
    box = FakeBox(inspect=RUNNING)

    def world(t):
        box.inspect = RUNNING if t < 100 else EXITED_137

    got_death, got_plain = await _outage(
        async_session, fake_redis, interval=interval, box=box,
        probe=Probe(failure=FAIL_REFUSED), until=300, world=world,
    )
    assert got_death == death_at
    assert got_death - 100 <= interval        # the first probe after the exit
    assert got_plain == plain_at
    deaths = await _deaths(async_session)
    assert len(deaths) == 1
    assert deaths[0].detail["exit_code"] == 137
    assert deaths[0].detail["consecutive_failures"] == death_at // interval
    live = await _live(fake_redis, "dsv")
    assert live["status"] == "failed" and live["reason_code"] == "exit"


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,death_at,inspects", [
    pytest.param(30, 150, 2, id="30s"),             # probe 3 (timeout) + probe 5 (refused)
    pytest.param(90, 180, 1, id="90s-unchanged"),   # probe 2 (refused)
])
async def test_a_wedged_engine_killed_later_is_still_declared_dead(
    async_session, fake_redis, interval, death_at, inspects,
):
    """The port times out (engine wedged, still running) and the engine is
    killed at t=140 s; from then on the port refuses. The timeout is checked
    once, at the threshold (a stalling box looks the same); the refused port
    after the kill is checked again inside the window."""
    box = FakeBox(inspect=RUNNING)
    probe = Probe(failure=FAIL_TIMEOUT)

    def world(t):
        killed = t >= 140
        probe.failure = FAIL_REFUSED if killed else FAIL_TIMEOUT
        box.inspect = EXITED_137 if killed else RUNNING

    got_death, _ = await _outage(
        async_session, fake_redis, interval=interval, box=box, probe=probe,
        until=300, world=world,
    )
    assert got_death == death_at
    assert got_death - 140 <= interval
    # A timeout asks the box once, at the threshold — never at every probe:
    # SSH to a stalling box is what stalls the tick.
    assert box.inspects == inspects


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,checks", [
    pytest.param(30, 9, id="30s-nine-probes"),
    pytest.param(90, 3, id="90s-unchanged"),
    pytest.param(120, 3, id="120s-never-under-three"),
])
async def test_the_death_check_window_is_270_seconds_not_three_probes(
    async_session, fake_redis, interval, checks,
):
    """Port refused, own container running for the whole outage: the box is
    asked once per probe while the outage is young, then never again — at
    most ``checks`` bounded SSH inspects per outage, the last one no earlier
    than 270 s in (the window at the old tick)."""
    window = max(UNREACHABLE_EVENT_THRESHOLD, math.ceil(rw.DEATH_CHECK_WINDOW_S / interval))
    assert window == checks
    assert rw.DEATH_CHECK_WINDOW_S == 270
    assert RuntimeWatcher(interval=interval)._death_check_probes() == checks
    box = FakeBox(inspect=RUNNING)
    got_death, got_plain = await _outage(
        async_session, fake_redis, interval=interval, box=box,
        probe=Probe(failure=FAIL_REFUSED), until=interval * 20, world=lambda t: None,
    )
    assert got_death is None
    assert got_plain == [UNREACHABLE_EVENT_THRESHOLD * interval]  # once, at probe 3
    assert box.inspects == checks
    assert checks * interval >= 270


@pytest.mark.asyncio
async def test_a_start_that_never_came_up_gets_no_death_after_its_alarm(async_session, fake_redis):
    """Past the threshold the clock ("serving since") is empty because THIS
    outage's threshold emptied it. A start MC issued that never answered
    (pending start) has an empty clock for another reason: it never ran.
    It ends in its one plain alarm (ADR-096 §11.2), never in a death, even
    when its container exits while loading."""
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    box = FakeBox(inspect=EXITED_137)
    probe = Probe(served="DeepSeek-V4.1")
    watcher = RuntimeWatcher()
    with _stack(box, fake_redis, probe):
        await watcher.tick(session=async_session)                 # was up, own answer
        # "Restart now": the route empties the clock, the start is pending;
        # its 20-min grace has run out, the engine never answered.
        await runtime_grace.mark_start_pending(
            rt.slug, runtime_grace.SOURCE_MANUAL, endpoint=rt.endpoint,
        )
        rt.serving_since = None
        async_session.add(rt)
        await async_session.commit()
        probe.served, probe.failure = None, FAIL_REFUSED
        for _ in range(12):
            await watcher.tick(session=async_session)
    assert await _deaths(async_session) == []
    plain = [e for e in await _events(async_session, "runtime.unreachable")
             if not (e.detail or {}).get("engine_death")]
    assert len(plain) == 1
    assert box.inspects == 0


async def _host_reporting(session, *, state: str, at: datetime) -> Host:
    host = Host(slug=f"box-{uuid.uuid4().hex[:4]}", display_name="Box A", kind="ssh",
                ssh_host="192.0.2.10",
                agent_device_state={"anchors": [
                    {"kind": "container", "name": "dsv-tf", "state": state},
                    {"kind": "process", "name": "dsv-tf", "state": state},
                ]},
                agent_device_state_updated_at=at)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


@pytest.mark.asyncio
async def test_a_node_agent_running_from_before_the_port_closed_does_not_hide_a_death(
    async_session, fake_redis,
):
    """The node-agent reports every 15 s and a report counts as fresh for
    120 s. One taken just before the death still says "running": trusted on
    the first refused probe it skipped the SSH inspect and the death moved a
    tick later (critic repro 2026-10-20, latent until watch lists ship). A
    "running" counts only when it was reported after the port was already
    seen dark; otherwise the one SSH inspect decides."""
    host = await _host_reporting(
        async_session, state="running",
        at=datetime.now(timezone.utc) - timedelta(seconds=25),
    )
    box = FakeBox(inspect=EXITED_137)
    got_death, got_plain = await _outage(
        async_session, fake_redis, interval=30, box=box,
        probe=Probe(failure=FAIL_REFUSED), until=30, world=lambda t: None,
        host_id=host.id,
    )
    assert got_death == 30                    # the first refused probe
    assert got_plain == []
    assert box.inspects == 1


# ── What reaches the operator's alert channel (ADR-096 §11.4) ────────────


def _outlives_port(box: FakeBox, probe: Probe):
    """Port refused from t=0, the container exits 137 at t=100."""
    probe.failure = FAIL_REFUSED

    def world(t):
        box.inspect = RUNNING if t < 100 else EXITED_137

    return world


def _wedged_then_killed(box: FakeBox, probe: Probe):
    """Port times out (wedged), the engine is killed at t=140, then refused."""
    probe.failure = FAIL_TIMEOUT

    def world(t):
        killed = t >= 140
        probe.failure = FAIL_REFUSED if killed else FAIL_TIMEOUT
        box.inspect = EXITED_137 if killed else RUNNING

    return world


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,interval,plain_first", [
    pytest.param(_outlives_port, 30, True, id="outlives-port-30s"),
    pytest.param(_outlives_port, 90, False, id="outlives-port-90s"),
    pytest.param(_wedged_then_killed, 30, True, id="wedged-killed-30s"),
    pytest.param(_wedged_then_killed, 90, False, id="wedged-killed-90s"),
])
async def test_a_death_after_the_plain_alarm_still_pushes_its_cause(
    async_session, fake_redis, scenario, interval, plain_first,
):
    """Reviewer repro (PR 915, 2026-10-20). Both events are
    ``runtime.unreachable`` for the same slug, so they share one alert-dedup
    key. At the 30 s tick the plain warning (probe 3) opened that key's
    one-hour window, and the death confirmed later in the 270 s window — the
    error WITH the cause — came back "suppressed": nothing reached the
    channel at once, and the digest later carried only the line without a
    cause. An escalation is not a repeat: at both ticks the operator gets
    exactly one alert for the outage, at once, and it names the cause."""
    from app.services import discord_notify as dn

    delivered: list[tuple[str, str]] = []
    outcomes: list[tuple[str, bool, str]] = []
    real_notify = dn.notify_event

    async def fake_deliver(title, description, severity="warning"):
        delivered.append((severity, title))

    async def spy(event_type, title, severity, *, detail=None):
        result = await real_notify(event_type, title, severity, detail=detail)
        outcomes.append((severity, bool((detail or {}).get("engine_death")), result))
        return result

    box = FakeBox(inspect=RUNNING)
    probe = Probe()
    world = scenario(box, probe)
    with patch.object(dn, "_deliver", fake_deliver), \
            patch.object(dn, "get_redis", _redis_getter(fake_redis)), \
            patch.object(dn, "notify_event", spy):
        got_death, got_plain = await _outage(
            async_session, fake_redis, interval=interval, box=box, probe=probe,
            until=300, world=world,
        )
        with patch.object(dn, "DIGEST_WINDOW_SECONDS", 0):
            flushed = await dn.flush_digest()     # the digest, once its window is over

    deaths = await _deaths(async_session)
    assert got_death is not None and len(deaths) == 1
    assert bool(got_plain) is plain_first
    assert ("error", True, "sent") in outcomes
    reason = deaths[0].title
    assert delivered == [("error", reason)]
    assert "137" in reason or "Speicher" in reason
    # The plain line without a cause does not follow 30 min later either.
    assert flushed == 0



# ── The agent sync breaker keeps its pace (one attempt per tick before) ──


async def _flagged_agent(session, *, recreate=False):
    from app.models.agent import Agent
    from app.models.runtime import Runtime

    rt = Runtime(slug=f"rt-{uuid.uuid4().hex[:4]}", display_name="rt",
                 runtime_type="vllm_docker", endpoint="http://box:8000/v1",
                 model_identifier="new-model", enabled=True)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    agent = Agent(name="Worker", role="developer", agent_runtime="cli-bridge",
                  runtime_id=rt.id, harness="openclaude" if recreate else None,
                  pending_runtime_sync=not recreate, pending_recreate=recreate)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _sync_stack(fake_redis, restart):
    from contextlib import ExitStack

    getter = _redis_getter(fake_redis)
    stack = ExitStack()
    for ctx in (
        patch.object(rp, "sync_docker_agent_files", new=AsyncMock()),
        patch.object(rp, "restart_docker_agent_container", restart),
        patch.object(rp, "get_redis", getter),
        patch.object(sse_mod, "get_redis", getter),
        patch.object(switch_mod, "get_redis", getter),
    ):
        stack.enter_context(ctx)
    return stack


async def _seconds_pass(fake_redis, key: str, seconds: int) -> None:
    """Virtual time for the failure counter: its lifetime counts down."""
    ttl = await fake_redis.ttl(key)
    await fake_redis.expire(key, ttl - seconds)


@pytest.mark.asyncio
async def test_a_failed_model_sync_waits_90_seconds_for_its_next_attempt(async_session, fake_redis):
    """One attempt per tick: at 30 s a cause that fails fast (a stalled Docker
    socket) would use all 3 attempts in about a minute and give up for 15
    minutes while the agent keeps the old model. The next attempt waits until
    90 s after the failure — the pace of the old tick. The agent stays
    flagged in between; a forced sync (the operator's button) never waits."""
    assert rp.SYNC_RETRY_SPACING == 90
    agent = await _flagged_agent(async_session)
    key = RedisKeys.agent_model_sync_fails(str(agent.id))
    restart = MagicMock(return_value={"status": "error: docker socket stalled"})
    with _sync_stack(fake_redis, restart):
        await rp.sync_pending_agents(async_session)              # t=0: fails (1)
        await _seconds_pass(fake_redis, key, 30)
        await rp.sync_pending_agents(async_session)              # t=30: waits
        await _seconds_pass(fake_redis, key, 30)
        await rp.sync_pending_agents(async_session)              # t=60: waits
        assert restart.call_count == 1
        await async_session.refresh(agent)
        assert agent.pending_runtime_sync is True
        await _seconds_pass(fake_redis, key, 30)
        await rp.sync_pending_agents(async_session)              # t=90: fails (2)
        assert restart.call_count == 2
        await rp.sync_pending_agents(async_session, force=True)  # the button: at once
        assert restart.call_count == 3
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False                   # 3 failures: gave up
    assert len(await _events(async_session, "agent.model_sync_failed")) == 1


@pytest.mark.asyncio
async def test_three_failed_syncs_span_at_least_three_minutes_at_the_30s_tick(async_session, fake_redis):
    agent = await _flagged_agent(async_session)
    key = RedisKeys.agent_model_sync_fails(str(agent.id))
    restart = MagicMock(return_value={"status": "error: boom"})
    gave_up_at = None
    with _sync_stack(fake_redis, restart):
        for t in range(0, 600, settings.runtime_watcher_interval):
            await rp.sync_pending_agents(async_session)
            await async_session.refresh(agent)
            if not agent.pending_runtime_sync:
                gave_up_at = t
                break
            await _seconds_pass(fake_redis, key, settings.runtime_watcher_interval)
    assert restart.call_count == rp.MAX_SYNC_ATTEMPTS
    assert gave_up_at == 180        # 0, 90, 180 — was 0, 90, 180 at the old tick


@pytest.mark.asyncio
async def test_a_failed_cli_recreate_waits_90_seconds_too(async_session, fake_redis):
    agent = await _flagged_agent(async_session, recreate=True)
    key = RedisKeys.agent_recreate_fails(str(agent.id))
    restart = MagicMock(return_value={"status": "error: boom"})
    with _sync_stack(fake_redis, restart):
        await rp.recreate_pending_agents(async_session)          # fails (1)
        await _seconds_pass(fake_redis, key, 30)
        await rp.recreate_pending_agents(async_session)          # waits
        assert restart.call_count == 1
        await _seconds_pass(fake_redis, key, 60)
        await rp.recreate_pending_agents(async_session)          # fails (2)
        assert restart.call_count == 2
    await async_session.refresh(agent)
    assert agent.pending_recreate is True
