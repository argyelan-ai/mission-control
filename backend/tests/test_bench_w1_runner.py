"""Bench W1 runner (W1.3, W1.4, W1.7-backend, W1.9, W1.11).

Fake engine (OpenAI-compatible SSE), fake renderer and recorder — no
network, no real bench run. The runner's own sessions use the test engine
(``factory``)."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

pytest.importorskip("app.verticals.bench_studio")

from app.models.approval import Approval  # noqa: E402
from app.models.bench import BenchChallenge, BenchEntry, BenchReceipt  # noqa: E402
from app.models.runtime import Runtime  # noqa: E402
from app.verticals.bench_studio import w1_common as c  # noqa: E402
from app.verticals.bench_studio import w1_kit, w1_runner, w1_tests  # noqa: E402
from app.verticals.bench_studio import w1_schemas as s  # noqa: E402

from tests.test_bench_w1_fleet import (  # noqa: E402,F401
    DS, PAGE, SPARK_EP, engines, factory, no_sleep, seed_fleet, world,
)


async def _create(session, fleet, *, preset="standard", efforts=("default",), harness="api", extra=None):
    picks = [{"runtime_id": fleet["slot"].id, "harness": harness, "efforts": list(efforts)}]
    if extra:
        picks.append(extra)
    return await w1_tests.create_test(session, picks=picks, preset=preset, trigger="runtimes", user_id=None)


async def _entries(session, ch_id):
    return (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch_id)
                               .order_by(BenchEntry.position, BenchEntry.repeat_idx))).all()


# ── streaming measurement ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_measures_ttft_decode_finish_and_reasoning(world):
    content, m = await w1_runner.stream_chat(
        f"{SPARK_EP}/chat/completions",
        {"model": DS, "messages": [], "stream": True, "stream_options": {"include_usage": True}},
        api_key=None,
    )
    assert content == PAGE
    assert m["tokens_out"] == 6607 and m["tokens_in"] == 300
    assert m["tokens_reasoning"] == 2140 and m["effort_observed"]["thinking"] == "on"
    assert m["finish_reason"] == "stop" and m["truncated"] is False
    assert m["ttft_ms"] > 0 and m["gen_ms"] > m["ttft_ms"]
    # decode can never be below the wall average (concept §5.3)
    assert m["decode_tok_s"] >= m["wall_tok_s"] and m["decode_sane"] is True


@pytest.mark.asyncio
async def test_stream_without_streaming_reports_no_decode(world):
    world.sse = False
    _content, m = await w1_runner.stream_chat(f"{SPARK_EP}/chat/completions", {"model": DS, "stream": True},
                                              api_key=None)
    assert m["tokens_out"] == 6607 and m["streamed"] is False
    assert "decode_tok_s" not in m and "ttft_ms" not in m


@pytest.mark.asyncio
async def test_stream_without_usage_is_flagged(world):
    world.usage = False
    _content, m = await w1_runner.stream_chat(f"{SPARK_EP}/chat/completions", {"model": DS, "stream": True},
                                              api_key=None)
    assert m["usage_missing"] is True and m.get("tokens_out") is None


@pytest.mark.asyncio
async def test_buffered_stream_is_not_a_decode_number(world, monkeypatch):
    """All chunks at one instant (a buffering proxy): no decode claim."""
    ticks = iter([0.0] + [100.0 + i * 0.001 for i in range(50)])
    monkeypatch.setattr(w1_runner, "_clock", lambda: next(ticks))
    _content, m = await w1_runner.stream_chat(f"{SPARK_EP}/chat/completions", {"model": DS, "stream": True},
                                              api_key=None)
    assert m["decode_tok_s"] > 0  # a number was computed …
    assert m["decode_sane"] is False  # … and refused: the chunks came in one burst


# ── one whole test ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_standard_test_runs_three_times_records_only_run_one(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)

    assert len(world.chat) == 3
    for body in world.chat:
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        assert body["temperature"] == 0.7 and body["model"] == DS
        assert "reasoning_effort" not in body  # default = nothing sent
    entries = await _entries(session, ch.id)
    assert [e.repeat_idx for e in entries] == [1, 2, 3]
    assert len(world.records) == 1 and world.records[0] == str(entries[0].id)  # W1.9
    assert entries[0].video_path and not entries[1].video_path and not entries[2].video_path

    await session.refresh(ch)
    assert ch.status == "review"
    detail = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    a = detail.blocks[0].contestants[0]
    assert detail.state == "done" and a.summary.time.n == 3
    assert a.summary.tokens_out.median == 6607 and a.summary.tokens_out.min == 6400
    assert a.summary.decode_tok_s.measured and a.summary.ttft_ms.measured
    assert a.summary.cost.cost_kind == "local" and a.summary.cost.usd is None
    assert a.receipt.protocol_version == "v1" and a.receipt_version == 1
    assert a.receipt.n_runs == 3 and a.receipt.attempts == 3 and a.receipt.failures == 0
    assert a.receipt.quiet_box == "ok" and a.receipt.network_blocked is True
    assert detail.gate.status in ("green", "amber") and detail.gate.checksum
    assert detail.verdict.served_model_id == DS
    # one receipt row per contestant, version 1
    rows = (await session.exec(select(BenchReceipt))).all()
    assert len(rows) == 1 and rows[0].version == 1 and rows[0].reason == "run_end"
    # renderer: card PNG + composed video from ONE payload, names carry the checksum
    paths = {p: b for p, b in world.renderer}
    assert paths["card"]["card"] == paths["compose"]["w1"]["card"]
    assert paths["card"]["width"] == 1080 and paths["card"]["height"] == 1350
    assert detail.gate.checksum[:12] in paths["card"]["output_path"]
    assert detail.gate.checksum[:12] in ch.composed_video_path
    card = paths["card"]["card"]
    assert card["contestants"][0]["model"] == DS
    assert [n["value"] for n in card["contestants"][0]["numbers"]][1] == "6.6k"
    # Eingang item "Test fertig"
    approvals = (await session.exec(select(Approval).where(Approval.action_type == "bench_result"))).all()
    assert len(approvals) == 1 and approvals[0].payload["test_id"] == str(ch.id)
    assert approvals[0].payload["href"] == f"/bench/r/{ch.id}"


@pytest.mark.asyncio
async def test_truncated_run_warns_privately_and_never_publicly(session, engines, world):
    fleet = await seed_fleet(session)
    world.finish = ["stop", "length", "stop"]
    world.tokens_out = [6607, 16384, 6900]
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch)
    detail = s.BenchTestDetail.model_validate(d)
    assert detail.truncation.runs_cut == 1 and detail.truncation.n_runs == 3
    assert detail.truncation.suggested_max_tokens > detail.truncation.max_tokens == 16384
    assert detail.section == "needs_look" and "rerun_larger" in detail.allowed
    assert any(r.code == "runs_truncated" and r.fix == "rerun_larger" for r in detail.gate.reasons)
    assert detail.gate.status != "red"
    public = " ".join(w1_kit.public_strings(d) + w1_kit.settings_blocks(d) + w1_kit.facts(d)).lower()
    assert "truncat" not in public and "token limit" not in public and "length" not in public
    card = dict(world.renderer)["card"]["card"]
    assert "truncat" not in str(card).lower() and "limit" not in str(card).lower()


@pytest.mark.asyncio
async def test_failed_attempt_is_counted_and_retried(session, engines, world):
    fleet = await seed_fleet(session)
    world.fail_runs = 1
    ch = await _create(session, fleet, preset="quick")
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    entries = await _entries(session, ch.id)
    assert entries[0].status == "rendered" and entries[0].attempt_no == 2
    assert entries[0].metrics["attempt_errors"] == ["http_error"]
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, (await session.get(BenchChallenge, ch.id))))
    rec = d.blocks[0].contestants[0].receipt
    assert rec.attempts == 2 and rec.failures == 1


@pytest.mark.asyncio
async def test_all_runs_failing_aborts_with_a_reason(session, engines, world):
    fleet = await seed_fleet(session)
    world.fail_runs = 99
    ch = await _create(session, fleet, preset="quick")
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch)
    assert d["state"] == "aborted" and d["state_reason"]["code"] == "all_runs_failed"
    assert d["section"] == "needs_look"
    assert (await session.exec(select(Approval).where(Approval.action_type == "bench_result"))).first()


@pytest.mark.asyncio
async def test_effort_is_sent_with_the_probed_key(session, engines, world):
    from app.models.bench import BenchProbe

    fleet = await seed_fleet(session)
    session.add(BenchProbe(runtime_id=fleet["slot"].id, served_model_id=DS,
                           serving_since=fleet["slot"].serving_since, thinking_key="reasoning_effort",
                           effort_param="reasoning_effort", levels=["off", "low", "medium", "high", "max"],
                           default_observed="on", status="ok", raw={}))
    await session.commit()
    ch = await _create(session, fleet, preset="effort_ladder", efforts=("off", "max"))
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    sent = [b.get("reasoning_effort") for b in world.chat]
    assert sent == ["none"] * 3 + ["max"] * 3  # A contestants one after another
    maxes = [b["max_tokens"] for b in world.chat]
    assert maxes[:3] == [16384] * 3 and maxes[3] > 16384  # max effort gets a scaled budget
    assert len(world.records) == 2  # two video contestants, run 1 each


# ── quiet box (W1.4) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_model_waits_until_fifteen_minutes(session, engines, world):
    fleet = await seed_fleet(session, serving_age_min=5)
    ch = await _create(session, fleet, preset="quick")
    seen = []

    async def sleep(_s):
        async with factory() as s2:
            c2 = await s2.get(BenchChallenge, ch.id)
            seen.append((c2.wait_reason, (c2.plan or {}).get("not_before")))
            live = await w1_tests.test_detail(s2, c2)
            assert live["state"] == "waiting" and live["state_reason"]["code"] == "model_fresh"
            rt = await s2.get(Runtime, fleet["slot"].id)
            rt.serving_since = rt.serving_since - timedelta(minutes=11)
            s2.add(rt)
            await s2.commit()

    await w1_runner.run_test(ch.id, factory, sleep=sleep)
    assert seen and seen[0][0] == "model_fresh" and seen[0][1]
    d = await w1_tests.test_detail(session, await session.get(BenchChallenge, ch.id))
    assert d["blocks"][0]["contestants"][0]["receipt"]["serving_age_min"] >= 15


@pytest.mark.asyncio
async def test_busy_box_waits_ten_minutes_then_runs_amber(session, engines, world):
    fleet = await seed_fleet(session)
    engines["in_use"][SPARK_EP] = True
    ch = await _create(session, fleet, preset="quick")
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    await w1_runner.run_test(ch.id, factory, sleep=sleep)
    assert sum(sleeps) >= c.QUIET_WAIT_MAX_S
    ch = await session.get(BenchChallenge, ch.id)
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch)
    rec = d["blocks"][0]["contestants"][0]["receipt"]
    assert rec["quiet_box"] == "timeout"
    assert any(r["code"] == "box_was_busy" for r in d["gate"]["reasons"])
    assert not any("tok/s" in f for f in w1_kit.facts(d))  # speed not comparable


# ── class A before class B ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_runs_after_all_api_runs(session, engines, world, monkeypatch):
    from app.verticals.bench_studio import orchestrator as orch

    fleet = await seed_fleet(session)
    fleet["omp_agent"].operational_mode = "active"
    session.add(fleet["omp_agent"])
    await session.commit()
    dispatched = []

    async def dispatch(s2, entry, challenge):
        world.order.append("B")
        dispatched.append(len(world.chat))
        entry.status = "generating"
        s2.add(entry)
        await s2.commit()

    monkeypatch.setattr(orch, "dispatch_agent_entry", dispatch)

    async def sleep(_s):
        async with factory() as s2:
            e = (await s2.exec(select(BenchEntry).where(BenchEntry.run_class == "B"))).first()
            if e.status == "generating":
                e.status = "generated"
                e.metrics = {**(e.metrics or {}), "duration_ms": 5_551_000}
                s2.add(e)
                await s2.commit()

    ch = await _create(session, fleet, extra={"runtime_id": fleet["slot"].id, "harness": "omp",
                                               "efforts": ["default"]})
    detail = await w1_tests.test_detail(session, ch)
    b_run = detail["blocks"][1]["contestants"][0]["runs"][0]
    assert b_run["wait"] == {"code": "after_class_a", "params": {"after_run": 3}}
    await w1_runner.run_test(ch.id, factory, sleep=sleep)
    assert world.order == ["A", "A", "A", "B"] and dispatched == [3]
    await session.refresh(ch)
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    assert [b.run_class for b in d.blocks] == ["A", "B"] and d.blocks[1].starts_after == "A"
    b = d.blocks[1].contestants[0]
    assert b.summary.decode_tok_s is None and b.summary.time_kind == "active_ms"  # no tempo on B
    assert b.receipt.time_limit_min == c.AGENT_TIME_LIMIT_MIN and b.receipt.brief_sha
    assert len(world.records) == 1  # the video shows class A only (never mixed)


# ── restart heal + auto tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_heal_requeues_with_a_counted_attempt(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet, preset="quick")
    e = (await _entries(session, ch.id))[0]
    e.status = "generating"
    ch.status = "generating"
    session.add_all([e, ch])
    await session.commit()
    await w1_runner.heal_after_restart(factory)
    await session.refresh(e)
    await session.refresh(ch)
    assert e.status == "pending" and e.attempt_no == 2 and e.metrics["attempt_errors"] == ["backend_restart"]
    assert ch.status == "waiting"
    assert await w1_runner.next_test_id(factory) == ch.id


@pytest.mark.asyncio
async def test_auto_test_waits_for_the_switched_model_then_freezes_the_plan(session, engines, world):
    fleet = await seed_fleet(session)
    out = await w1_tests.create_auto_test(session, host_id=fleet["spark1"].id, recipe_slug="ds-recipe",
                                          user_id=None)
    tid = out["test_id"]
    ch = await session.get(BenchChallenge, tid)
    assert ch.status == "waiting" and ch.wait_reason == "model_loading"
    # the slot has served since BEFORE the switch request: not the new model yet
    await w1_runner.resolve_auto_tests(factory)
    await session.refresh(ch)
    assert not ch.plan["contestants"]
    rt = await session.get(Runtime, fleet["slot"].id)
    rt.serving_since = c.now_utc() - timedelta(minutes=1)
    session.add(rt)
    await session.commit()
    await w1_runner.resolve_auto_tests(factory)
    await session.refresh(ch)
    assert ch.plan["contestants"][0]["served_model_id"] == DS and ch.wait_reason == "model_fresh"
    assert len(await _entries(session, tid)) == 3
    assert await w1_runner.next_test_id(factory) is None  # not before serving_since + 15 min
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    assert d.state == "waiting" and d.state_reason.code == "model_fresh" and d.state_reason.params["until"]


@pytest.mark.asyncio
async def test_second_switch_supersedes_the_pending_auto_test(session, engines):
    fleet = await seed_fleet(session)
    first = await w1_tests.create_auto_test(session, host_id=fleet["spark1"].id, recipe_slug="a", user_id=None)
    second = await w1_tests.create_auto_test(session, host_id=fleet["spark1"].id, recipe_slug="b", user_id=None)
    assert second["superseded_test_id"] == first["test_id"]
    old = await session.get(BenchChallenge, first["test_id"])
    await session.refresh(old)
    d = await w1_tests.test_detail(session, old)
    assert d["state"] == "aborted" and d["state_reason"]["code"] == "superseded_by_switch"
    assert d["section"] == "done"


@pytest.mark.asyncio
async def test_switch_that_never_answers_fails_the_auto_test(session, engines):
    fleet = await seed_fleet(session)
    engines["served"][SPARK_EP] = None  # the switched engine never answers
    out = await w1_tests.create_auto_test(session, host_id=fleet["spark1"].id, recipe_slug="x", user_id=None)
    ch = await session.get(BenchChallenge, out["test_id"])
    ch.plan = {**ch.plan, "auto": {**ch.plan["auto"],
                                   "requested_at": (c.now_utc() - timedelta(hours=2)).isoformat()}}
    session.add(ch)
    await session.commit()
    await w1_runner.resolve_auto_tests(factory)
    await session.refresh(ch)
    assert ch.status == "failed" and ch.error == "switch_failed"


# ── receipts are write-once; harvest writes a new version ────────────────


@pytest.mark.asyncio
async def test_harvest_writes_a_new_receipt_version_never_updates(session, engines, world, monkeypatch):
    from datetime import datetime, timezone

    from app.models.model_usage import ModelUsageEvent
    from app.models.task import Task

    fleet = await seed_fleet(session)
    ch = BenchChallenge(title="Shot Sequence", prompt_text="shot", status="review")
    session.add(ch)
    await session.flush()
    task = Task(board_id=fleet["board"].id, title="[Bench] shot", status="done")
    session.add(task)
    await session.flush()
    e = BenchEntry(challenge_id=ch.id, model_label="Alpha omp", source_kind="agent", agent_id=fleet["omp_agent"].id,
                   task_id=task.id, status="rendered", run_class="B", metrics={"duration_ms": 5_551_000})
    session.add(e)
    await session.commit()

    d0 = await w1_tests.test_detail(session, ch)
    assert d0["gate"]["status"] == "red"
    assert any(r["rule"] == "R1" and r["fix"] == "harvest" for r in d0["gate"]["reasons"])

    async def no_effort(*_a, **_k):
        return {"source": "omp_session", "level": "high"}

    import app.verticals.bench_studio.agent_effort as ae

    monkeypatch.setattr(ae, "observed_agent_effort", no_effort)
    assert await w1_runner.harvest(session, ch) == 1  # v0 backfill receipt, version 1
    first = (await session.exec(select(BenchReceipt))).one()
    assert first.version == 1 and first.reason == "backfill_v0"
    session.add(ModelUsageEvent(task_id=task.id, harness="omp", model=DS, provider="vllm", session_id="s",
                                message_uuid=str(uuid.uuid4()), input_tokens=364_731, output_tokens=264_150,
                                cache_read_tokens=752_708, cache_write_tokens=0,
                                ts=datetime.now(timezone.utc), source_file="t"))
    await session.commit()
    assert await w1_runner.harvest(session, ch) == 1  # numbers changed -> version 2
    assert await w1_runner.harvest(session, ch) == 0  # nothing new -> no version
    rows = (await session.exec(select(BenchReceipt).order_by(BenchReceipt.version))).all()
    assert [r.version for r in rows] == [1, 2]
    assert rows[0].checksum == first.checksum  # version 1 untouched
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    b = d.blocks[0].contestants[0]
    assert b.summary.tokens_out.median == 264_150 and b.summary.tokens_cache_read == 752_708
    assert b.receipt_version == 2 and b.receipt.protocol_version == "v0"
    assert not any(r.rule == "R1" for r in d.gate.reasons)
    assert any(r.code == "method_v0" for r in d.gate.reasons)
