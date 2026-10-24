"""Bench W1 — fixes from the PR #982 reviews (code, design, operator).

Each test states the behaviour the review asked for; every one of them was
red before its fix (sabotage probes noted in the PR body)."""
from __future__ import annotations

import json
import time
import uuid
import zipfile
from datetime import timedelta

import httpx
import pytest
from sqlmodel import select

pytest.importorskip("app.verticals.bench_studio")

from app.models.approval import Approval  # noqa: E402
from app.models.bench import BenchChallenge, BenchEntry  # noqa: E402
from app.models.model_usage import ModelUsageEvent  # noqa: E402
from app.models.runtime import Runtime  # noqa: E402
from app.verticals.bench_studio import w1_common as c  # noqa: E402
from app.verticals.bench_studio import w1_kit, w1_plan, w1_runner, w1_tests  # noqa: E402
from app.verticals.bench_studio import w1_schemas as s  # noqa: E402

from tests.test_bench_w1_fleet import (  # noqa: E402,F401
    DS, PAGE, SPARK_EP, engines, factory, no_sleep, seed_fleet, world,
)

BASE = "/api/v1/bench"


async def _create(session, fleet, preset="standard", rt=None, **kw):
    picks = [{"runtime_id": (rt or fleet["slot"]).id, "harness": "api", "efforts": ["default"]}]
    return await w1_tests.create_test(session, picks=picks, preset=preset, trigger="runtimes", user_id=None, **kw)


def _sse(model, parts, finish, n):
    lines = [{"model": model, "choices": [{"delta": {"reasoning_content": "think"}}]}]
    for p in parts:
        lines.append({"model": model, "choices": [{"delta": {"content": p}}]})
    lines.append({"model": model, "choices": [{"delta": {}, "finish_reason": finish}]})
    lines.append({"model": model, "choices": [], "usage": {"prompt_tokens": 300, "completion_tokens": n}})
    text = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"
    return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})


def _script(world, plan):
    """plan[i] = what the i-th streamed chat request gets: "ok", "cut" (thinks
    until max_tokens, no content), "500"."""
    orig = world.handler
    calls = {"n": 0}

    def handler(request):
        body = json.loads(request.content or b"{}")
        if body.get("stream") and "/chat/completions" in str(request.url):
            i = calls["n"]
            calls["n"] += 1
            what = plan[min(i, len(plan) - 1)]
            if what == "cut":
                return _sse(body["model"], [], "length", body.get("max_tokens") or 16384)
            if what == "500":
                return httpx.Response(500, json={"error": "x"})
            return _sse(body["model"], [PAGE], "stop", 6600)
        return orig(request)

    world.handler = handler
    return calls


# ── MUST: a run that thinks until max_tokens and writes no page ──────────


@pytest.mark.asyncio
async def test_truncation_without_any_content_is_warned_and_not_retried(session, engines, world):
    fleet = await seed_fleet(session)
    calls = _script(world, ["ok", "cut", "ok"])
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert calls["n"] == 3, "a cut run is not retried with the same budget"
    await session.refresh(ch)
    entries = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id)
                                  .order_by(BenchEntry.repeat_idx))).all()
    cut = entries[1]
    assert cut.status == "failed" and cut.metrics["error_code"] == "no_html_truncated"
    assert cut.metrics["finish_reason"] == "length" and cut.metrics["truncated"] is True
    assert cut.metrics["max_tokens"] == c.BASE_MAX_TOKENS and cut.attempt_no == 1
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    assert d.truncation is not None and d.truncation.runs_cut == 1 and d.truncation.runs_without_page == 1
    assert d.truncation.suggested_max_tokens > c.BASE_MAX_TOKENS and not d.truncation.video_run_cut
    assert "rerun_larger" in d.allowed


@pytest.mark.asyncio
async def test_all_runs_cut_still_offers_the_larger_budget(session, engines, world):
    """Every run thought until the limit: the test is aborted, but the page
    still warns and offers the larger budget instead of "Nochmal" only."""
    fleet = await seed_fleet(session)
    _script(world, ["cut"])
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    assert d.state == "aborted"
    assert d.truncation is not None and d.truncation.runs_cut == 3 and d.truncation.runs_without_page == 3
    assert "rerun_larger" in d.allowed


@pytest.mark.asyncio
async def test_video_run_cut_is_flagged_for_the_private_warning(session, engines, world):
    fleet = await seed_fleet(session)
    world.finish = ["length", "stop", "stop"]
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    assert d.truncation.video_run_cut is True and d.truncation.runs_without_page == 0


# ── MUST: the card counts the runs behind the median ─────────────────────


@pytest.mark.asyncio
async def test_card_run_count_matches_the_median(session, engines, world):
    fleet = await seed_fleet(session)
    _script(world, ["ok", "500", "500", "ok"])  # run 2 fails both attempts
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch)
    a = d["blocks"][0]["contestants"][0]
    assert a["summary"]["time"]["n"] == 2
    card = w1_kit.card_payload(d)
    subs = {n["sub"] for n in card["contestants"][0]["numbers"] if n.get("sub")}
    assert subs == {"median of 2 of 3"}
    assert card["foot"] == "median of 2 runs · same prompt · 1 run failed"
    alt = w1_kit.alt_text(d)
    assert "of 3 runs" not in alt.replace("2 of 3", "") and "1 run failed" in alt
    head = w1_kit.settings_blocks(d)[0]
    assert "n=3 (1 failed)" in head
    assert "2 runs" in " ".join(w1_kit.facts(d))


def test_card_says_full_chips_and_the_video_pane_the_compact_ones():
    r = {"served_model_id": DS, "display_name": DS, "locality": "local", "device_name": "DGX Spark ×2",
         "harness": "api", "effort": {"requested": "default", "default_level": "high"}, "run_class": "A",
         "n_runs": 3, "summary": {"time": {"measured": True, "median": 66000, "min": 62000, "max": 71000, "n": 3},
                                  "tokens_out": {"measured": True, "median": 6600, "min": 6400, "max": 6900, "n": 3},
                                  "cost": {"cost_kind": "local"}}, "_video": True}
    cloud = {**r, "locality": "cloud", "device_name": None, "provider": "Ollama Cloud"}
    detail = {"prompt": {"title": "Pagoda Garden"}, "created_at": None, "protocol_version": "v1",
              "blocks": [{"run_class": "A", "contestants": [r, cloud]}], "gate": {}}
    card = w1_kit.card_payload(detail)
    assert card["contestants"][0]["chips"] == "local · DGX Spark ×2 · raw API · effort high (default)"
    assert card["contestants"][1]["chips"] == "cloud · Ollama Cloud · raw API · effort high (default)"
    frame = w1_kit.frame_payload(detail)
    assert "DGX Spark" not in frame["panes"][0]["chips"]  # the 840 px pane stays compact


# ── SHOULD: a restart during the last attempt ends the run ───────────────


@pytest.mark.asyncio
async def test_restart_during_second_attempt_does_not_hang(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet, preset="quick")
    e = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).first()
    e.status = "generating"
    e.attempt_no = 2
    ch.status = "generating"
    session.add_all([e, ch])
    await session.commit()
    await w1_runner.heal_after_restart(factory)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    e2 = await session.get(BenchEntry, e.id)
    await session.refresh(e2)
    assert e2.status == "failed" and e2.metrics["error_code"] == "backend_restart"
    assert e2.attempt_no == w1_runner.MAX_ATTEMPTS and ch.status == "failed"


# ── SHOULD: D4 fail-closed ───────────────────────────────────────────────


async def _unpriced(session):
    rt = Runtime(slug="router-x", display_name="R", runtime_type="openai_compatible",
                 endpoint="https://openrouter.ai/api/v1", model_identifier="vendor/new-model-9",
                 api_key_secret_id=uuid.uuid4(), ui_order=12)
    session.add(rt)
    await session.commit()
    return rt


@pytest.mark.asyncio
async def test_unpriced_cloud_api_asks_before_spending(auth_client, session, engines):
    await seed_fleet(session)
    rt = await _unpriced(session)
    body = {"picks": [{"runtime_id": str(rt.id), "harness": "api", "efforts": ["default"]}]}
    resp = await auth_client.post(f"{BASE}/tests", json=body)
    assert resp.status_code == 409, resp.text
    ask = s.CostConfirmation.model_validate(resp.json()["detail"])
    assert ask.price_unknown is True and ask.usd is None and ask.step == 1
    resp = await auth_client.post(f"{BASE}/tests", json={**body, "confirm_unknown_cost": True})
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_unpriced_rerun_asks_too(auth_client, session, engines):
    await seed_fleet(session)
    rt = await _unpriced(session)
    body = {"picks": [{"runtime_id": str(rt.id), "harness": "api", "efforts": ["default"]}],
            "confirm_unknown_cost": True}
    old = (await auth_client.post(f"{BASE}/tests", json=body)).json()
    resp = await auth_client.post(f"{BASE}/tests/{old['id']}/rerun", json={"budget": "larger"})
    assert resp.status_code == 409 and resp.json()["detail"]["price_unknown"] is True
    resp = await auth_client.post(f"{BASE}/tests/{old['id']}/rerun",
                                  json={"budget": "larger", "confirm_unknown_cost": True})
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_paid_run_is_never_retried_and_its_failed_tokens_are_counted(session, engines, world):
    fleet = await seed_fleet(session)
    calls = _script(world, ["cut", "ok"])
    ask = None
    try:
        await _create(session, fleet, preset="quick", rt=fleet["paid"])
    except Exception as exc:  # noqa: BLE001
        ask = exc.detail
    ch = await _create(session, fleet, preset="quick", rt=fleet["paid"], cost_confirmed_usd=ask["usd"],
                       confirm_over_cap=True)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert calls["n"] == 1, "a paid run is attempted once (no spend past the confirmed amount)"
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch)
    cost = d["blocks"][0]["contestants"][0]["summary"]["cost"]
    assert cost["cost_kind"] == "api_price" and cost["usd"] > 0  # the cut run was paid all the same


@pytest.mark.asyncio
async def test_paid_run_after_a_restart_is_not_sent_again(session, engines, world):
    fleet = await seed_fleet(session)
    calls = _script(world, ["ok"])
    ch = await _create(session, fleet, preset="quick", rt=fleet["paid"], cost_confirmed_usd=99.0,
                       confirm_over_cap=True)
    e = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).first()
    e.status = "generating"
    ch.status = "generating"
    session.add_all([e, ch])
    await session.commit()
    await w1_runner.heal_after_restart(factory)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert calls["n"] == 0
    await session.refresh(e)
    assert e.status == "failed" and e.metrics["error_code"] == "backend_restart"


@pytest.mark.asyncio
async def test_paid_run_that_fails_is_not_sent_a_second_time(session, engines, world):
    fleet = await seed_fleet(session)
    calls = _script(world, ["500", "ok"])
    ch = await _create(session, fleet, preset="quick", rt=fleet["paid"], cost_confirmed_usd=99.0,
                       confirm_over_cap=True)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    assert calls["n"] == 1
    e = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).first()
    await session.refresh(e)
    assert e.status == "failed" and e.attempt_no == 1


def test_cost_kinds_rank_the_same_on_both_sides():
    """sheetModel.ts ranks api_price > unknown > subscription > local — so
    does the server (an unknown price is never hidden behind "Abo")."""
    m = w1_plan.total_money([{"cost_kind": "subscription"}, {"cost_kind": "unknown"}])
    assert m["cost_kind"] == "unknown"


# ── SHOULD: usage rows for direct runs ───────────────────────────────────


@pytest.mark.asyncio
async def test_every_direct_attempt_writes_a_usage_row(session, engines, world):
    fleet = await seed_fleet(session)
    _script(world, ["ok", "cut", "ok"])
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    rows = (await session.exec(select(ModelUsageEvent))).all()
    assert len(rows) == 3
    assert {r.harness for r in rows} == {"vanilla"} and {r.model for r in rows} == {DS}
    assert all(r.cost_usd is None for r in rows)  # local: no $ (R3)
    assert all(r.session_id == f"bench-{ch.id}" for r in rows)


# ── SHOULD: kit.zip never goes stale ─────────────────────────────────────


@pytest.mark.asyncio
async def test_kit_zip_is_rebuilt_when_the_files_behind_it_change(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch, with_actions=False)
    card = w1_runner.card_path(ch.id, d["gate"]["checksum"])
    card.unlink()  # the zip is built while there is no card (old renderer)
    first = w1_kit.build_zip(ch, d)
    assert not any(n.endswith(".png") for n in zipfile.ZipFile(first).namelist())
    card.write_bytes(b"\x89PNG new card")  # renderer rebuilt, same numbers = same checksum
    second = w1_kit.build_zip(ch, d)
    assert any(n.endswith(".png") for n in zipfile.ZipFile(second).namelist())
    assert not first.exists()


@pytest.mark.asyncio
async def test_compose_drops_every_old_zip(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    d = await w1_tests.test_detail(session, ch, with_actions=False)
    z = w1_kit.build_zip(ch, d)
    await w1_runner.compose_artefacts(factory, ch.id)
    assert not z.exists()


# ── SHOULD: no polling of a sleeping box ─────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_box_is_not_asked_for_its_load(session, engines, monkeypatch):
    await seed_fleet(session)
    asked: list[str] = []

    async def in_use(ep):
        asked.append(ep)
        return False

    monkeypatch.setattr(w1_plan, "_in_use", in_use)
    await w1_plan.load_views(session)
    assert asked == [SPARK_EP]  # the second box did not answer /models


@pytest.mark.asyncio
async def test_probe_warming_runs_at_most_once_a_minute(monkeypatch):
    n = {"warm": 0}

    async def warm(_f=None):
        n["warm"] += 1

    async def nothing(_f=None):
        return None

    monkeypatch.setattr(w1_runner, "warm_probes", warm)
    monkeypatch.setattr(w1_runner, "resolve_auto_tests", nothing)
    monkeypatch.setattr(w1_runner, "next_test_id", nothing)
    monkeypatch.setattr(w1_runner, "_last_warm", None)
    await w1_runner.tick()
    await w1_runner.tick()
    assert n["warm"] == 1
    monkeypatch.setattr(w1_runner, "_last_warm", time.monotonic() - w1_runner.WARM_EVERY_S - 1)
    await w1_runner.tick()
    assert n["warm"] == 2


# ── Operator: planned auto test, Eingang ─────────────────────────────────


@pytest.mark.asyncio
async def test_planned_auto_test_shows_on_the_runtimes_card(session, engines):
    fleet = await seed_fleet(session)
    res = await w1_tests.create_auto_test(session, host_id=fleet["spark1"].id, recipe_slug="ds", user_id=None)
    rows = {r["runtime_id"]: r for r in await w1_plan.runtime_summary(session)}
    row = s.RuntimeTestSummary.model_validate(rows[fleet["slot"].id])
    assert row.planned_test is not None and row.planned_test.id == res["test_id"]
    assert row.planned_test.not_before is not None
    assert rows[fleet["ollama"].id].get("planned_test") is None


@pytest.mark.asyncio
async def test_opening_the_result_page_clears_its_inbox_item(auth_client, session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    item = (await session.exec(select(Approval).where(Approval.action_type == "bench_result"))).first()
    assert item.status == "pending"
    assert item.payload["models"] == [DS] and item.payload["gate_status"] in ("green", "amber")
    resp = await auth_client.post(f"{BASE}/tests/{ch.id}/seen")
    assert resp.status_code == 200 and resp.json() == {"resolved": 1}
    await session.refresh(item)
    assert item.status == "approved" and item.resolved_at is not None
    assert (await auth_client.post(f"{BASE}/tests/{ch.id}/seen")).json() == {"resolved": 0}


@pytest.mark.asyncio
async def test_seen_while_running_resolves_nothing(auth_client, session, engines):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet)
    resp = await auth_client.post(f"{BASE}/tests/{ch.id}/seen")
    assert resp.json() == {"resolved": 0}


@pytest.mark.asyncio
async def test_aborted_test_says_stopped_in_the_inbox(session, engines, world):
    fleet = await seed_fleet(session)
    world.fail_runs = 99
    ch = await _create(session, fleet, preset="quick")
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    item = (await session.exec(select(Approval).where(Approval.action_type == "bench_result"))).first()
    assert item.description.startswith("Test stopped") and item.payload["state"] == "aborted"
    assert item.payload["models"] == [DS] and item.payload["state_reason"] == "all_runs_failed"


# ── NICE ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_same_pick_twice_is_refused(auth_client, session, engines):
    fleet = await seed_fleet(session)
    pick = {"runtime_id": str(fleet["slot"].id), "harness": "api", "efforts": ["default"]}
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [pick, pick]})
    assert resp.status_code == 422 and resp.json()["detail"][0]["type"] == "bench_pick_duplicate"


@pytest.mark.asyncio
async def test_agent_time_limit_survives_a_restart(session, engines, world, monkeypatch):
    """The 120 min count from the first dispatch, not from the restart."""
    fleet = await seed_fleet(session)
    ch = await w1_tests.create_test(
        session, picks=[{"runtime_id": fleet["slot"].id, "harness": "omp", "efforts": ["default"]}],
        preset="standard", trigger="runtimes", user_id=None)
    e = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).first()
    e.status = "generating"
    long_ago = c.now_utc() - timedelta(minutes=c.AGENT_TIME_LIMIT_MIN + 5)
    e.metrics = {**(e.metrics or {}), "agent_started_at": long_ago.isoformat()}
    session.add(e)
    await session.commit()

    from app.models.task import Task

    task = Task(title="bench", board_id=fleet["board"].id, status="in_progress")
    session.add(task)
    await session.commit()
    e.task_id = task.id
    session.add(e)
    await session.commit()
    ok = await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)
    assert ok is False
    await session.refresh(e)
    assert e.metrics["error_code"] == "time_limit"


@pytest.mark.asyncio
async def test_kit_files_follow_the_kit_rule_for_aborted_tests(auth_client, session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _create(session, fleet)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    ch = await session.get(BenchChallenge, ch.id)
    ch.status = "failed"
    ch.error = "stopped_by_operator"
    session.add(ch)
    await session.commit()
    kit = (await auth_client.get(f"{BASE}/tests/{ch.id}/kit")).json()
    assert kit["downloadable"] is False
    token = (await auth_client.post(f"{BASE}/tests/{ch.id}/kit/download-token")).json()["token"]
    resp = await auth_client.get(f"{BASE}/tests/{ch.id}/kit/files/video.mp4", params={"token": token})
    assert resp.status_code == 409


def test_settings_text_names_the_default_level_like_the_card():
    """K10 on the public artefacts: card "effort high (default)", settings
    "server default = high" — never two names for one level."""
    r = {"run_class": "A", "effort": {"requested": "default", "default_level": "high"}}
    assert w1_kit._effort_disclosure(r, {}).startswith("effort: server default = high")
    assert w1_kit._effort_word(r) == "effort high (default)"
    plain = {"run_class": "A", "effort": {"requested": "default"}}
    assert w1_kit._effort_disclosure(plain, {}).startswith("effort: server default (")
