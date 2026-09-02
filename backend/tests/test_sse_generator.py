"""Der SSE-Generator darf nur alle ``ping_interval`` Sekunden einen Ping
schicken — nicht in einer heissen Schleife.

Live-Befund 02.09.2026: 57 000 Pings pro Sekunde und 1,4 MB/s pro offenem
Tab, seit dem ersten Release. ``get_message`` ohne ``timeout`` kehrt sofort
mit None zurueck, ``wait_for`` lief darum nie in seinen Timeout.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.services import sse


class _FakePubSub:
    """Verhaelt sich wie redis-py: ``timeout=0`` = sofort None, sonst warten."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.timeouts: list[float | None] = []
        self.subscribed: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)
    async def unsubscribe(self, *channels: str) -> None: ...
    async def aclose(self) -> None: ...

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float | None = 0.0):
        self.timeouts.append(timeout)
        if timeout == 0.0 or timeout is None:
            return None if self.queue.empty() else self._frame(self.queue.get_nowait())
        try:
            item = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._frame(item)

    @staticmethod
    def _frame(item):
        # redis-py liefert {"channel": ..., "data": ...}; Tests duerfen
        # einen nackten String (Kanal "c") oder ein (Kanal, Daten)-Paar legen.
        if isinstance(item, tuple):
            return {"channel": item[0], "data": item[1]}
        return {"channel": "c", "data": item}


class _FakeRedis:
    def __init__(self, ps: _FakePubSub) -> None:
        self._ps = ps

    def pubsub(self) -> _FakePubSub:
        return self._ps

    async def aclose(self) -> None: ...


@pytest.fixture
def fake_pubsub(monkeypatch):
    ps = _FakePubSub()
    monkeypatch.setattr(sse.aioredis, "from_url", lambda *a, **k: _FakeRedis(ps))
    return ps


async def _collect(gen, seconds: float) -> list[dict]:
    out: list[dict] = []
    deadline = time.monotonic() + seconds

    async def _drain() -> None:
        async for item in gen:
            out.append(item)
            if time.monotonic() >= deadline:
                break

    try:
        await asyncio.wait_for(_drain(), timeout=seconds + 0.5)
    except asyncio.TimeoutError:
        pass
    await gen.aclose()
    return out


@pytest.mark.asyncio
async def test_idle_stream_pings_once_per_interval_not_in_a_hot_loop(fake_pubsub):
    gen = sse._sse_generator(["c"], ping_interval=0.1)
    items = await _collect(gen, 0.35)
    pings = [i for i in items if i["event"] == "ping"]
    # 0,35 s bei 0,1 s Takt → 3 Pings (±1). Die heisse Schleife lieferte Tausende.
    assert 2 <= len(pings) <= 5, len(pings)


@pytest.mark.asyncio
async def test_a_published_message_is_forwarded_promptly(fake_pubsub):
    gen = sse._sse_generator(["c"], ping_interval=5)
    payload = json.dumps({"id": "x", "event": "chat_event", "data": {"kind": "preview"}})

    async def _publish_soon() -> None:
        await asyncio.sleep(0.05)
        await fake_pubsub.queue.put(payload)

    asyncio.create_task(_publish_soon())
    started = time.monotonic()
    first = await asyncio.wait_for(gen.__anext__(), timeout=2)
    await gen.aclose()
    assert first["event"] == "chat_event"
    assert json.loads(first["data"]) == {"kind": "preview"}
    # Sabotage-Gegenprobe: Nachrichten warten NICHT auf den Ping-Takt.
    assert time.monotonic() - started < 1.0


# --- Kanal-bewusster Uebersetzer (Gruppen-Vorschau) -------------------------
#
# Der Gruppenraum haengt sich zusaetzlich an die Chat-Kanaele seiner
# Mitglieder. Der Generator muss darum wissen, AUS WELCHEM Kanal ein Frame
# kam, und ihn ueber ``transform(channel, payload)`` umschreiben oder
# verwerfen koennen.


@pytest.mark.asyncio
async def test_transform_rewrites_or_drops_frames_per_channel(fake_pubsub):
    seen: list[tuple[str, dict]] = []

    def transform(channel: str, payload: dict) -> dict | None:
        seen.append((channel, payload))
        if channel == "drop-me":
            return None
        return {**payload, "event": f"rewritten:{channel}"}

    gen = sse._sse_generator(["keep", "drop-me"], ping_interval=5, transform=transform)
    await fake_pubsub.queue.put(
        ("drop-me", json.dumps({"id": "1", "event": "chat_event", "data": {"kind": "preview"}}))
    )
    await fake_pubsub.queue.put(
        ("keep", json.dumps({"id": "2", "event": "group.turn_started", "data": {"x": 1}}))
    )
    first = await asyncio.wait_for(gen.__anext__(), timeout=2)
    await gen.aclose()
    assert first["event"] == "rewritten:keep"
    assert first["id"] == "2"
    assert json.loads(first["data"]) == {"x": 1}
    assert [c for c, _ in seen] == ["drop-me", "keep"]
