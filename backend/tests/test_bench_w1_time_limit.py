"""Bench W1 — time, not tokens, ends a run (operator decision 25.10.2026).

A visual test must let the model finish the page. So:
- local runs ("local · costs nothing") send max_tokens = context − prompt
  room (none at all when the context limit is unknown);
- every run ends after 2 h wall clock with error code ``time_limit``;
- paid runs keep a large cap, and "default" gets the budget of the level the
  server really thinks at ("high (default)"), not factor 1;
- the cost question quotes the worst case of that cap.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
from sqlmodel import select

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchEntry, BenchProbe  # noqa: E402
from app.models.runtime import Runtime  # noqa: E402
from app.services import reasoning_params  # noqa: E402
from app.verticals.bench_studio import orchestrator as orch  # noqa: E402
from app.verticals.bench_studio import w1_common as c  # noqa: E402
from app.verticals.bench_studio import w1_plan, w1_receipt, w1_runner, w1_tests  # noqa: E402
from app.verticals.bench_studio import w1_schemas as s  # noqa: E402

from tests.test_bench_w1_fleet import (  # noqa: E402,F401
    DS, PAID, PAGE, FakeClock, engines, factory, no_sleep, seed_fleet, world,
)

CTX = 131072  # the seeded Spark slot's max_context_len


def _pick(rt, efforts=("default",)):
    return {"runtime_id": str(rt.id), "harness": "api", "efforts": list(efforts)}


async def _create(session, fleet, rt, preset="quick", efforts=("default",), **kw):
    return await w1_tests.create_test(session, picks=[_pick(rt, efforts)], preset=preset,
                                      trigger="runtimes", user_id=None, **kw)


async def _create_paid(session, fleet, efforts=("default",), preset="quick"):
    try:
        await _create(session, fleet, fleet["paid"], preset=preset, efforts=efforts)
    except Exception as exc:  # noqa: BLE001 — D4 asks first
        ask = exc.detail
    ch = await _create(session, fleet, fleet["paid"], preset=preset, efforts=efforts,
                       cost_confirmed_usd=ask["usd"], confirm_over_cap=True)
    return ch, ask


async def _paid_probe(session, fleet, default_level="high"):
    session.add(BenchProbe(runtime_id=fleet["paid"].id, served_model_id=PAID,
                           serving_since=fleet["paid"].serving_since, thinking_key="reasoning_effort",
                           effort_param="reasoning_effort", levels=["off", "low", "medium", "high", "max"],
                           default_observed="on", status="ok", raw={"default_level": default_level}))
    await session.commit()


async def _entries(session, ch_id):
    return (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch_id)
                               .order_by(BenchEntry.repeat_idx))).all()


# ── 1. local: the whole context, no practical cap ────────────────────────


def test_context_budget_is_context_minus_prompt_room():
    assert reasoning_params.context_budget(262144) == 262144 - 4096
    assert reasoning_params.context_budget(None) is None
    assert reasoning_params.context_budget(4096) is None


@pytest.mark.asyncio
async def test_local_run_sends_the_whole_context_as_max_tokens(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet, fleet["slot"])
    pc = ch.plan["contestants"][0]
    assert pc["max_tokens"] == CTX - 4096 and pc["max_tokens_rule"] == "context"
    assert pc["time_limit_min"] == 120
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert [b["max_tokens"] for b in world.chat] == [CTX - 4096]
    e = (await _entries(session, ch.id))[0]
    assert e.metrics["max_tokens"] == CTX - 4096


@pytest.mark.asyncio
async def test_local_run_with_unknown_context_sends_no_max_tokens(session, engines, world):
    """Unknown context limit: no guessed number (a too large one is a 400
    on vLLM) — the request carries no max_tokens, the engine uses its own
    maximum, the 2 h time limit ends the run."""
    fleet = await seed_fleet(session)
    slot = await session.get(Runtime, fleet["slot"].id)
    slot.max_context_len = None
    session.add(slot)
    await session.commit()
    ch = await _create(session, fleet, fleet["slot"])
    pc = ch.plan["contestants"][0]
    assert pc["max_tokens"] is None and pc["max_tokens_rule"] == "engine"
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert len(world.chat) == 1 and "max_tokens" not in world.chat[0]
    await session.refresh(ch)
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    rec = d.blocks[0].contestants[0].receipt
    assert rec.max_tokens is None and rec.max_tokens_rule == "engine" and rec.time_limit_min == 120


@pytest.mark.asyncio
async def test_plans_from_before_keep_their_stored_budget(session, engines, world):
    """A plan frozen before 25.10.2026 (no rule) runs as it was planned."""
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet, fleet["slot"])
    plan = dict(ch.plan)
    pc = dict(plan["contestants"][0])
    for k in ("max_tokens_rule", "time_limit_min"):
        pc.pop(k)
    pc["max_tokens"] = 16384
    plan["contestants"] = [pc]
    ch.plan = plan
    session.add(ch)
    await session.commit()
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert [b["max_tokens"] for b in world.chat] == [16384]


# ── 2. the 2 h time limit ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_ends_with_time_limit_after_two_hours(monkeypatch):
    """Fake clock: every look at the clock is 40 min later, so the stream is
    past 2 h after a few lines — the real limit, never a real wait."""
    clock = FakeClock(step=40 * 60)
    monkeypatch.setattr(w1_runner, "_clock", clock.monotonic)
    lines = "".join(f'data: {{"choices": [{{"delta": {{"content": "x{i}"}}}}]}}\n\n' for i in range(50))
    monkeypatch.setattr(w1_runner, "_transport", httpx.MockTransport(lambda r: httpx.Response(
        200, content=lines.encode(), headers={"content-type": "text/event-stream"})))
    with pytest.raises(w1_runner.RunError) as err:
        await w1_runner.stream_chat("http://engine/v1/chat/completions", {"stream": True}, api_key=None,
                                    time_limit_s=c.RUN_TIME_LIMIT_MIN * 60)
    assert err.value.code == "time_limit"


@pytest.mark.asyncio
async def test_stream_without_limit_or_inside_it_finishes(monkeypatch):
    clock = FakeClock(step=1.0)
    monkeypatch.setattr(w1_runner, "_clock", clock.monotonic)
    lines = 'data: {"choices": [{"delta": {"content": "<html></html>"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
    monkeypatch.setattr(w1_runner, "_transport", httpx.MockTransport(lambda r: httpx.Response(
        200, content=lines.encode(), headers={"content-type": "text/event-stream"})))
    text, m = await w1_runner.stream_chat("http://engine/v1/chat/completions", {"stream": True}, api_key=None,
                                          time_limit_s=c.RUN_TIME_LIMIT_MIN * 60)
    assert text == "<html></html>" and m["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_a_run_past_two_hours_fails_with_time_limit_and_is_not_retried(session, engines, world,
                                                                               monkeypatch):
    fleet = await seed_fleet(session)
    clock = FakeClock(step=40 * 60)
    monkeypatch.setattr(w1_runner, "_clock", clock.monotonic)
    ch = await _create(session, fleet, fleet["slot"])
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert len(world.chat) == 1, "a run that ran out of time is not tried for two more hours"
    e = (await _entries(session, ch.id))[0]
    assert e.status == "failed" and e.metrics["error_code"] == "time_limit"
    assert e.metrics["attempt_errors"] == ["time_limit"]
    assert "truncat" not in (e.error or "")


def test_one_limit_for_direct_and_agent_runs():
    assert c.RUN_TIME_LIMIT_MIN == 120 == c.AGENT_TIME_LIMIT_MIN
    assert orch.SPARK_TIMEOUT_S == orch.GENERATION_TIME_LIMIT_S == 2 * 60 * 60
    assert "time_limit" in w1_runner.NO_RETRY


@pytest.mark.asyncio
async def test_legacy_spark_retry_waits_two_hours_and_names_the_limit(monkeypatch):
    seen = {}

    class _Spark:
        llm_url = "http://spark:8000/v1"

        def __init__(self, timeout=None):
            self.timeout = timeout

        async def _resolve_llm_model(self):
            return "m"

    class _Client:
        def __init__(self, timeout=None):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr("app.services.spark_client.SparkClient", _Spark)
    monkeypatch.setattr(orch.httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError, match="time_limit"):
        await orch._spark_generate("p", None, max_tokens=65536)
    assert seen["timeout"] == 2 * 60 * 60  # flat, not scaled by tokens


# ── 3./4. paid: a large cap, "default" = the observed level ──────────────


@pytest.mark.asyncio
async def test_paid_default_gets_the_budget_of_the_level_it_really_thinks_at(session, engines):
    """Live bug: "high (default)" got factor 1 (16 384) while an explicit
    "high" got x2. The level the probe merged the default into drives it."""
    fleet = await seed_fleet(session)
    await _paid_probe(session, fleet, default_level="high")
    # "high" is not offered on its own here: it IS the default (merged chip).
    default, _ = await _create_paid(session, fleet, efforts=("default",))
    low, _ = await _create_paid(session, fleet, efforts=("low",))
    budget = {name: ch.plan["contestants"][0]["max_tokens"] for name, ch in
              (("default", default), ("low", low))}
    assert budget["default"] == 2 * c.PAID_BASE_MAX_TOKENS  # = what an explicit "high" gets
    assert budget["low"] == c.PAID_BASE_MAX_TOKENS
    assert default.plan["contestants"][0]["max_tokens_rule"] == "cap"


@pytest.mark.asyncio
async def test_paid_default_without_a_merged_level_uses_what_the_probe_saw(session, engines):
    fleet = await seed_fleet(session)
    session.add(BenchProbe(runtime_id=fleet["paid"].id, served_model_id=PAID,
                           serving_since=fleet["paid"].serving_since, thinking_key="chat_template_kwargs.thinking",
                           effort_param=None, levels=["off", "on"], default_observed="on", status="ok", raw={}))
    await session.commit()
    ch, _ = await _create_paid(session, fleet)
    assert ch.plan["contestants"][0]["max_tokens"] == 2 * c.PAID_BASE_MAX_TOKENS


@pytest.mark.asyncio
async def test_paid_cap_never_passes_the_ceiling(session, engines):
    fleet = await seed_fleet(session)
    await _paid_probe(session, fleet)
    ch, _ = await _create_paid(session, fleet, efforts=("max",))
    assert ch.plan["contestants"][0]["max_tokens"] == c.PAID_MAX_TOKENS_CEILING


@pytest.mark.asyncio
async def test_cost_question_quotes_the_worst_case_of_the_paid_cap(session, engines):
    """D4: the confirmed amount is never too low — every run up to its cap."""
    fleet = await seed_fleet(session)
    await _paid_probe(session, fleet)
    _, ask = await _create_paid(session, fleet, efforts=("default",), preset="standard")
    tok_in = w1_plan.DEFAULT_TOKENS_IN
    worst = 3 * (tok_in * 15 + 2 * c.PAID_BASE_MAX_TOKENS * 75) / 1e6
    assert ask["usd"] == pytest.approx(round(worst, 4))
    # The sheet's per-contestant money is quoted for the largest budget any
    # offered level gets, so picking "max" never asks for more than shown.
    lst = await w1_plan.list_contestants(session)
    paid = next(x for x in lst["cloud"] if x["runtime_id"] == fleet["paid"].id)
    assert paid["money"]["usd"] == pytest.approx(round(3 * (tok_in * 15 + c.PAID_MAX_TOKENS_CEILING * 75) / 1e6, 4))
    plan = await w1_plan.build_plan(session, runtime_id=fleet["paid"].id, preset="standard",
                                    template_id=None, rerun_of=None)
    assert plan["money"]["usd"] == pytest.approx(round(worst, 4))


# ── 5. estimate, receipt, truncation hint ────────────────────────────────


@pytest.mark.asyncio
async def test_local_estimate_stays_realistic_and_inside_the_limit(session, engines):
    """No cap must not turn into "about 9 h": the estimate of a local deep
    run counts the tokens such a run writes (16k x effort factor), one run
    never longer than 2 h."""
    fleet = await seed_fleet(session)
    view = next(v for v in await w1_plan.load_views(session) if v.runtime.id == fleet["slot"].id)
    hist = w1_plan.History(a_run_s=[60.0], a_decode=[25.0])
    tokens = w1_plan.expected_tokens(view, "high", None)
    assert tokens == 2 * c.BASE_MAX_TOKENS  # not the 126 976 of the context
    est = w1_plan.estimate_a_effort(hist, 3, "high", tokens)
    assert est["max_s"] == int(3 * 2 * c.BASE_MAX_TOKENS / 25.0)
    slow = w1_plan.estimate_a_effort(w1_plan.History(a_run_s=[60.0], a_decode=[1.0]), 3, "max", 4 * 16384)
    assert slow["max_s"] == 3 * c.RUN_TIME_LIMIT_MIN * 60
    merged = {"options": [{"level": "default", "is_default": True, "merged_level": "high"}]}
    assert w1_plan.effective_level("default", merged) == "high"
    assert w1_plan.expected_tokens(view, "default", merged) == 2 * c.BASE_MAX_TOKENS


def test_truncation_offers_a_larger_budget_only_for_capped_runs():
    class E:
        def __init__(self, i, rule_max):
            self.id = uuid.uuid4()
            self.status = "generated"
            self.repeat_idx = i
            self.metrics = {"truncated": True, "finish_reason": "length", "max_tokens": rule_max,
                            "tokens_out": 90000}

    local = ({"run_class": "A", "max_tokens_rule": "context", "max_tokens": 126976}, [E(1, 126976)])
    paid = ({"run_class": "A", "max_tokens_rule": "cap", "max_tokens": 32768}, [E(1, 32768)])
    t_local = w1_receipt.truncation_info([local], context_limit=CTX)
    assert t_local["larger_budget"] is False and t_local["suggested_max_tokens"] == 126976
    # Engine maximum (context unknown): no number to double, still no offer.
    engine = ({"run_class": "A", "max_tokens_rule": "engine", "max_tokens": None}, [E(1, None)])
    t_engine = w1_receipt.truncation_info([engine], context_limit=None)
    assert t_engine["larger_budget"] is False and t_engine["max_tokens"] == 90000
    t_paid = w1_receipt.truncation_info([paid], context_limit=1_000_000)
    assert t_paid["larger_budget"] is True and t_paid["suggested_max_tokens"] == 65536
    old = ({"run_class": "A", "max_tokens": 16384}, [E(1, 16384)])  # plan from before: a cap
    assert w1_receipt.truncation_info([old], context_limit=CTX)["larger_budget"] is True


@pytest.mark.asyncio
async def test_receipt_and_kit_say_how_the_budget_was_set(session, engines, world):
    from app.verticals.bench_studio import w1_kit

    fleet = await seed_fleet(session)
    ch = await _create(session, fleet, fleet["slot"])
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch)
    rec = s.BenchTestDetail.model_validate(d).blocks[0].contestants[0].receipt
    assert rec.max_tokens == CTX - 4096 and rec.max_tokens_rule == "context" and rec.time_limit_min == 120
    blob = " ".join(w1_kit.settings_blocks(d))
    assert f"max_tokens {CTX - 4096} (full context)" in blob and "time limit 120 min" in blob
