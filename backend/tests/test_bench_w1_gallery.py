"""Bench W1 gallery + per-run words (25.10.2026): the list says whether a
test has a picture (``poster``), ``GET /tests/{id}/poster`` serves a small
WebP of the video run's page screenshot — only from below the
shared-deliverables root — and every run carries the tokens it thought
(``tokens_reasoning``) for the per-run breakdown of a stopped test."""
from __future__ import annotations

import io
import os

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


def _noisy_png(path, w=1440, h=810, seed=1):
    """A picture with real content (no colour covers 90 %)."""
    import random

    rnd = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(0, h, 9):
        for x in range(0, w, 9):
            c = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for dy in range(9):
                for dx in range(9):
                    if x + dx < w and y + dy < h:
                        px[x + dx, y + dy] = c
    im.save(path, "PNG")
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
    inside = _noisy_png(tmp_path / "shared" / "bench-1" / "run" / "screenshot.png", 1920, 1080)
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

    shot = _noisy_png(tmp_path / f"bench-{ch.id}" / "run-1" / "screenshot.png", 1280, 720)
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


def test_poster_is_cached_next_to_the_screenshot_and_refreshed_by_a_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path)
    shot = _noisy_png(tmp_path / "bench-1" / "run" / "screenshot.png")
    first = w1_kit.poster_webp(str(shot))
    cached = list(shot.parent.glob(".screenshot.poster-*.webp"))
    assert first is not None and len(cached) == 1 and cached[0].read_bytes() == first

    # A hit does not decode the screenshot again.
    def boom(*a, **k):
        raise AssertionError("screenshot decoded on a cache hit")

    import PIL.Image as PImage

    real_open, real_blank = PImage.open, w1_kit.poster_is_blank
    monkeypatch.setattr(PImage, "open", boom)
    monkeypatch.setattr(w1_kit, "poster_is_blank", boom)
    assert w1_kit.poster_webp(str(shot)) == first
    monkeypatch.setattr(PImage, "open", real_open)
    monkeypatch.setattr(w1_kit, "poster_is_blank", real_blank)

    # Re-recorded: new bytes + new mtime → a new poster, the old cache file is gone.
    _noisy_png(shot, seed=2)
    st = shot.stat()
    os.utime(shot, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second = w1_kit.poster_webp(str(shot))
    assert second is not None and second != first
    left = list(shot.parent.glob(".screenshot.poster-*"))
    assert len(left) == 1 and left[0].read_bytes() == second


def test_a_blank_page_has_no_poster_and_the_list_learns_it(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path)
    sky = _png(tmp_path / "bench-1" / "run" / "screenshot.png", 1440, 810, (135, 206, 235))
    # a small label on a sky-blue canvas is still blank (≥ 90 % one colour)
    im = Image.open(sky)
    for x in range(20, 200):
        for y in range(20, 40):
            im.putpixel((x, y), (40, 40, 40))
    im.save(sky)
    assert w1_kit.poster_is_blank(Image.open(sky))
    assert not w1_kit.poster_known_blank(str(sky))
    assert w1_kit.poster_webp(str(sky)) is None
    assert w1_kit.poster_known_blank(str(sky))
    # a real picture is not blank
    assert not w1_kit.poster_is_blank(Image.open(_noisy_png(tmp_path / "bench-2" / "r" / "screenshot.png")))


def test_the_cache_never_lives_outside_the_root(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path / "shared")
    outside = _noisy_png(tmp_path / "elsewhere" / "screenshot.png")
    assert w1_kit.poster_webp(str(outside)) is None
    assert list(outside.parent.glob(".screenshot.poster-*")) == []


@pytest.mark.asyncio
async def test_list_says_no_poster_for_a_known_blank_page(auth_client, session, engines, tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path)
    fleet = await seed_fleet(session)
    ch, entries = await _test_with_entries(session, fleet)
    shot = _png(tmp_path / f"bench-{ch.id}" / "run-1" / "screenshot.png", 1440, 810, (0, 0, 0))
    entries[0].screenshot_path = str(shot)
    session.add(entries[0])
    await session.commit()
    item = lambda r: next(i for i in r.json()["items"] if i["id"] == str(ch.id))  # noqa: E731
    assert item(await auth_client.get(f"{BASE}/tests"))["poster"] is True   # not known yet
    assert (await auth_client.get(f"{BASE}/tests/{ch.id}/poster")).status_code == 404
    assert item(await auth_client.get(f"{BASE}/tests"))["poster"] is False  # now known
