"""P2 live finding 13.10.2026 — omp's js browser tool (`eval`, shown as
"[js] Capturing screenshot of …") screenshots reach the chat.

Fixture ``fixtures/omp_js_screenshot/`` is VERBATIM from the live bridge
transcript of that test (both screenshot calls, seed + result) plus the
picture the bridge stored (an 800x600 WebP of the example.org/.com page —
both render identically, so both results carry the same sha256). The
picture rides only as ``details.images``; the transcript has no image
block by design (the "0 image blocks" seen live are correct).
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import chat_media

FIX = Path(__file__).parent / "fixtures" / "omp_js_screenshot"
SHA = "fb94467986aa47ebe8ab906f027f7e8b5055e6e1ca1c8c7c6f984b1a0d04d5bd"
AGENT = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_media.settings, "chat_media_root", tmp_path / "store")
    sdir = tmp_path / "sessions" / "--workspace--"
    (sdir / "media").mkdir(parents=True)
    shutil.copy(FIX / f"{SHA}.webp", sdir / "media" / f"{SHA}.webp")
    path = sdir / "2026-10-13T07-41-07_s-js.jsonl"
    shutil.copy(FIX / "session.jsonl", path)
    return path


def _omp():
    from app.services.transcript_adapters import adapter_for

    return adapter_for(SimpleNamespace(harness="omp"))


def test_fixture_is_the_live_shape():
    lines = [json.loads(l) for l in (FIX / "session.jsonl").read_text().splitlines()]
    results = [l["message"] for l in lines if (l.get("message") or {}).get("role") == "toolResult"]
    assert len(results) == 2
    for r in results:
        assert r["details"]["images"] == [{"sha256": SHA, "mimeType": "image/webp", "bytes": 24360}]
        assert r["details"]["title"].startswith("[js] Capturing screenshot")
    assert '"type": "image"' not in (FIX / "session.jsonl").read_text()


def test_both_js_screenshot_cards_carry_the_picture(session):
    from app.services.transcript_chat import read_history

    events = read_history(session, _omp(), media=chat_media.MediaContext(AGENT, session.parent))["events"]
    shots = [e for e in events if e["kind"] == "tool" and "[js] Capturing screenshot" in e["title"]]
    assert len(shots) == 2
    for ev in shots:
        assert [i["id"] for i in ev["images"]] == [SHA]
        assert ev["images"][0]["width"] == 800 and ev["images"][0]["height"] == 600
    path, mime = chat_media.open_variant(AGENT, SHA, "thumb")
    assert mime == "image/webp" and path.stat().st_size > 0


async def test_live_tailer_delivers_the_js_screenshot(session, monkeypatch):
    import asyncio

    import app.services.transcript_chat as tc
    from app.services import transcript_adapters

    omp = _omp()
    monkeypatch.setattr(transcript_adapters, "adapter_for", lambda agent: omp)
    monkeypatch.setattr(
        tc.chat_media, "context_for", lambda agent, p: chat_media.MediaContext(AGENT, p.parent)
    )
    published: list[dict] = []

    async def _fake(channel, event_type, data):
        published.append(json.loads(json.dumps(data)))

    monkeypatch.setattr(tc.sse, "broadcast", _fake)
    lines = session.read_text().splitlines()
    session.write_text(lines[0] + "\n")  # header only; the calls arrive live
    m = tc.ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    m.STATE_PROBE_INTERVAL_SECONDS = 0.02
    await m.acquire("a", session)
    try:
        with session.open("a") as fh:
            fh.write("\n".join(lines[1:]) + "\n")
        for _ in range(150):
            if sum(1 for d in published if d.get("kind") == "tool" and d.get("result")) >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        await m.release("a")
    settled = [d for d in published if d.get("kind") == "tool" and d.get("result")]
    assert len(settled) == 2
    assert all(d["images"][0]["id"] == SHA for d in settled)
