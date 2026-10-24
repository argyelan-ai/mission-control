"""Bench video branding copy from data (concept W0.5, 2026-10-22).

- The video carries the run's DATE (in the operator's zone), plus
  "episode N" only once the run is published — no `RUN 0xx` counter (it
  counted rows, so it shifted after every delete and never matched the
  published episode number: video "RUN 035" vs site "#4").
- The chip says what MC actually knows about a direct-API run: it ran on the
  local OpenAI-compatible endpoint, with no agent in between. No hard-coded
  engine or hardware ("VLLM · SPARK" was wrong for SGLang/EXL3 recipes).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.config import settings
from app.models.bench import BenchChallenge, BenchEntry
from app.verticals.bench_studio import orchestrator

ORCHESTRATOR_SRC = Path(orchestrator.__file__)


async def _challenge(session, *, created_at: datetime) -> BenchChallenge:
    ch = BenchChallenge(title="Pagoda Garden", prompt_text="Build a pagoda.", mode="single",
                        created_at=created_at)
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    return ch


def _spark_entry(ch: BenchChallenge) -> BenchEntry:
    return BenchEntry(challenge_id=ch.id, model_label="DeepSeek-V4.1-Flash-1017",
                      source_kind="spark", status="rendered", video_path="/sd/a.mp4",
                      metrics={"duration_ms": 65791, "tokens_in": 117, "tokens_out": 6607})


@pytest.mark.asyncio
async def test_branding_carries_the_run_date_not_a_run_counter(session, monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(settings, "usage_timezone", "UTC")
    # Two older challenges: the old counter would print "003".
    for _ in range(2):
        await _challenge(session, created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    ch = await _challenge(session, created_at=datetime(2026, 10, 21, 18, 5, tzinfo=timezone.utc))

    payload = await orchestrator._build_branding_payload(session, ch, [_spark_entry(ch)])

    assert payload["date_label"] == "2026-10-21"


@pytest.mark.asyncio
async def test_legacy_run_label_is_still_sent_for_an_old_renderer(session, monkeypatch, tmp_path):
    """Deploy-order guard (review of #944): a renderer image built before
    W0.5 declares ``run_label: str`` as REQUIRED — without it every compose
    is a 422. The image build can now fail by design (fonts unreachable), so
    a new backend may talk to an old renderer for a while. The backend keeps
    sending the old counter for one release; the new renderer ignores it and
    the video shows only date_label."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    for _ in range(2):
        await _challenge(session, created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    ch = await _challenge(session, created_at=datetime(2026, 10, 21, 18, 5, tzinfo=timezone.utc))

    payload = await orchestrator._build_branding_payload(session, ch, [_spark_entry(ch)])

    assert payload["run_label"] == "003"
    # every field the pre-W0.5 renderer BrandingSpec requires
    for key in ("title", "run_label", "prompt_line", "models", "outro_rows"):
        assert key in payload
    assert {"name", "time", "size"} <= set(payload["outro_rows"][0])


@pytest.mark.asyncio
async def test_run_date_is_the_operators_calendar_day(session, monkeypatch, tmp_path):
    """23:30 UTC on the 21st is already the 22nd in Zurich — the video shows
    the day the operator ran it (USAGE_TIMEZONE / TZ, same zone as Insights)."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(settings, "usage_timezone", "Europe/Zurich")
    ch = await _challenge(session, created_at=datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc))

    payload = await orchestrator._build_branding_payload(session, ch, [_spark_entry(ch)])

    assert payload["date_label"] == "2026-10-22"


@pytest.mark.asyncio
async def test_unknown_zone_falls_back_to_utc(session, monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(settings, "usage_timezone", "Not/AZone")
    ch = await _challenge(session, created_at=datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc))

    payload = await orchestrator._build_branding_payload(session, ch, [_spark_entry(ch)])

    assert payload["date_label"] == "2026-10-21"


@pytest.mark.asyncio
async def test_published_run_gets_its_episode_number(session, monkeypatch, tmp_path):
    """The catalog publisher records a publish in
    <challenge dir>/publish/state.json (absent = never published or
    unpublished) — that episode number, never a count of rows."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(settings, "usage_timezone", "UTC")
    ch = await _challenge(session, created_at=datetime(2026, 10, 21, 18, 5, tzinfo=timezone.utc))
    state = orchestrator.challenge_dir(ch.id) / "publish" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"episode_id": "ep-x", "episode_no": 4}), encoding="utf-8")

    payload = await orchestrator._build_branding_payload(session, ch, [_spark_entry(ch)])

    assert payload["date_label"] == "episode 4 · 2026-10-21"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{not json", json.dumps({"episode_no": "4"}),
                                     json.dumps({"episode_no": 0}), json.dumps(["x"])])
async def test_broken_or_odd_publish_state_shows_only_the_date(session, monkeypatch, tmp_path,
                                                              content):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(settings, "usage_timezone", "UTC")
    ch = await _challenge(session, created_at=datetime(2026, 10, 21, 18, 5, tzinfo=timezone.utc))
    state = orchestrator.challenge_dir(ch.id) / "publish" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(content, encoding="utf-8")

    payload = await orchestrator._build_branding_payload(session, ch, [_spark_entry(ch)])

    assert payload["date_label"] == "2026-10-21"


@pytest.mark.asyncio
async def test_direct_api_chip_names_no_engine_or_hardware(session, monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch = await _challenge(session, created_at=datetime(2026, 10, 21, tzinfo=timezone.utc))

    payload = await orchestrator._build_branding_payload(session, ch, [_spark_entry(ch)])

    tag = payload["models"][0]["tag"]
    assert tag == "LOCAL · DIRECT API"
    for guess in ("VLLM", "SGLANG", "SPARK", "GB10"):
        assert guess not in tag.upper()


def test_no_hard_coded_engine_or_hardware_tag_left_in_the_orchestrator():
    """Acceptance grep of concept W0.5: no "VLLM · SPARK" left."""
    src = ORCHESTRATOR_SRC.read_text(encoding="utf-8")
    assert "VLLM · SPARK" not in src
    assert "VLLM \\u00b7 SPARK" not in src
