"""Bench W1 plan builder (W1.2): GET /plan, /contestants, /runtime-summary.

A fixed fleet that mirrors the live situation of 22.10. (concept 30 W1.2
acceptance): one DGX Spark engine served by two boxes with a slot row
(DeepSeek) and a drifted GLM recipe row on the same port, a sleeping
power-managed box, an Ollama-cloud runtime (subscription) and a paid API
runtime. Engines are faked (no network)."""
from __future__ import annotations

import uuid
from datetime import timedelta, timezone

import pytest
from sqlmodel import select

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchChallenge, BenchEntry, BenchProbe  # noqa: E402
from app.verticals.bench_studio import w1_plan, w1_schemas as s  # noqa: E402

from tests.test_bench_w1_fleet import (  # noqa: E402,F401 — shared fleet + the engines fixture
    CLOUD_DS, DS, GLM, PAID, PORSCHE_EP, SPARK_EP, engines, seed_fleet,
)


# ── contestants ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_spark_row_with_the_served_model_never_the_drifted_one(session, engines):
    await seed_fleet(session)
    out = s.ContestantList.model_validate(await w1_plan.list_contestants(session))
    spark = [c for c in out.local if c.device and c.device.name.startswith("DGX Spark")]
    assert len(spark) == 1, [c.display_name for c in out.local]
    row = spark[0]
    assert row.display_name == row.served_model_id == DS  # R2, never GLM, never agents.model
    assert row.device.name == "DGX Spark ×2" and row.device.nodes == 2
    assert row.device.gpu_mode == "eco"
    # engine unknown without a recipe, spec decoding unknown too (never "off")
    assert row.engine is None and row.spec_decoding is None
    assert row.money.cost_kind == "local" and row.money.usd is None
    assert row.money.hypothetical_usd and row.money.hypothetical_usd > 0
    harnesses = {h.harness: h for h in row.harnesses}
    assert harnesses["api"].run_class == "A" and harnesses["api"].effort_settable
    assert harnesses["omp"].run_class == "B" and not harnesses["omp"].effort_settable  # D5


@pytest.mark.asyncio
async def test_label_never_comes_from_agents_model(session, engines):
    await seed_fleet(session, agent_model="Qwen-3.5-35B")
    out = await w1_plan.list_contestants(session)
    names = [c["display_name"] for c in out["local"] + out["cloud"]]
    assert "Qwen-3.5-35B" not in names
    assert DS in names


@pytest.mark.asyncio
async def test_sleeping_box_is_listed_with_a_blocking_reason(session, engines):
    await seed_fleet(session)
    out = s.ContestantList.model_validate(await w1_plan.list_contestants(session))
    porsche = next(c for c in out.local if c.device and c.device.name == "RTX 5090")
    assert porsche.device.state == "asleep"
    assert [b.code for b in porsche.blockers] == ["box_asleep"] and porsche.blockers[0].blocking


@pytest.mark.asyncio
async def test_device_names_never_carry_an_address(session, engines):
    await seed_fleet(session)
    out = await w1_plan.list_contestants(session)
    blob = repr(out)
    assert "203.0.113." not in blob and "spark-a" not in blob


@pytest.mark.asyncio
async def test_cloud_money_subscription_and_api_price(session, engines):
    await seed_fleet(session)
    out = s.ContestantList.model_validate(await w1_plan.list_contestants(session))
    cloud = {c.display_name: c for c in out.cloud}
    sub = cloud[CLOUD_DS]
    assert sub.money.cost_kind == "subscription" and sub.money.usd is None
    assert sub.money.hypothetical_usd and sub.provider == "Ollama Cloud"
    assert sub.cloud_build_unknown  # N7: "gleicher Modellname · Cloud-Version unbekannt"
    paid = cloud[PAID]
    assert paid.money.cost_kind == "api_price" and paid.money.needs_confirmation
    # worst case of 3 runs at the full token budget is over $1 -> 2nd question (D4)
    assert paid.money.usd > 1 and paid.money.second_confirmation


@pytest.mark.asyncio
async def test_catch_all_price_is_unknown_never_zero(session, engines):
    fleet = await seed_fleet(session)
    fleet["paid"].model_identifier = "mystery-model"
    session.add(fleet["paid"])
    await session.commit()
    out = await w1_plan.list_contestants(session)
    row = next(c for c in out["cloud"] if c["display_name"] == "mystery-model")
    assert row["money"]["cost_kind"] == "unknown"
    assert row["money"].get("usd") is None and row["money"]["reason"]["code"] == "price_unknown"


# ── effort chips from the stored probe ───────────────────────────────────


@pytest.mark.asyncio
async def test_effort_unchecked_without_a_probe_offers_only_default(session, engines):
    await seed_fleet(session)
    out = await w1_plan.list_contestants(session)
    row = next(c for c in out["local"] if c["display_name"] == DS)
    assert row["effort"]["status"] == "unchecked"
    assert [o["level"] for o in row["effort"]["options"]] == ["default"]


@pytest.mark.asyncio
async def test_effort_ok_merges_high_into_default(session, engines):
    fleet = await seed_fleet(session)
    session.add(BenchProbe(runtime_id=fleet["slot"].id, served_model_id=DS,
                           serving_since=fleet["slot"].serving_since, thinking_key="reasoning_effort",
                           effort_param="reasoning_effort", levels=["off", "low", "medium", "high", "max"],
                           default_observed="on", status="ok", raw={"default_level": "high"}))
    await session.commit()
    out = s.ContestantList.model_validate(await w1_plan.list_contestants(session))
    row = next(c for c in out.local if c.display_name == DS)
    assert row.effort.status == "ok" and row.effort.default_observed == "on"
    levels = [o.level for o in row.effort.options]
    assert levels == ["default", "off", "low", "medium", "max"]  # "high" merged
    assert row.effort.options[0].is_default and row.effort.options[0].merged_level == "high"
    assert row.effort.probed_at is not None


@pytest.mark.asyncio
async def test_effort_only_on_off_and_failed(session, engines):
    fleet = await seed_fleet(session)
    probe = BenchProbe(runtime_id=fleet["slot"].id, served_model_id=DS,
                       serving_since=fleet["slot"].serving_since, thinking_key="chat_template_kwargs.enable_thinking",
                       effort_param=None, levels=["off"], default_observed="on", status="ok", raw={})
    session.add(probe)
    await session.commit()
    row = next(c for c in (await w1_plan.list_contestants(session))["local"] if c["display_name"] == DS)
    assert row["effort"]["status"] == "only_on_off"
    assert [o["level"] for o in row["effort"]["options"]] == ["default", "off"]
    probe.status = "failed"
    session.add(probe)
    await session.commit()
    row = next(c for c in (await w1_plan.list_contestants(session))["local"] if c["display_name"] == DS)
    assert row["effort"]["status"] == "failed"
    assert [o["level"] for o in row["effort"]["options"]] == ["default"]


# ── blockers ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_model_waits_with_a_start_time(session, engines):
    fleet = await seed_fleet(session, serving_age_min=6)
    out = await w1_plan.list_contestants(session)
    row = next(c for c in out["local"] if c["display_name"] == DS)
    fresh = next(b for b in row["blockers"] if b["code"] == "model_fresh")
    assert fresh["blocking"] is False
    expected = fleet["slot"].serving_since.replace(tzinfo=timezone.utc) + timedelta(minutes=15)
    assert abs((fresh["until"] - expected).total_seconds()) < 1


@pytest.mark.asyncio
async def test_busy_engine_is_a_waiting_blocker(session, engines):
    await seed_fleet(session)
    engines["in_use"][SPARK_EP] = True
    out = await w1_plan.list_contestants(session)
    row = next(c for c in out["local"] if c["display_name"] == DS)
    assert {"code": "box_busy", "blocking": False} in row["blockers"]
    assert row["device"]["state"] == "busy"


# ── plan ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_prefills_everything_for_the_live_model(session, engines):
    await seed_fleet(session)
    plan = s.BenchPlan.model_validate(await w1_plan.build_plan(
        session, runtime_id=None, preset="standard", template_id=None, rerun_of=None))
    assert plan.preset.id == "standard" and plan.preset.n_runs == 3
    assert plan.preset.prompt.title == "Pagoda Garden" and len(plan.preset.prompt.sha256) == 64
    assert [p.id for p in plan.presets] == ["standard", "quick", "effort_ladder"]
    assert plan.contestants[0].display_name == DS
    assert plan.picks[0].harness == "api" and plan.picks[0].efforts == ["default"]
    assert plan.money.cost_kind == "local"
    assert plan.estimate.basis == "default" and plan.estimate.min_s > 0
    assert plan.sequential is False and plan.max_video_contestants == 2


@pytest.mark.asyncio
async def test_plan_for_a_recipe_row_resolves_to_its_live_engine(session, engines):
    fleet = await seed_fleet(session)
    plan = await w1_plan.build_plan(session, runtime_id=fleet["slot"].id, preset="quick",
                                    template_id=None, rerun_of=None)
    assert plan["preset"]["n_runs"] == 1
    assert plan["contestants"][0]["runtime_id"] == fleet["slot"].id


@pytest.mark.asyncio
async def test_plan_history_estimate_and_effort_ladder(session, engines):
    fleet = await seed_fleet(session)
    session.add(BenchProbe(runtime_id=fleet["slot"].id, served_model_id=DS,
                           serving_since=fleet["slot"].serving_since, thinking_key="reasoning_effort",
                           effort_param="reasoning_effort", levels=["off", "low", "medium", "high", "max"],
                           default_observed="on", status="ok", raw={"default_level": "high"}))
    ch = BenchChallenge(title="Pagoda", prompt_text="p", status="review")
    session.add(ch)
    await session.flush()
    for ms in (62_000, 71_000):
        session.add(BenchEntry(challenge_id=ch.id, model_label=DS, source_kind="api", run_class="A",
                               runtime_id=fleet["slot"].id, status="rendered",
                               metrics={"gen_ms": ms, "tokens_in": 300, "tokens_out": 6600,
                                        "decode_tok_s": 101.0}))
    await session.commit()
    plan = s.BenchPlan.model_validate(await w1_plan.build_plan(
        session, runtime_id=fleet["slot"].id, preset="effort_ladder", template_id=None, rerun_of=None))
    assert plan.preset.multi_effort
    assert plan.picks[0].efforts == ["off", "default", "max"]
    # max can think until its scaled budget: the range comes from the rate
    assert plan.estimate.basis == "probe" and plan.estimate.max_s > 3 * 71
    assert plan.contestants[0].last_test.id == ch.id


@pytest.mark.asyncio
async def test_plan_rerun_reuses_the_frozen_plan(session, engines):
    fleet = await seed_fleet(session)
    old = BenchChallenge(title="Shot Sequence", prompt_text="build a shot", status="review",
                         protocol_version="v1", prompt_sha256="a" * 64,
                         plan={"n_runs": 1, "picks": [{"runtime_id": str(fleet["slot"].id),
                                                       "harness": "api", "efforts": ["default"]}]})
    session.add(old)
    await session.commit()
    plan = s.BenchPlan.model_validate(await w1_plan.build_plan(
        session, runtime_id=None, preset="standard", template_id=None, rerun_of=old.id))
    assert plan.preset.id == "rerun" and plan.preset.n_runs == 1
    assert plan.preset.prompt.title == "Shot Sequence"
    assert [p.id for p in plan.presets][-1] == "rerun"


@pytest.mark.asyncio
async def test_plan_with_agent_pick_is_sequential(session, engines):
    fleet = await seed_fleet(session)
    old = BenchChallenge(title="t", prompt_text="p", status="review", protocol_version="v1",
                         plan={"n_runs": 3, "picks": [
                             {"runtime_id": str(fleet["slot"].id), "harness": "api", "efforts": ["default"]},
                         ]})
    session.add(old)
    await session.commit()
    plan = await w1_plan.build_plan(session, runtime_id=None, preset="standard", template_id=None,
                                    rerun_of=old.id)
    assert plan["sequential"] is False


# ── runtime summary ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_summary_primary_and_reasons(session, engines):
    fleet = await seed_fleet(session)
    rows = {r["runtime_id"]: r for r in await w1_plan.runtime_summary(session)}
    [s.RuntimeTestSummary.model_validate(r) for r in rows.values()]
    assert rows[fleet["slot"].id]["testable"] and rows[fleet["slot"].id]["primary"]
    assert rows[fleet["glm"].id]["testable"] is False  # drifted recipe row
    assert rows[fleet["porsche"].id]["reason"]["code"] == "box_asleep"
    assert rows[fleet["ollama"].id]["testable"] and not rows[fleet["ollama"].id].get("primary")
    assert sum(1 for r in rows.values() if r.get("primary")) == 1  # K11: one primary


# ── routes ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_routes_are_real_and_read_only(auth_client, session, engines):
    await seed_fleet(session)
    before = len((await session.exec(select(BenchProbe))).all())
    for path, model in (("/api/v1/bench/plan", s.BenchPlan), ("/api/v1/bench/contestants", s.ContestantList)):
        resp = await auth_client.get(path)
        assert resp.status_code == 200, resp.text
        assert "x-bench-contract-stub" not in resp.headers
        model.model_validate(resp.json())
    resp = await auth_client.get("/api/v1/bench/runtime-summary")
    assert resp.status_code == 200 and "x-bench-contract-stub" not in resp.headers
    after = len((await session.exec(select(BenchProbe))).all())
    assert before == after  # never probes


@pytest.mark.asyncio
async def test_plan_unknown_runtime_is_404(auth_client, session, engines):
    await seed_fleet(session)
    resp = await auth_client.get(f"/api/v1/bench/plan?runtime_id={uuid.uuid4()}")
    assert resp.status_code == 404
