"""Bench data integrity (round-1 user tests, package P3).

- Every entry writes into its OWN folder — two rows with the same label
  (same model, effort Off vs Max) used to share one folder, and the second
  run silently overwrote the first one's page and video.
- Create refuses true duplicate labels (same label AND same effort). Labels
  that only differ by effort stay as typed since round-2 Q2 — the effort is
  its own field (test_bench_effort_field.py).
- The video carries the effort: frame tag and outro row (old renderer form
  here; the effort-field form in test_bench_effort_field.py).
- The requested effort is on the entry as soon as the run starts (the UI
  said "sent: nothing" while the max run was already on the wire).
- GET /bench/effort-levels answers from the cache only — no DB write, no
  request to the box. Probing is an explicit POST and refused while the
  Spark is busy.

Vertical test: skipped entirely when the vertical directory is stripped.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlalchemy import text
from sqlmodel import select

from app.models.bench import BenchChallenge, BenchEntry, BenchProbe
from app.services import reasoning_params as rp
from app.verticals.bench_studio import effort_probe, orchestrator


LABEL = "DeepSeek-V4.1-Flash-1017"


async def _challenge(session, *efforts: str, label: str = LABEL):
    ch = BenchChallenge(title="Pagoda", prompt_text="make a page")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = []
    for effort in efforts:
        e = BenchEntry(
            challenge_id=ch.id, model_label=label, source_kind="spark",
            spark_model="deepseek-x", metrics={"effort_requested": effort},
        )
        session.add(e)
        entries.append(e)
    await session.commit()
    for e in entries:
        await session.refresh(e)
    return ch, entries


def _probe_row(levels, key=rp.REASONING_EFFORT, status="ok", model="deepseek-x"):
    return BenchProbe(
        served_model_id=model, status=status, thinking_key=key,
        effort_param=key if key == rp.REASONING_EFFORT else None, levels=levels,
        default_observed="on",
    )


# ── one folder per entry ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_label_entries_write_separate_folders(session, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    gen = AsyncMock(side_effect=[
        ("<html>off page</html>", {"duration_ms": 10}),
        ("<html>max page</html>", {"duration_ms": 20}),
    ])
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, (off, mx) = await _challenge(session, "default", "default")

    await orchestrator.generate_spark_entry(session, off, ch.prompt_text)
    await orchestrator.generate_spark_entry(session, mx, ch.prompt_text)
    await session.refresh(off)
    await session.refresh(mx)

    assert off.status == mx.status == "generated"
    assert off.artifact_path != mx.artifact_path
    # The first page is still on disk — not overwritten by the second run.
    assert open(off.artifact_path).read() == "<html>off page</html>"
    assert open(mx.artifact_path).read() == "<html>max page</html>"
    # Still readable for a human: the label stays in the folder name.
    assert "DeepSeek-V4-1-Flash-1017" in off.artifact_path


@pytest.mark.asyncio
async def test_record_writes_each_entry_into_its_own_folder(session, monkeypatch):
    posted: list[dict] = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"video_path": "/sd/x.mp4"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            posted.append(json)
            return FakeResp()

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)
    ch, (a, b) = await _challenge(session, "off", "max")
    a.artifact_path = str(orchestrator.entry_dir(a) / "index.html")
    b.artifact_path = str(orchestrator.entry_dir(b) / "index.html")

    await orchestrator.record_entry(a, ch)
    await orchestrator.record_entry(b, ch)

    assert posted[0]["output_dir"] != posted[1]["output_dir"]
    assert posted[0]["output_dir"] == str(orchestrator.entry_dir(a))


# ── create: duplicate labels ───────────────────────────────────────────────


@pytest.fixture
def _no_background(monkeypatch):
    monkeypatch.setattr(orchestrator, "start_challenge", AsyncMock())


def _spec(effort=None, label=LABEL, model="deepseek-x"):
    spec = {"label": label, "source_kind": "spark", "spark_model": model}
    if effort is not None:
        spec["effort"] = effort
    return spec


@pytest.mark.asyncio
async def test_create_refuses_true_duplicate_labels(auth_client, monkeypatch, _no_background):
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off", "max"])))
    resp = await auth_client.post("/api/v1/bench/challenges", json={
        "title": "Twice", "prompt_text": "one page",
        "models": [_spec("max"), _spec("max", label=f" {LABEL.lower()} ")],
    })
    assert resp.status_code == 422
    assert "label" in resp.json()["detail"].lower()


# ── video: effort in tag and outro ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_branding_names_the_effort_in_tag_and_outro(session):
    ch, (off, mx) = await _challenge(session, "off", "max")
    payload = await orchestrator._build_branding_payload(session, ch, [off, mx])

    tags = [m["tag"] for m in payload["models"]]
    assert tags == ["LOCAL · DIRECT API · EFFORT OFF", "LOCAL · DIRECT API · EFFORT MAX"]
    names = [r["name"] for r in payload["outro_rows"]]
    assert names[0] != names[1]
    assert names == [f"{LABEL} · effort off", f"{LABEL} · effort max"]


@pytest.mark.asyncio
async def test_branding_leaves_default_effort_and_operator_tag_alone(session):
    ch, (plain,) = await _challenge(session, "default")
    payload = await orchestrator._build_branding_payload(session, ch, [plain])
    assert payload["models"][0]["tag"] == "LOCAL · DIRECT API"
    assert payload["outro_rows"][0]["name"] == LABEL

    ch2, (tagged,) = await _challenge(session, "max")
    tagged.display_tag = "MY TAG"
    payload = await orchestrator._build_branding_payload(session, ch2, [tagged])
    assert payload["models"][0]["tag"] == "MY TAG"


@pytest.mark.asyncio
async def test_outro_does_not_repeat_an_effort_already_in_the_label(session):
    ch, (mx,) = await _challenge(session, "max", label=f"{LABEL} · Max")
    payload = await orchestrator._build_branding_payload(session, ch, [mx])
    assert payload["outro_rows"][0]["name"] == f"{LABEL} · Max"


@pytest.mark.asyncio
async def test_outro_keeps_the_effort_when_the_label_only_ends_in_those_letters(session):
    """Review NICE (PR #955): "Qwen-Python" ends in "on" — the effort On
    must still be named, only a separate trailing word counts."""
    ch, (on,) = await _challenge(session, "on", label="Qwen-Python")
    payload = await orchestrator._build_branding_payload(session, ch, [on])
    assert payload["outro_rows"][0]["name"] == "Qwen-Python · effort on"


# ── effort stored at run start ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_effort_sent_is_stored_before_the_request_goes_out(session, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off", "max"])))
    monkeypatch.setattr(effort_probe, "spark_runtime", AsyncMock(return_value=None))
    ch, (entry,) = await _challenge(session, "max")
    seen: dict = {}

    async def gen(prompt, model, effort_fields=None, max_tokens=0):
        # What a parallel GET would read from the DB mid-run.
        row = (await session.exec(
            text("select metrics from bench_entries where id = :id").bindparams(id=entry.id.hex)
        )).first()
        seen["metrics"] = row[0]
        return "<html>x</html>", {"duration_ms": 10}

    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)

    import json as _json

    mid_run = seen["metrics"]
    mid_run = _json.loads(mid_run) if isinstance(mid_run, str) else mid_run
    assert mid_run["effort_sent"] == {"key": "reasoning_effort", "value": "max"}
    assert mid_run["effort_requested"] == "max"


# ── effort-levels: GET reads, POST probes ──────────────────────────────────


@pytest.mark.asyncio
async def test_effort_levels_get_never_probes_or_writes(auth_client, session, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(effort_probe, "run_probe", run)
    monkeypatch.setattr(effort_probe, "spark_runtime", AsyncMock(return_value=None))

    resp = await auth_client.get("/api/v1/bench/effort-levels?model=never-probed")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unchecked"
    assert resp.json()["levels"] == []
    run.assert_not_awaited()
    rows = (await session.exec(select(BenchProbe).where(BenchProbe.served_model_id == "never-probed"))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_effort_levels_get_returns_the_cached_probe(auth_client, session, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(effort_probe, "run_probe", run)
    monkeypatch.setattr(effort_probe, "spark_runtime", AsyncMock(return_value=None))
    row = _probe_row(["off", "max"], model="cached-x")
    row.probed_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()

    resp = await auth_client.get("/api/v1/bench/effort-levels?model=cached-x")
    assert resp.json()["status"] == "ok"
    assert resp.json()["levels"] == ["off", "max"]
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_effort_levels_check_probes_on_request(auth_client, monkeypatch):
    row = _probe_row(["off", "max"])
    probe = AsyncMock(return_value=row)
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    resp = await auth_client.post("/api/v1/bench/effort-levels/check", json={"model": "deepseek-x"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["levels"] == ["off", "max"]
    probe.assert_awaited_once()
    assert probe.await_args.kwargs == {"refresh": True}


@pytest.mark.asyncio
async def test_effort_levels_check_refused_while_a_spark_run_is_going(auth_client, session, monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    ch, (entry,) = await _challenge(session, "default")
    entry.status = "generating"
    session.add(entry)
    await session.commit()

    resp = await auth_client.post("/api/v1/bench/effort-levels/check", json={"model": "deepseek-x"})
    assert resp.status_code == 409
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_effort_levels_check_refused_while_an_agent_works_on_the_spark(
    auth_client, session, monkeypatch, make_agent
):
    from app.models.runtime import Runtime

    probe = AsyncMock()
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    rt = Runtime(slug=f"spark-{uuid.uuid4().hex[:6]}", display_name="Spark", runtime_type="ssh_process",
                 endpoint="http://192.0.2.10:8000/v1", enabled=True)
    session.add(rt)
    await session.commit()
    await make_agent(name="Worker", runtime_id=rt.id, current_task_id=uuid.uuid4())

    resp = await auth_client.post("/api/v1/bench/effort-levels/check", json={"model": "deepseek-x"})
    assert resp.status_code == 409
    assert "Worker" in resp.json()["detail"]
    probe.assert_not_awaited()


# ── a direct-API run cut off by a restart never blocks the Spark for good ──
# Review SHOULD-2 (PR #955): the sweep only healed agent entries. A backend
# restart during a direct-API run (e.g. a deploy) left the entry at
# `generating`, and POST /effort-levels/check answered 409 forever.


@pytest.mark.asyncio
async def test_spark_run_is_marked_in_flight_only_while_it_runs(session, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, (entry,) = await _challenge(session, "default")
    seen: dict = {}

    async def gen(prompt, model, effort_fields=None, max_tokens=0):
        seen["in_flight"] = orchestrator.spark_run_in_flight(entry.id)
        return "<html>x</html>", {"duration_ms": 10}

    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)

    assert seen["in_flight"] is True
    assert orchestrator.spark_run_in_flight(entry.id) is False


@pytest.mark.asyncio
async def test_sweep_heals_a_direct_api_run_cut_off_by_a_restart(session, monkeypatch):
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock())
    ch, (entry,) = await _challenge(session, "max")
    entry.status = "generating"
    session.add(entry)
    await session.commit()

    await orchestrator.reconcile_challenge(session, ch, [entry])
    await session.refresh(entry)
    await session.refresh(ch)

    assert entry.status == "failed"
    assert "restart" in entry.error
    assert ch.status == "failed"  # only entry failed -> the challenge moves on


@pytest.mark.asyncio
async def test_sweep_keeps_a_direct_api_run_that_is_still_going(session, monkeypatch):
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock())
    ch, (entry,) = await _challenge(session, "max")
    entry.status = "generating"
    session.add(entry)
    await session.commit()

    monkeypatch.setattr(orchestrator, "_SPARK_RUNS_IN_FLIGHT", {entry.id})
    await orchestrator.reconcile_challenge(session, ch, [entry])
    await session.refresh(entry)

    assert entry.status == "generating"
    assert entry.error is None


@pytest.mark.asyncio
async def test_effort_check_names_the_challenge_that_is_running(auth_client, session, monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    ch, (entry,) = await _challenge(session, "default")
    entry.status = "generating"
    session.add(entry)
    await session.commit()

    resp = await auth_client.post("/api/v1/bench/effort-levels/check", json={"model": "deepseek-x"})
    assert resp.status_code == 409
    assert "Pagoda" in resp.json()["detail"]
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_effort_check_ignores_a_left_over_entry_of_a_finished_challenge(
    auth_client, session, monkeypatch
):
    """An entry still at `generating` under a challenge that is no longer
    generating (stopped, failed, archived after review) is no run."""
    probe = AsyncMock(return_value=_probe_row(["off", "max"]))
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    ch, (entry,) = await _challenge(session, "default")
    entry.status = "generating"
    ch.status = "failed"
    session.add(entry)
    session.add(ch)
    await session.commit()

    resp = await auth_client.post("/api/v1/bench/effort-levels/check", json={"model": "deepseek-x"})
    assert resp.status_code == 200, resp.text
    probe.assert_awaited_once()
