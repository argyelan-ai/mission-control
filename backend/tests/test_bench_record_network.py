"""bench_studio W0.9 — what the recorder reports about the network ends up on
the entry (`metrics`, §7.2 names), so later receipts can say "network blocked,
N attempts" only for recordings that really had the block (critic 32 N2).
"""
import uuid
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry
from app.verticals.bench_studio import orchestrator
from tests.conftest import test_engine

BLOCKED_REPORT = {
    "video_path": "/sd/a.mp4",
    "screenshot_path": "/sd/a.png",
    "duration_s": 10,
    "bytes": 123,
    "network_blocked": True,
    "network_attempts": 2,
    "blocked_requests": ["https://unpkg.com/three@0.128.0/build/three.module.js"],
    "libraries_inlined": ["three.js r160"],
    "html_bytes": 708689,
}


async def _seed(session, **entry_over):
    ch = BenchChallenge(title="T", prompt_text="p", mode="single", status="generating")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    spec = {"model_label": "A", "source_kind": "spark", "status": "generated",
            "artifact_path": "/a/index.html", "metrics": {"duration_ms": 4000}}
    spec.update(entry_over)
    e = BenchEntry(challenge_id=ch.id, **spec)
    session.add(e)
    await session.commit()
    await session.refresh(e)
    return ch, e


async def _fresh_entry(entry_id: uuid.UUID) -> BenchEntry:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(BenchEntry, entry_id)


@pytest.mark.asyncio
async def test_render_stores_the_network_report_in_metrics(session, monkeypatch):
    ch, e = await _seed(session)
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock(return_value=BLOCKED_REPORT))
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))

    await orchestrator.maybe_advance(session, ch.id)

    m = (await _fresh_entry(e.id)).metrics
    assert m["network_blocked"] is True
    assert m["network_attempts"] == 2
    assert m["libraries_inlined"] == ["three.js r160"]
    assert m["html_bytes"] == 708689
    assert m["recorded_at"]  # ISO timestamp of this recording
    assert m["duration_ms"] == 4000  # existing metrics kept
    # The URL sample is for logs/debugging, not for the record (§7.2 names only).
    assert "blocked_requests" not in m


@pytest.mark.asyncio
async def test_old_sidecar_without_block_writes_no_network_claim(session, monkeypatch):
    """A sidecar that predates the block returns no network fields: the entry
    must not claim "blocked" — absence stays absence."""
    ch, e = await _seed(session)
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock(return_value={
        "video_path": "/sd/a.mp4", "screenshot_path": "/sd/a.png",
    }))
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))

    await orchestrator.maybe_advance(session, ch.id)

    m = (await _fresh_entry(e.id)).metrics
    assert "network_blocked" not in m
    assert "network_attempts" not in m
    assert m["recorded_at"]


@pytest.mark.asyncio
async def test_rerecording_replaces_an_older_network_report(session, monkeypatch):
    monkeypatch.setattr("app.database.engine", test_engine)
    ch, e = await _seed(
        session, status="rendered", video_path="/sd/old.mp4",
        metrics={"duration_ms": 4000, "network_blocked": True, "network_attempts": 9,
                 "libraries_inlined": ["GSAP 3.12.5"], "recorded_at": "2026-10-01T00:00:00+00:00"},
    )
    ch.status = "review"
    session.add(ch)
    await session.commit()
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock(return_value={
        **BLOCKED_REPORT, "network_attempts": 0, "libraries_inlined": [],
    }))
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))

    await orchestrator.rerender_entry(e.id, ch.id)

    m = (await _fresh_entry(e.id)).metrics
    assert m["network_attempts"] == 0
    assert m["libraries_inlined"] == []
    assert m["recorded_at"] != "2026-10-01T00:00:00+00:00"
    assert m["duration_ms"] == 4000
