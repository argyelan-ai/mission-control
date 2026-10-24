"""Bench W1 tests API (W1.6-backend, W1.7, W1.12): create (re-resolved
picks, D4 cost confirmation, effort rules), detail + list with human
states, rerun with a larger budget, stop, the X-Kit (texts, gate, files,
download token)."""
from __future__ import annotations

import io
import os
import uuid
import zipfile

import pytest
from sqlmodel import select

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchChallenge, BenchEntry, BenchProbe, BenchReceipt  # noqa: E402
from app.verticals.bench_studio import w1_kit, w1_runner, w1_tests  # noqa: E402
from app.verticals.bench_studio import w1_schemas as s  # noqa: E402

from tests.test_bench_w1_fleet import (  # noqa: E402,F401
    DS, PAID, SPARK_EP, engines, factory, no_sleep, seed_fleet, world,
)

BASE = "/api/v1/bench"


def _pick(rt, harness="api", efforts=("default",)):
    return {"runtime_id": str(rt.id), "harness": harness, "efforts": list(efforts)}


async def _probe(session, fleet, levels=("off", "low", "medium", "high", "max")):
    session.add(BenchProbe(runtime_id=fleet["slot"].id, served_model_id=DS,
                           serving_since=fleet["slot"].serving_since, thinking_key="reasoning_effort",
                           effort_param="reasoning_effort", levels=list(levels), default_observed="on",
                           status="ok", raw={"default_level": "high"}))
    await session.commit()


async def _finished(session, fleet, monkeypatch=None, **world_kw):
    ch = await w1_tests.create_test(session, picks=[_pick(fleet["slot"])], preset="standard",
                                    trigger="runtimes", user_id=None)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    return ch


# ── create ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_freezes_a_v1_plan_with_one_entry_per_run(auth_client, session, engines):
    fleet = await seed_fleet(session)
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [_pick(fleet["slot"])], "trigger": "runtimes"})
    assert resp.status_code == 201, resp.text
    d = s.BenchTestDetail.model_validate(resp.json())
    assert d.state == "waiting" and d.section == "running" and d.protocol_version == "v1"
    assert d.allowed == ["stop"]
    ch = await session.get(BenchChallenge, d.id)
    assert ch.protocol_version == "v1" and ch.trigger == "runtimes" and ch.preset == "standard"
    assert len(ch.prompt_sha256) == 64 and ch.title == "Pagoda Garden"
    pc = ch.plan["contestants"][0]
    assert pc["served_model_id"] == DS and pc["device_name"] == "DGX Spark ×2" and pc["video"] is True
    assert pc["max_tokens"] == 16384 and pc["n_runs"] == 3
    entries = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).all()
    assert sorted(e.repeat_idx for e in entries) == [1, 2, 3]
    assert {(e.run_class, e.harness, e.source_kind) for e in entries} == {("A", "api", "api")}


@pytest.mark.asyncio
async def test_create_refuses_a_drifted_row_and_a_sleeping_box(auth_client, session, engines):
    fleet = await seed_fleet(session)
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [_pick(fleet["glm"])]})
    assert resp.status_code == 422 and resp.json()["detail"][0]["type"] == "bench_runtime_not_testable"
    engines["served"]["http://192.0.2.20:8000/v1"] = None
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [_pick(fleet["porsche"])]})
    assert resp.status_code in (409, 422)


@pytest.mark.asyncio
async def test_effort_rules(auth_client, session, engines):
    fleet = await seed_fleet(session)
    # unchecked probe -> only the default is offered
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [_pick(fleet["slot"], efforts=["max"])]})
    assert resp.status_code == 422 and resp.json()["detail"][0]["type"] == "bench_effort_not_offered"
    await _probe(session, fleet)
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [_pick(fleet["slot"], efforts=["off", "max"])]})
    assert resp.json()["detail"][0]["type"] == "bench_effort_ladder_only"
    resp = await auth_client.post(f"{BASE}/tests", json={
        "preset": "effort_ladder", "picks": [_pick(fleet["slot"], efforts=["off", "default", "max"])]})
    assert resp.status_code == 201
    d = resp.json()
    assert len(d["blocks"][0]["contestants"]) == 3
    # D5: the agent path never takes an effort
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [_pick(fleet["slot"], "omp", ["max"])]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_paid_run_needs_one_then_two_confirmations(auth_client, session, engines):
    fleet = await seed_fleet(session)
    body = {"picks": [_pick(fleet["paid"])], "trigger": "list"}
    resp = await auth_client.post(f"{BASE}/tests", json=body)
    assert resp.status_code == 409
    step1 = s.CostConfirmation.model_validate(resp.json()["detail"])
    assert step1.step == 1 and step1.usd > 1
    resp = await auth_client.post(f"{BASE}/tests", json={**body, "cost_confirmed_usd": step1.usd})
    step2 = s.CostConfirmation.model_validate(resp.json()["detail"])
    assert resp.status_code == 409 and step2.step == 2
    resp = await auth_client.post(f"{BASE}/tests", json={**body, "cost_confirmed_usd": 0.01,
                                                         "confirm_over_cap": True})
    assert resp.status_code == 409 and resp.json()["detail"]["step"] == 1  # the amount must match
    resp = await auth_client.post(f"{BASE}/tests", json={**body, "cost_confirmed_usd": step1.usd,
                                                         "confirm_over_cap": True})
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_subscription_run_needs_no_confirmation(auth_client, session, engines):
    fleet = await seed_fleet(session)
    resp = await auth_client.post(f"{BASE}/tests", json={"picks": [_pick(fleet["ollama"])]})
    assert resp.status_code == 201, resp.text


# ── detail + list (human states) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_v0_agent_challenge_reads_as_red_with_unknowns(auth_client, session, engines):
    fleet = await seed_fleet(session)
    ch = BenchChallenge(title="Shot Sequence", prompt_text="shot", status="review")
    session.add(ch)
    await session.flush()
    session.add(BenchEntry(challenge_id=ch.id, model_label="Alpha omp", source_kind="agent",
                           agent_id=fleet["omp_agent"].id, status="rendered", run_class="B",
                           metrics={"duration_ms": 5_551_000, "served_model_id": DS,
                                    "effort_observed": {"source": "omp_session", "level": "high"}}))
    await session.commit()
    resp = await auth_client.get(f"{BASE}/tests/{ch.id}")
    d = s.BenchTestDetail.model_validate(resp.json())
    assert d.protocol_version == "v0" and d.state == "done_missing" and d.section == "needs_look"
    assert d.state_reason.code == "numbers_missing" and d.state_reason.params["fields"] == ["tokens_out"]
    b = d.blocks[0].contestants[0]
    assert d.blocks[0].run_class == "B" and b.summary.decode_tok_s is None and b.summary.ttft_ms is None
    assert not b.summary.tokens_out.measured and b.summary.tokens_out.reason.code == "harvest_pending"
    assert b.receipt.protocol_version == "v0" and "harness_version" in b.receipt.unknown
    assert b.receipt_version is None  # computed, not stored: GET never writes
    codes = {(r.rule, r.code) for r in d.gate.reasons}
    assert ("R1", "headline_missing") in codes and ("amber", "method_v0") in codes
    assert ("amber", "single_run") in codes
    assert d.gate.status == "red" and "harvest" in d.allowed and "kit" not in d.allowed
    assert (await session.exec(select(BenchReceipt))).first() is None


@pytest.mark.asyncio
async def test_list_sections_counts_filters_and_progress(auth_client, session, engines, world):
    fleet = await seed_fleet(session)
    done = await _finished(session, fleet)
    running = await w1_tests.create_test(session, picks=[_pick(fleet["slot"])], preset="standard",
                                         trigger="quick", user_id=None)
    stopped = BenchChallenge(title="t", prompt_text="p", status="failed", error="stopped by operator")
    session.add(stopped)
    await session.commit()
    before = len((await session.exec(select(BenchReceipt))).all())
    resp = await auth_client.get(f"{BASE}/tests")
    lst = s.BenchTestList.model_validate(resp.json())
    by_id = {i.id: i for i in lst.items}
    assert by_id[running.id].section == "running" and by_id[running.id].progress == {"done": 0, "total": 3}
    assert by_id[done.id].section == "done" and by_id[done.id].gate_status in ("green", "amber")
    assert by_id[stopped.id].state == "aborted" and by_id[stopped.id].state_reason.code == "stopped_by_operator"
    assert lst.counts["running"] == 1 and lst.total == 3
    assert (await auth_client.get(f"{BASE}/tests?section=running")).json()["items"][0]["id"] == str(running.id)
    assert (await auth_client.get(f"{BASE}/tests?model=deepseek")).json()["counts"]["done"] == 1
    assert (await auth_client.get(f"{BASE}/tests?harness=omp")).json()["items"] == []
    assert (await auth_client.get(f"{BASE}/tests?run_class=A")).json()["total"] == 2
    assert len((await session.exec(select(BenchReceipt))).all()) == before  # GET never writes


@pytest.mark.asyncio
async def test_unknown_test_is_404(auth_client):
    assert (await auth_client.get(f"{BASE}/tests/{uuid.uuid4()}")).status_code == 404


# ── rerun / stop / harvest ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rerun_with_a_larger_budget_uses_the_suggestion(auth_client, session, engines, world):
    fleet = await seed_fleet(session)
    world.finish = ["stop", "length", "stop"]
    old = await _finished(session, fleet)
    resp = await auth_client.post(f"{BASE}/tests/{old.id}/rerun", json={"budget": "larger"})
    assert resp.status_code == 201, resp.text
    new = await session.get(BenchChallenge, uuid.UUID(resp.json()["id"]))
    assert new.rerun_of == old.id and new.preset == "rerun" and new.trigger == "rerun"
    assert new.plan["contestants"][0]["max_tokens"] == 32768
    assert new.prompt_sha256 == old.prompt_sha256
    same = await auth_client.post(f"{BASE}/tests/{old.id}/rerun", json={"budget": "same"})
    assert (await session.get(BenchChallenge, uuid.UUID(same.json()["id"]))).plan["contestants"][0]["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_stop_a_waiting_test(auth_client, session, engines):
    fleet = await seed_fleet(session)
    ch = await w1_tests.create_test(session, picks=[_pick(fleet["slot"])], preset="quick", trigger="list", user_id=None)
    resp = await auth_client.post(f"{BASE}/tests/{ch.id}/stop")
    d = s.BenchTestDetail.model_validate(resp.json())
    assert d.state == "aborted" and d.state_reason.code == "stopped_by_operator" and d.section == "done"
    assert (await auth_client.post(f"{BASE}/tests/{ch.id}/stop")).status_code == 409


# ── X-Kit ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kit_texts_are_english_bounded_and_from_green_numbers(auth_client, session, engines, world,
                                                                    monkeypatch):
    monkeypatch.setenv("MC_BENCH_FOOTER", "@example_handle")
    fleet = await seed_fleet(session)
    ch = await _finished(session, fleet)
    kit = s.XKit.model_validate((await auth_client.get(f"{BASE}/tests/{ch.id}/kit")).json())
    assert kit.downloadable and kit.footer == "@example_handle" and kit.texts.link is None
    assert all(len(b) <= 280 for b in kit.texts.settings) and len(kit.texts.alt_text) <= 1000
    blob = " ".join(kit.texts.settings)
    assert "same prompt (sha" in blob and "temp 0.7" in blob and "max_tokens 16384" in blob
    assert "engine: unknown (hand-made runtime)" in blob and "spec-decoding: unknown" in blob
    assert "GPU mode eco" in blob and "method v1" in blob and "(comparison only)" in blob
    assert DS in kit.texts.alt_text and "6.6k" in kit.texts.alt_text
    assert kit.texts.facts and all(DS in f or not f.startswith("Deep") for f in kit.texts.facts)
    files = {f.name: f for f in kit.files}
    assert files["card.png"].ready and files["card.png"].width == 1080 and files["card.png"].height == 1350
    assert files["video.mp4"].ready and files["video.mp4"].width == 1920
    # never a bare $ on a local run, never an agent name or a host address
    assert "$" not in " ".join(kit.texts.facts)
    everything = blob + kit.texts.alt_text + " ".join(kit.texts.facts)
    assert "Alpha" not in everything and "203.0.113" not in everything and "spark-a" not in everything


@pytest.mark.asyncio
async def test_kit_download_token_and_files(auth_client, session, engines, world, client):
    fleet = await seed_fleet(session)
    ch = await _finished(session, fleet)
    tok = s.DownloadToken.model_validate((await auth_client.post(f"{BASE}/tests/{ch.id}/kit/download-token")).json())
    assert tok.expires_in == 1800
    other = await w1_tests.create_test(session, picks=[_pick(fleet["slot"])], preset="quick", trigger="list",
                                       user_id=None)
    # the token opens only this test's files
    assert (await client.get(f"{BASE}/tests/{other.id}/kit/files/card.png?token={tok.token}")).status_code == 403
    assert (await client.get(f"{BASE}/tests/{ch.id}/kit/files/card.png?token=nope")).status_code == 403
    # and is never a session: the plan route refuses it
    assert (await client.get(f"{BASE}/plan", headers={"Authorization": f"Bearer {tok.token}"})).status_code in (401, 403)
    resp = await client.get(f"{BASE}/tests/{ch.id}/kit/files/card.png?token={tok.token}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;") and "deepseek-v4.1-flash-1017-pagoda-garden-" in cd and cd.endswith('.png"')
    resp = await client.get(f"{BASE}/tests/{ch.id}/kit/files/kit.zip?token={tok.token}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert {"settings.txt", "alt-text.txt", "facts.txt", "thread.txt"} <= set(names)
    assert any(n.endswith(".mp4") for n in names) and any(n.endswith(".png") for n in names)


@pytest.mark.asyncio
async def test_red_gate_kit_is_preview_only_and_files_409(auth_client, session, engines, client):
    await seed_fleet(session)
    ch = BenchChallenge(title="Shot", prompt_text="shot", status="review")
    session.add(ch)
    await session.flush()
    session.add(BenchEntry(challenge_id=ch.id, model_label="x", source_kind="agent", status="rendered",
                           run_class="B", metrics={"duration_ms": 1000}))
    await session.commit()
    kit = s.XKit.model_validate((await auth_client.get(f"{BASE}/tests/{ch.id}/kit")).json())
    assert not kit.downloadable and kit.blocked_reason.code == "gate_red" and kit.texts.facts == []
    token = w1_kit.create_download_token("u", str(ch.id))
    assert (await client.get(f"{BASE}/tests/{ch.id}/kit/files/video.mp4?token={token}")).status_code == 409


@pytest.mark.asyncio
async def test_privacy_hit_turns_the_gate_red(session, engines, world):
    """R4: a device name with an address or the operator's name never
    reaches an artefact."""
    from app.models.user import User

    fleet = await seed_fleet(session)
    session.add(User(email="op@example.org", name="Marek Example", role="admin", is_active=True))
    fleet["spark1"].display_name = "Marek box at 203.0.113.54 1"
    session.add(fleet["spark1"])
    await session.commit()
    ch = await _finished(session, fleet)
    d = await w1_tests.test_detail(session, ch)
    assert d["gate"]["status"] == "red"
    r4 = next(r for r in d["gate"]["reasons"] if r["rule"] == "R4")
    assert {"ip", "private_word"} <= set(r4["params"]["kinds"])
    assert "203.0.113" not in str(r4)  # the reason names the kind, never the value


@pytest.mark.asyncio
async def test_a_dollar_on_a_local_run_is_red(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _finished(session, fleet)
    ch.plan = {**ch.plan, "contestants": [{**ch.plan["contestants"][0],
                                           "money": {"cost_kind": "local"},
                                           "price": {"input_per_mtok": 1, "output_per_mtok": 1}}]}
    session.add(ch)
    await session.commit()
    d = await w1_tests.test_detail(session, ch)
    a = d["blocks"][0]["contestants"][0]
    assert a["summary"]["cost"].get("usd") is None  # spend is a comparison price only
    a["summary"]["cost"]["usd"] = 0.43  # what the old outro printed
    from app.verticals.bench_studio import w1_receipt

    gate = w1_receipt.evaluate_gate(protocol_version="v1", prompt_sha=d["prompt"]["sha256"], blocks=d["blocks"],
                                    receipts=[(a, a["receipt"], a["receipt_checksum"])], public_texts=[],
                                    private_words=[], truncation=None, finished=True)
    assert any(r["rule"] == "R3" for r in gate["reasons"]) and gate["status"] == "red"


@pytest.mark.asyncio
async def test_name_mismatch_is_red(session, engines, world):
    fleet = await seed_fleet(session)
    ch = await _finished(session, fleet)
    ch.plan = {**ch.plan, "contestants": [{**ch.plan["contestants"][0], "display_name": "Qwen-3.5"}]}
    session.add(ch)
    await session.commit()
    d = await w1_tests.test_detail(session, ch)
    assert any(r["rule"] == "R2" for r in d["gate"]["reasons"])


@pytest.mark.asyncio
async def test_auto_test_route(auth_client, session, engines):
    fleet = await seed_fleet(session)
    resp = await auth_client.post(f"{BASE}/auto-tests", json={"host_id": str(uuid.uuid4()), "recipe_slug": "x"})
    assert resp.status_code == 404
    resp = await auth_client.post(f"{BASE}/auto-tests", json={"host_id": str(fleet["spark1"].id),
                                                               "recipe_slug": "glm53-flash"})
    out = s.AutoTestResponse.model_validate(resp.json())
    assert resp.status_code == 201 and out.state == "waiting" and out.not_before
    resp2 = await auth_client.post(f"{BASE}/auto-tests", json={"host_id": str(fleet["spark1"].id),
                                                                "recipe_slug": "ds"})
    assert resp2.json()["superseded_test_id"] == str(out.test_id)


def test_footer_comes_from_config_never_the_repo():
    handle = "mark" + "argyelan"  # the operator's handle lives in his .env only
    for path in (w1_kit.__file__,):
        assert handle not in open(path, encoding="utf-8").read()
    os.environ.pop("MC_BENCH_FOOTER", None)
    assert w1_kit.footer() == ""


@pytest.mark.asyncio
async def test_private_words_skip_public_display_names_and_harness_words(session):
    from app.models.agent import Agent
    from app.models.host import Host
    from app.verticals.bench_studio import w1_receipt

    session.add_all([Host(slug="spark", display_name="DGX Spark 1", kind="ssh", ssh_host="10.1.2.3"),
                     Host(slug="box-7f3", display_name="RTX 5090", kind="ssh"),
                     Agent(name="Grok"), Agent(name="Alpha")])
    await session.commit()
    words = await w1_receipt.private_words(session)
    assert "spark" not in words and "Grok" not in words
    assert {"box-7f3", "10.1.2.3", "Alpha"} <= set(words)
