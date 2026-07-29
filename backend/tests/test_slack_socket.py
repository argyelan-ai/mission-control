"""Slack Socket Mode — the transport MC opens because Slack cannot call in.

What is asserted here is the state machine, not Slack: every envelope is
acknowledged (and acknowledged BEFORE the handler runs, so a throwing handler
cannot cause a redelivery storm), a disconnect is normal and reconnects, an
unreachable Slack backs off instead of spinning, exactly one process holds the
socket, and "off" means silent.

No network, ever: the websocket is a fake and the token is never read.
"""
from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis
import pytest

import app.redis_client
from app.config import settings
from app.services.slack_client import SlackSocketUrl
from app.services.slack_socket import LOCK_KEY, SlackSocketModeService


# ── Fakes ─────────────────────────────────────────────────────────────────


class FakeSocket:
    """Stands in for a `websockets` client connection.

    Yields the frames it was handed, records everything sent back, and can be
    told to end (Slack closing) or to fail on send (a broken ack).
    """

    def __init__(self, frames: list[str], *, send_fails: bool = False):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.send_fails = send_fails
        self.closed = False

    async def send(self, raw: str) -> None:
        if self.send_fails:
            raise ConnectionResetError("socket gone")
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


def envelope(kind: str, envelope_id: str = "env-1", **payload) -> str:
    body: dict = {"type": kind, "envelope_id": envelope_id}
    body.update(payload)
    return json.dumps(body)


def event_envelope(text: str = "hallo", envelope_id: str = "env-1") -> str:
    return envelope(
        "events_api",
        envelope_id,
        payload={
            "type": "event_callback",
            "event": {"type": "message", "user": "U1", "text": text, "channel": "C1"},
        },
    )


def make_service(frames, *, handler=None, url="wss://slack.test/link"):
    """A service wired to one fake connection. Returns (service, socket)."""
    socket = FakeSocket(frames)

    async def open_url():
        return SlackSocketUrl(url=url)

    def connect(_url):
        return socket

    seen: list[dict] = []

    async def default_handler(event):
        seen.append(event)

    service = SlackSocketModeService(
        open_url=open_url, connect=connect, handler=handler or default_handler
    )
    service.seen = seen  # type: ignore[attr-defined]
    return service, socket


@pytest.fixture
async def redis_lock():
    """A real (in-memory) Redis, so the lock logic is exercised, not mocked."""
    server = fakeredis.aioredis.FakeServer()
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    original = app.redis_client._redis
    app.redis_client._redis = redis
    yield redis
    app.redis_client._redis = original
    await redis.aclose()


@pytest.fixture(autouse=True)
def slack_on(monkeypatch):
    monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "slack_default_channel", "C1", raising=False)
    monkeypatch.setattr(settings, "chat_channels", "", raising=False)


# ── 1. Every envelope is acknowledged ─────────────────────────────────────


@pytest.mark.asyncio
async def test_an_event_is_acknowledged(redis_lock):
    """Unacked = redelivered, forever. This is the single most expensive
    mistake in Socket Mode, so it gets the first test."""
    service, socket = make_service([event_envelope()])

    await service._connect_once()

    assert socket.sent == [{"envelope_id": "env-1"}]
    assert service.envelopes_acked == 1


@pytest.mark.asyncio
async def test_every_envelope_gets_its_own_ack(redis_lock):
    service, socket = make_service(
        [event_envelope(envelope_id=f"env-{i}") for i in range(4)]
    )

    await service._connect_once()

    assert [s["envelope_id"] for s in socket.sent] == [f"env-{i}" for i in range(4)]


@pytest.mark.asyncio
async def test_the_ack_goes_out_before_the_handler_runs(redis_lock):
    """Order matters: Slack redelivers after ~3s, so a slow handler must not
    hold the ack hostage."""
    order: list[str] = []
    socket = FakeSocket([event_envelope()])
    original_send = socket.send

    async def recording_send(raw):
        order.append("ack")
        await original_send(raw)

    socket.send = recording_send  # type: ignore[assignment]

    async def handler(_event):
        order.append("handler")

    async def open_url():
        return SlackSocketUrl(url="wss://slack.test/link")

    service = SlackSocketModeService(
        open_url=open_url, connect=lambda _u: socket, handler=handler
    )
    await service._connect_once()

    assert order == ["ack", "handler"]


@pytest.mark.asyncio
async def test_a_throwing_handler_still_leaves_the_message_acknowledged(redis_lock):
    """Otherwise one poisonous message becomes an infinite redelivery loop."""

    async def handler(_event):
        raise RuntimeError("boom")

    service, socket = make_service([event_envelope()], handler=handler)

    await service._connect_once()

    assert socket.sent == [{"envelope_id": "env-1"}]


@pytest.mark.asyncio
async def test_a_throwing_handler_does_not_end_the_connection(redis_lock):
    calls = {"n": 0}

    async def handler(_event):
        calls["n"] += 1
        raise RuntimeError("boom")

    service, socket = make_service(
        [event_envelope(envelope_id="a"), event_envelope(envelope_id="b")],
        handler=handler,
    )

    await service._connect_once()

    assert calls["n"] == 2
    assert len(socket.sent) == 2


@pytest.mark.asyncio
async def test_a_failing_ack_does_not_kill_the_socket(redis_lock):
    socket = FakeSocket([event_envelope(), event_envelope(envelope_id="env-2")],
                        send_fails=True)

    async def open_url():
        return SlackSocketUrl(url="wss://slack.test/link")

    seen: list[dict] = []

    async def handler(event):
        seen.append(event)

    service = SlackSocketModeService(
        open_url=open_url, connect=lambda _u: socket, handler=handler
    )
    connected, _ = await service._connect_once()

    assert connected is True
    assert len(seen) == 2  # both messages still processed


# ── 2. Envelope kinds ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hello_is_not_acknowledged(redis_lock):
    """`hello` carries no envelope_id — acking it would be inventing traffic."""
    service, socket = make_service([json.dumps({"type": "hello"})])

    await service._connect_once()

    assert socket.sent == []


@pytest.mark.asyncio
async def test_a_disconnect_ends_the_connection_without_an_error(redis_lock, caplog):
    """Slack disconnects on its own schedule. That is weather, not an error."""
    caplog.set_level("INFO")
    service, socket = make_service(
        [json.dumps({"type": "disconnect", "reason": "refresh_requested"}),
         event_envelope()]
    )

    connected, _ = await service._connect_once()

    assert connected is True
    assert socket.sent == []  # the event after the disconnect is never processed
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]


@pytest.mark.asyncio
async def test_an_unreadable_frame_is_skipped_not_fatal(redis_lock):
    service, socket = make_service(["}{ not json", event_envelope()])

    await service._connect_once()

    assert socket.sent == [{"envelope_id": "env-1"}]


@pytest.mark.asyncio
async def test_an_envelope_without_an_event_is_ignored(redis_lock):
    service, _socket = make_service(
        [envelope("events_api", "env-1", payload={"type": "event_callback"})]
    )

    await service._connect_once()

    assert service.seen == []  # type: ignore[attr-defined]


# ── 3. Reconnect + backoff ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_open_reports_why_and_does_not_connect(redis_lock):
    async def open_url():
        return SlackSocketUrl(code="no_app_token", error="No Slack app-level token stored.")

    service = SlackSocketModeService(open_url=open_url, connect=lambda _u: None)

    connected, detail = await service._connect_once()

    assert connected is False
    assert "app-level token" in detail


@pytest.mark.asyncio
async def test_the_loop_reconnects_after_a_disconnect(redis_lock):
    """Two connections in a row: the whole point of surviving a disconnect."""
    sockets = [
        FakeSocket([json.dumps({"type": "disconnect"})]),
        FakeSocket([event_envelope()]),
    ]
    handed: list[FakeSocket] = []

    async def open_url():
        return SlackSocketUrl(url="wss://slack.test/link")

    def connect(_url):
        socket = sockets.pop(0) if sockets else FakeSocket([])
        handed.append(socket)
        return socket

    seen: list[dict] = []

    async def handler(event):
        seen.append(event)
        service._running = False  # stop after the second connection did its work

    service = SlackSocketModeService(open_url=open_url, connect=connect, handler=handler)
    service._running = True
    await asyncio.wait_for(service._run_loop(), timeout=5)

    assert len(handed) >= 2
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_permanently_broken_slack_backs_off_instead_of_spinning(
    redis_lock, monkeypatch
):
    """Without backoff this is a hot loop that fills the log in seconds."""
    attempts = {"n": 0}
    sleeps: list[float] = []

    async def open_url():
        attempts["n"] += 1
        if attempts["n"] >= 4:
            service._running = False
        return SlackSocketUrl(code="transport_error", error="could not reach Slack")

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    service = SlackSocketModeService(open_url=open_url, connect=lambda _u: None)
    service._running = True
    await asyncio.wait_for(service._run_loop(), timeout=5)

    assert attempts["n"] == 4
    # Strictly growing waits (jitter keeps them from being exact multiples).
    assert len(sleeps) >= 3
    assert sleeps[-1] > sleeps[0]


@pytest.mark.asyncio
async def test_a_broken_slack_does_not_flood_the_log(redis_lock, monkeypatch, caplog):
    caplog.set_level("WARNING")
    attempts = {"n": 0}

    async def open_url():
        attempts["n"] += 1
        if attempts["n"] >= 12:
            service._running = False
        return SlackSocketUrl(code="transport_error", error="could not reach Slack")

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))

    service = SlackSocketModeService(open_url=open_url, connect=lambda _u: None)
    service._running = True
    await asyncio.wait_for(service._run_loop(), timeout=5)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) < attempts["n"], "every failure was logged loudly"


# ── 4. One process, one socket ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_worker_does_not_open_a_second_socket(redis_lock):
    """Two sockets = every operator message processed twice."""
    first = SlackSocketModeService(open_url=None, connect=None)
    second = SlackSocketModeService(open_url=None, connect=None)

    assert await first._acquire_lock() is True
    assert await second._acquire_lock() is False


@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_service_stops(redis_lock):
    first = SlackSocketModeService()
    second = SlackSocketModeService()

    await first._acquire_lock()
    await first._release_lock()

    assert await second._acquire_lock() is True


@pytest.mark.asyncio
async def test_the_same_worker_may_take_its_own_lock_back(redis_lock):
    """A reconnect must not lock the owner out of its own socket."""
    service = SlackSocketModeService()

    assert await service._acquire_lock() is True
    assert await service._acquire_lock() is True


@pytest.mark.asyncio
async def test_losing_the_lock_closes_the_connection(redis_lock, monkeypatch):
    """If another worker took over, this socket must stop reading — otherwise
    both process the same messages."""
    monkeypatch.setattr("app.services.slack_socket.LOCK_RENEW_INTERVAL", 0)
    service, socket = make_service(
        [event_envelope(envelope_id=f"env-{i}") for i in range(3)]
    )
    await service._acquire_lock()
    # Somebody else owns it now.
    await redis_lock.set(LOCK_KEY, "another-worker")

    await service._connect_once()

    assert len(socket.sent) == 1  # stopped right after the first envelope


@pytest.mark.asyncio
async def test_redis_being_down_does_not_silence_the_channel(monkeypatch):
    """Fail open: a rare duplicate beats a chat that never delivers anything."""
    async def broken_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.redis_client.get_redis", broken_redis)
    service = SlackSocketModeService()

    assert await service._acquire_lock() is True


# ── 5. Off is silent ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_service_stays_down_when_slack_is_switched_off(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    monkeypatch.setattr(settings, "slack_team_chat_enabled", False, raising=False)
    service = SlackSocketModeService()

    await service.start()

    assert service._task is None
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    await service.stop()


@pytest.mark.asyncio
async def test_the_service_stays_down_when_chat_channels_excludes_slack(monkeypatch):
    monkeypatch.setattr(settings, "chat_channels", "telegram", raising=False)
    service = SlackSocketModeService()

    await service.start()

    assert service._task is None
    await service.stop()


@pytest.mark.asyncio
async def test_the_service_runs_when_slack_is_selected(monkeypatch):
    monkeypatch.setattr(settings, "chat_channels", "slack,telegram", raising=False)
    assert SlackSocketModeService().should_run() is True


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent(monkeypatch, redis_lock):
    async def open_url():
        await asyncio.sleep(0.01)
        return SlackSocketUrl(code="transport_error", error="nope")

    service = SlackSocketModeService(open_url=open_url, connect=lambda _u: None)
    await service.start()
    await service.start()
    assert service._running is True
    await service.stop()
    await service.stop()
    assert service._running is False
    assert service._task is None


# ── 6. The dependency choice ──────────────────────────────────────────────


def test_no_slack_sdk_is_imported():
    """MC talks to Slack over httpx + websockets, both already dependencies.
    A stray `slack_sdk`/`slack_bolt` import would add a package and a second
    way of doing everything."""
    import inspect

    from app.services import slack_client, slack_inbound, slack_socket

    for module in (slack_socket, slack_inbound, slack_client):
        for line in inspect.getsource(module).splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "slack_sdk" not in stripped
                assert "slack_bolt" not in stripped


def test_the_websocket_library_is_a_declared_dependency():
    """`websockets` is in backend/pyproject.toml — no new package was added."""
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert "websockets" in pyproject.read_text(encoding="utf-8")
