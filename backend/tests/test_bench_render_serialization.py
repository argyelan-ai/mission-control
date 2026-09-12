"""bench_studio — media-step serialisation + readable render errors.

Root cause (2026-08-18, three failed challenges): every challenge runs its
own asyncio task, so three challenges started minutes apart overlapped in
the render step. Each /record spawns Chromium (2880x1620) plus an ffmpeg
libx264 encoder inside the mc-playwright container; two of those at once
exhaust the Docker VM and the kernel OOM-killer takes ffmpeg out
(rc=-9). The failures surfaced as an empty "render failed: " because
raise_for_status() drops the response body that names the real cause.
"""
import asyncio

import httpx
import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchChallenge, BenchEntry
from app.verticals.bench_studio import orchestrator


@pytest.mark.asyncio
async def test_record_entry_error_keeps_the_sidecar_detail(monkeypatch):
    """A 502 from /record must carry mc-playwright's own explanation.

    Without this the operator sees "render failed: " with an empty tail and
    has to reproduce the run by hand to learn it was an OOM-killed ffmpeg.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502, json={"detail": "ffmpeg (pipe encode) failed (rc=-9): "}
        )

    transport = httpx.MockTransport(handler)
    _patch_client(monkeypatch, transport)

    challenge = BenchChallenge(title="T", prompt_text="p", mode="single")
    entry = BenchEntry(
        challenge_id=challenge.id,
        model_label="A",
        source_kind="spark",
        artifact_path="/shared-deliverables/bench-x/A/index.html",
    )

    with pytest.raises(Exception) as excinfo:
        await orchestrator.record_entry(entry, challenge)

    assert "rc=-9" in str(excinfo.value)
    assert "502" in str(excinfo.value)


@pytest.mark.asyncio
async def test_concurrent_recordings_never_overlap(monkeypatch):
    """Two challenges rendering at once must queue, not run side by side.

    One Chromium + ffmpeg pair fits in the Docker VM; two do not.
    """
    peak = 0
    live = 0

    async def fake_post(*args, **kwargs):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        return httpx.Response(
            200,
            json={"video_path": "/shared-deliverables/x/clip.mp4",
                  "screenshot_path": "/shared-deliverables/x/shot.png"},
            request=httpx.Request("POST", "http://mc-playwright:8790/record"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    challenge = BenchChallenge(title="T", prompt_text="p", mode="single")
    entries = [
        BenchEntry(
            challenge_id=challenge.id,
            model_label=f"M{i}",
            source_kind="spark",
            artifact_path=f"/shared-deliverables/bench-x/M{i}/index.html",
        )
        for i in range(4)
    ]

    await asyncio.gather(*(orchestrator.record_entry(e, challenge) for e in entries))

    assert peak == 1, f"{peak} recordings ran at once — the Docker VM fits one"


def _patch_client(monkeypatch, transport):
    """Routes orchestrator's httpx.AsyncClient through a MockTransport."""
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


@pytest.mark.asyncio
async def test_compose_does_not_run_beside_a_recording(session, monkeypatch):
    """The compose tail spawns its own Chromium for the branding cards —
    it must wait for a running recording instead of doubling the load."""
    peak = 0
    live = 0

    async def fake_post(self, url, *args, **kwargs):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        body = (
            {"output_path": "/shared-deliverables/x/grid.mp4"}
            if url.endswith("/compose")
            else {"video_path": "/shared-deliverables/x/clip.mp4",
                  "screenshot_path": "/shared-deliverables/x/shot.png"}
        )
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(
        orchestrator, "_build_branding_payload", _async_return({"models": []})
    )

    challenge = BenchChallenge(title="T", prompt_text="p", mode="single")
    session.add(challenge)
    await session.commit()
    await session.refresh(challenge)
    entry = BenchEntry(
        challenge_id=challenge.id,
        model_label="A",
        source_kind="spark",
        artifact_path="/shared-deliverables/bench-x/A/index.html",
        video_path="/shared-deliverables/bench-x/A/recording.mp4",
        status="rendered",
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)

    await asyncio.gather(
        orchestrator.record_entry(entry, challenge),
        orchestrator.compose_challenge(session, challenge, [entry]),
    )

    assert peak == 1, f"{peak} media steps ran at once — the Docker VM fits one"


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn
