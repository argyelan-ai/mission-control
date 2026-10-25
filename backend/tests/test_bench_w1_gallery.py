"""Bench W1 gallery + per-run words (25.10.2026): the list says whether a
test has a picture (``poster``), ``GET /tests/{id}/poster`` serves a small
WebP of the video run's page screenshot — only from below the
shared-deliverables root — and every run carries the tokens it thought
(``tokens_reasoning``) for the per-run breakdown of a stopped test."""
from __future__ import annotations

import io

import pytest
from sqlmodel import select

pytest.importorskip("app.verticals.bench_studio")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from app.models.bench import BenchEntry  # noqa: E402
from app.verticals.bench_studio import orchestrator as orch  # noqa: E402
from app.verticals.bench_studio import w1_kit, w1_tests  # noqa: E402

from tests.test_bench_w1_fleet import engines, seed_fleet  # noqa: E402,F401

BASE = "/api/v1/bench"


def _png(path, w=1920, h=1080, color=(30, 60, 90)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path, "PNG")
    return path


async def _test_with_entries(session, fleet):
    ch = await w1_tests.create_test(
        session, picks=[{"runtime_id": str(fleet["slot"].id), "harness": "api", "efforts": ["default"]}],
        preset="standard", trigger="runtimes", user_id=None,
    )
    entries = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).all()
    return ch, sorted(entries, key=lambda e: e.repeat_idx)


def test_poster_entry_prefers_the_video_run():
    a = BenchEntry(challenge_id=None, repeat_idx=2, screenshot_path="/x/2.png")  # type: ignore[arg-type]
    b = BenchEntry(challenge_id=None, repeat_idx=1, screenshot_path="/x/1.png")  # type: ignore[arg-type]
    c = BenchEntry(challenge_id=None, repeat_idx=3, screenshot_path=None)  # type: ignore[arg-type]
    assert w1_tests.poster_entry([a, b, c]) is b
    assert w1_tests.poster_entry([a, c]) is a
    assert w1_tests.poster_entry([c]) is None


def test_poster_webp_scales_down_and_stays_inside_the_root(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path / "shared")
    inside = _png(tmp_path / "shared" / "bench-1" / "run" / "screenshot.png")
    data = w1_kit.poster_webp(str(inside))
    assert data is not None
    im = Image.open(io.BytesIO(data))
    assert im.format == "WEBP" and im.width == w1_kit.POSTER_WIDTH and im.height == 450
    # outside the root, a traversal, a missing file: never read
    outside = _png(tmp_path / "elsewhere" / "secret.png")
    assert w1_kit.poster_webp(str(outside)) is None
    assert w1_kit.poster_webp(str(tmp_path / "shared" / ".." / "elsewhere" / "secret.png")) is None
    assert w1_kit.poster_webp(str(tmp_path / "shared" / "missing.png")) is None
    assert w1_kit.poster_webp(None) is None


@pytest.mark.asyncio
async def test_list_says_poster_and_the_route_serves_it(auth_client, session, engines, tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path)
    fleet = await seed_fleet(session)
    ch, entries = await _test_with_entries(session, fleet)

    resp = await auth_client.get(f"{BASE}/tests")
    item = next(i for i in resp.json()["items"] if i["id"] == str(ch.id))
    assert item["poster"] is False
    assert (await auth_client.get(f"{BASE}/tests/{ch.id}/poster")).status_code == 404

    shot = _png(tmp_path / f"bench-{ch.id}" / "run-1" / "screenshot.png", 1280, 720)
    entries[0].screenshot_path = str(shot)
    session.add(entries[0])
    await session.commit()

    resp = await auth_client.get(f"{BASE}/tests")
    item = next(i for i in resp.json()["items"] if i["id"] == str(ch.id))
    assert item["poster"] is True
    resp = await auth_client.get(f"{BASE}/tests/{ch.id}/poster")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert "private" in resp.headers["cache-control"]
    assert Image.open(io.BytesIO(resp.content)).width == w1_kit.POSTER_WIDTH


@pytest.mark.asyncio
async def test_poster_needs_a_user(client, session, engines):
    fleet = await seed_fleet(session)
    ch, _ = await _test_with_entries(session, fleet)
    resp = await client.get(f"{BASE}/tests/{ch.id}/poster")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_each_run_carries_the_tokens_it_thought(auth_client, session, engines):
    fleet = await seed_fleet(session)
    ch, entries = await _test_with_entries(session, fleet)
    entries[0].status = "failed"
    entries[0].metrics = {"tokens_out": 16384, "tokens_reasoning": 16384, "finish_reason": "length",
                          "truncated": True, "error_code": "no_html_truncated", "phase": "done"}
    session.add(entries[0])
    await session.commit()
    resp = await auth_client.get(f"{BASE}/tests/{ch.id}")
    runs = resp.json()["blocks"][0]["contestants"][0]["runs"]
    run1 = next(r for r in runs if r["repeat_idx"] == 1)
    assert run1["tokens_reasoning"] == 16384 and run1["tokens_out"] == 16384
    assert run1["error"]["code"] == "no_html_truncated"
    assert next(r for r in runs if r["repeat_idx"] == 2)["tokens_reasoning"] is None
