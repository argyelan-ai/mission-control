"""Bench W1 plan: the liveness answers per engine are cached for a short time.

Live bug 25.10.: every GET /bench/plan took 4 s. A sleeping box never answers
/models; ``asyncio.wait_for`` cancelled the probe at 4 s, before the engine's
own /models cache was written, so every preset or prompt tap in the test
sheet waited the full timeout again. A "down" answer is now cached like an
"up" one and expires the same way (a woken box shows up again)."""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.services.heads import engine  # noqa: E402
from app.verticals.bench_studio import w1_plan  # noqa: E402

from tests.test_bench_w1_fleet import DS, SPARK_EP, WOL_EP, seed_fleet  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(w1_plan, "time", c)
    return c


@pytest.fixture
def boxes(monkeypatch):
    """The Spark serves DS; the WoL box hangs like a sleeping box does."""
    state = {"asked": [], "load_asked": [], "awake": {SPARK_EP}}

    async def served_models(ep, **_):
        state["asked"].append(ep)
        if ep in state["awake"]:
            return frozenset({DS})
        await asyncio.sleep(60)  # never answers

    async def running_requests(ep):
        state["load_asked"].append(ep)
        return 0

    monkeypatch.setattr(engine, "served_models", served_models)
    monkeypatch.setattr(engine, "running_requests", running_requests)
    monkeypatch.setattr(w1_plan, "LIVE_TIMEOUT_S", 0.05)
    return state


@pytest.mark.asyncio
async def test_a_sleeping_box_costs_its_timeout_once_not_on_every_call(boxes, clock):
    assert await w1_plan._served(WOL_EP) is None
    t0 = time.perf_counter()
    assert await w1_plan._served(WOL_EP) is None
    assert time.perf_counter() - t0 < 0.04  # no second wait
    assert boxes["asked"].count(WOL_EP) == 1


@pytest.mark.asyncio
async def test_an_answering_box_is_not_asked_again_within_the_ttl(boxes, clock):
    assert await w1_plan._served(SPARK_EP) == frozenset({DS})
    clock.now += w1_plan.SERVED_TTL_S - 1
    assert await w1_plan._served(SPARK_EP) == frozenset({DS})
    assert boxes["asked"] == [SPARK_EP]


@pytest.mark.asyncio
async def test_a_cached_down_expires_and_a_woken_box_shows_up(boxes, clock):
    assert await w1_plan._served(WOL_EP) is None
    boxes["awake"].add(WOL_EP)
    clock.now += w1_plan.SERVED_TTL_S - 1
    assert await w1_plan._served(WOL_EP) is None  # still the cached answer
    clock.now += 2
    assert await w1_plan._served(WOL_EP) == frozenset({DS})
    assert boxes["asked"].count(WOL_EP) == 2


@pytest.mark.asyncio
async def test_the_load_is_cached_only_briefly(boxes, clock):
    assert await w1_plan._in_use(SPARK_EP) is False
    assert await w1_plan._in_use(SPARK_EP) is False
    assert boxes["load_asked"] == [SPARK_EP]
    clock.now += w1_plan.IN_USE_TTL_S + 0.1
    assert await w1_plan._in_use(SPARK_EP) is False
    assert boxes["load_asked"] == [SPARK_EP, SPARK_EP]


@pytest.mark.asyncio
async def test_the_second_plan_read_asks_no_engine_and_still_says_asleep(session, boxes, clock):
    await seed_fleet(session)
    first = await w1_plan.load_views(session)
    asked = list(boxes["asked"])
    assert WOL_EP in asked and SPARK_EP in asked
    t0 = time.perf_counter()
    second = await w1_plan.load_views(session)
    assert time.perf_counter() - t0 < 0.04
    assert boxes["asked"] == asked  # nothing asked again
    by_ep = lambda views: {v.runtime.endpoint: v.reachable for v in views}  # noqa: E731
    assert by_ep(second) == by_ep(first)
    assert by_ep(second)[WOL_EP] is False and by_ep(second)[SPARK_EP] is True


# ── The picker list: answer from the last word, refresh behind it ────────
#
# User test 21.11.2026: GET /bench/contestants took 24 s cold and the picker
# waited for every box again whenever its 20 s answer had run out (measured
# in a fresh process the same day: load_views 2.2 s, everything else
# < 0.05 s — the sleeping box's timeout). The list only SHOWS boxes, so it
# may answer from the expired word and refresh it in the background; the
# runner and the plan keep asking fresh (they act on the answer).


@pytest.mark.asyncio
async def test_list_answers_from_an_expired_word_and_refreshes_behind(boxes, clock):
    assert await w1_plan._served(WOL_EP) is None  # box asleep, cached
    boxes["awake"].add(WOL_EP)
    clock.now += w1_plan.SERVED_TTL_S + 1  # the word expired

    t0 = time.perf_counter()
    assert await w1_plan._served(WOL_EP, stale_ok=True) is None  # old word, no wait
    assert time.perf_counter() - t0 < 0.04
    await w1_plan.wait_for_refreshes()
    assert boxes["asked"].count(WOL_EP) == 2  # asked again, in the background
    assert await w1_plan._served(WOL_EP, stale_ok=True) == frozenset({DS})


@pytest.mark.asyncio
async def test_one_background_refresh_per_box_at_a_time(boxes, clock):
    assert await w1_plan._served(WOL_EP) is None
    clock.now += w1_plan.SERVED_TTL_S + 1
    for _ in range(5):
        await w1_plan._served(WOL_EP, stale_ok=True)
    await w1_plan.wait_for_refreshes()
    assert boxes["asked"].count(WOL_EP) == 2


@pytest.mark.asyncio
async def test_without_stale_ok_an_expired_word_is_asked_fresh(boxes, clock):
    """The runner and the plan act on the answer — they never get an old one."""
    assert await w1_plan._served(WOL_EP) is None
    boxes["awake"].add(WOL_EP)
    clock.now += w1_plan.SERVED_TTL_S + 1
    assert await w1_plan._served(WOL_EP) == frozenset({DS})


@pytest.mark.asyncio
async def test_contestants_logs_its_step_times(session, boxes, caplog):
    import logging

    await seed_fleet(session)
    caplog.set_level(logging.DEBUG, logger="mc.bench_studio")
    await w1_plan.list_contestants(session)
    lines = [r.getMessage() for r in caplog.records if "contestants timing" in r.getMessage()]
    assert len(lines) == 1
    for step in ("load_views", "load_prices", "load_history", "last_tests", "loop", "total"):
        assert f"{step}=" in lines[0]


@pytest.mark.asyncio
async def test_a_cancelled_refresh_never_drops_the_newer_one():
    """Critic 21.11.2026: clear_live_cache() cancels a started refresh T1;
    T1's cleanup must not remove a newer refresh T2 for the same key, or a
    third reader starts a duplicate probe."""
    key = ("served", "http://probe-race.invalid")
    gate = asyncio.Event()

    async def ask():
        await gate.wait()
        return frozenset()

    w1_plan._refresh_behind(key, ask)
    await asyncio.sleep(0)  # T1 is running and waits on the gate
    w1_plan.clear_live_cache()
    w1_plan._refresh_behind(key, ask)
    t2 = w1_plan._refreshing.get(key)
    assert t2 is not None
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # T1's cancellation and cleanup have run
    try:
        assert w1_plan._refreshing.get(key) is t2
    finally:
        gate.set()
        await w1_plan.wait_for_refreshes()
    assert key not in w1_plan._refreshing
