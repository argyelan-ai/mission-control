"""Slack Socket Mode — the direction Slack cannot call.

MC is self-hosted behind Tailscale and has no public URL, so Slack's normal
delivery (an HTTP POST to a Request URL) can never reach it. Socket Mode turns
the arrow around: MC asks `apps.connections.open` for a single-use `wss://`
URL, connects outbound, and receives events on that socket. Nothing has to be
exposed to the internet.

Four properties this service exists to guarantee:

  * **Every envelope is acknowledged.** Slack redelivers anything it does not
    see an ack for within three seconds — an unacked message comes back, and
    back, and back. The ack is therefore sent BEFORE the handler runs, not
    after: a handler that throws must still not cause a redelivery storm, and
    the handler's own error handling is what protects the message.

  * **A dropped connection is normal.** Slack disconnects on its own schedule
    (`type: "disconnect"`, reason `warning`/`refresh_requested`), and it is not
    an error. The loop reconnects with exponential backoff and logs a *repeated*
    failure quietly, so a Slack outage costs one warning, not a log per second.

  * **Exactly one process holds the socket.** Uvicorn can run several workers;
    two sockets would mean every message is processed twice. Same answer the
    other long-runners give (``intelligence``, ``runtime_watcher``): a Redis
    lock, held for the lifetime of the connection and renewed while it lives.
    Redis being unreachable fails OPEN (connect anyway) — a silent channel is
    worse than a rare duplicate, and that is the same trade the other services
    already make.

  * **Off is silent.** No app token, switch off, or `slack` not in
    CHAT_CHANNELS → the service does not start, logs one INFO line, and raises
    nothing. No credentials, no noise.

No `slack_sdk`/`slack_bolt`: MC already speaks to Slack over plain httpx
(`slack_client`), and `websockets` is already a direct dependency (pyproject),
so the socket needs no new package either. One dependency-free path is easier
to keep honest than two.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import uuid

logger = logging.getLogger("mc.slack_socket")

# Redis lock: held while the socket is open, renewed at a third of its TTL so a
# stalled renewal still has two attempts before the lock expires. A crashed
# worker therefore frees the socket after at most LOCK_TTL seconds.
LOCK_KEY = "mc:slack:socket:lock"
LOCK_TTL = 60
LOCK_RENEW_INTERVAL = 20
# How long a worker that lost the race waits before trying again.
LOCK_RETRY_INTERVAL = 30

BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
# A connection must have lived at least this long to count as healthy. Slack's
# own rotation happens after minutes; anything that dies inside seconds is a
# fault and must go through the backoff, not the "reconnect at once" path.
HEALTHY_AFTER = 5.0
# After this many consecutive failures the warnings stop and only every
# QUIET_EVERY-th failure is logged — a Slack outage must not fill the log.
LOUD_FAILURES = 3
QUIET_EVERY = 20

# Envelope types that carry work and must be acknowledged.
ACKABLE_TYPES = frozenset({"events_api", "slash_commands", "interactive"})


class SlackSocketModeService:
    """Singleton, asyncio loop, Redis lock — the shape every MC long-runner has.

    ``open_url`` and ``connect`` are injectable so tests drive the whole state
    machine (ack, reconnect, backoff, lock) without a socket or a token.
    """

    def __init__(self, *, open_url=None, connect=None, handler=None):
        self._running = False
        self._task: asyncio.Task | None = None
        self._open_url = open_url
        self._connect = connect
        self._handler = handler
        self._owner = uuid.uuid4().hex
        self._holds_lock = False
        # Seconds the most recent connection stayed up — the loop uses it to
        # tell Slack's normal rotation apart from a flapping connection.
        self._last_connection_lasted = 0.0
        # Diagnostics — cheap, and the only way to see from outside that the
        # socket is actually alive rather than merely "started".
        self.connections = 0
        self.envelopes_acked = 0
        self.events_handled = 0

    # ── Switch ───────────────────────────────────────────────────────────

    def should_run(self) -> bool:
        """The channel is selected AND switched on. Credentials are checked
        later (the token lives in the database, this is a sync call)."""
        try:
            from app.services.chat_adapter import enabled_chat_adapters

            return any(a.key == "slack" for a in enabled_chat_adapters())
        except Exception as e:  # noqa: BLE001 — a broken flag must not start a socket
            logger.warning("slack socket: could not read the channel switch: %s", e)
            return False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        if not self.should_run():
            logger.info(
                "Slack Socket Mode off (SLACK_TEAM_CHAT_ENABLED / CHAT_CHANNELS) "
                "— no inbound Slack"
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="slack_socket_mode")
        logger.info("Slack Socket Mode starting")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._release_lock()
        logger.info("Slack Socket Mode stopped")

    # ── The loop ─────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        backoff = BACKOFF_START
        failures = 0
        try:
            while self._running:
                if not await self._acquire_lock():
                    # Another worker owns the socket. Not an error, not a log
                    # line per attempt — just wait and try again.
                    await asyncio.sleep(LOCK_RETRY_INTERVAL)
                    continue
                try:
                    connected, detail = await self._connect_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — the loop must outlive anything
                    connected, detail = False, f"{type(e).__name__}: {e}"
                finally:
                    await self._release_lock()
                if not connected:
                    self._log_failure(failures, detail or "unknown")
                elif self._last_connection_lasted < HEALTHY_AFTER:
                    self._log_failure(
                        failures,
                        f"connection closed after "
                        f"{self._last_connection_lasted:.1f}s",
                    )

                if not self._running:
                    break
                if connected and self._last_connection_lasted >= HEALTHY_AFTER:
                    # A connection that lived and then ended is Slack's normal
                    # rhythm — reconnect straight away, no backoff, no warning.
                    backoff, failures = BACKOFF_START, 0
                    await asyncio.sleep(0)
                    continue
                # A connection that dies within seconds is NOT the normal rhythm.
                # Without this the "reconnect at once" rule above would turn a
                # server that accepts and instantly closes into a hot loop
                # hammering apps.connections.open.
                failures += 1
                await asyncio.sleep(backoff * (0.5 + random.random()))
                backoff = min(backoff * 2, BACKOFF_MAX)
        except asyncio.CancelledError:
            pass
        finally:
            await self._release_lock()
            logger.info("Slack Socket Mode loop exited (running=%s)", self._running)

    def _log_failure(self, failures: int, detail: str) -> None:
        """Loud while it is news, quiet while it is weather."""
        if failures < LOUD_FAILURES:
            logger.warning("Slack Socket Mode connection failed: %s", detail)
        elif failures % QUIET_EVERY == 0:
            logger.warning(
                "Slack Socket Mode still failing (%d attempts): %s", failures + 1, detail
            )
        else:
            logger.debug("Slack Socket Mode connection failed: %s", detail)

    async def _connect_once(self) -> tuple[bool, str | None]:
        """One connection, from URL to close.

        ``(True, None)``  — the socket was open and the connection ended
                            (Slack's normal rhythm; reconnect at once).
        ``(False, why)``  — we never got in; the caller backs off.
        """
        self._last_connection_lasted = 0.0
        result = await self._open_socket_url()
        if result.url is None:
            return False, result.error or result.code or "unknown"

        opened = False
        started = asyncio.get_running_loop().time()
        try:
            async with self._open_socket(result.url) as socket:
                opened = True
                self.connections += 1
                logger.info("Slack Socket Mode connected")
                await self._pump(socket)
        finally:
            if opened:
                self._last_connection_lasted = (
                    asyncio.get_running_loop().time() - started
                )
        return opened, None

    async def _pump(self, socket) -> None:
        """Read envelopes until the socket ends. Renews the lock as it goes."""
        renew_at = asyncio.get_running_loop().time() + LOCK_RENEW_INTERVAL
        async for raw in socket:
            try:
                envelope = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("slack socket: unreadable frame ignored")
                continue

            if not await self._handle_envelope(socket, envelope):
                return  # Slack asked us to go away

            now = asyncio.get_running_loop().time()
            if now >= renew_at:
                if not await self._renew_lock():
                    logger.warning(
                        "slack socket: lost the Redis lock — closing to avoid a "
                        "second reader"
                    )
                    return
                renew_at = now + LOCK_RENEW_INTERVAL

    async def _handle_envelope(self, socket, envelope: dict) -> bool:
        """Ack + dispatch one envelope. False = close the connection."""
        kind = envelope.get("type")

        if kind == "hello":
            logger.debug("slack socket: hello")
            return True
        if kind == "disconnect":
            # Slack's own scheduled rotation. Normal operation, INFO not WARNING.
            logger.info(
                "slack socket: Slack asked to reconnect (%s)",
                envelope.get("reason") or "no reason given",
            )
            return False

        envelope_id = envelope.get("envelope_id")
        if envelope_id and kind in ACKABLE_TYPES:
            # ACK FIRST. Slack redelivers anything unacked after ~3s; a slow or
            # throwing handler must not turn one message into an endless retry.
            await self._ack(socket, envelope_id)

        if kind == "events_api":
            await self._dispatch(envelope.get("payload") or {})
        return True

    async def _ack(self, socket, envelope_id: str) -> None:
        try:
            await socket.send(json.dumps({"envelope_id": envelope_id}))
            self.envelopes_acked += 1
        except Exception as e:  # noqa: BLE001 — a failed ack costs a redelivery, not the loop
            logger.warning("slack socket: ack failed: %s", type(e).__name__)

    async def _dispatch(self, payload: dict) -> None:
        event = payload.get("event") or {}
        if not event:
            return
        try:
            await self._ingest(event)
            self.events_handled += 1
        except Exception as e:  # noqa: BLE001 — one bad message must not kill the socket
            logger.exception("slack inbound handler error: %s", e)

    # ── Seams (overridden in tests) ──────────────────────────────────────

    async def _open_socket_url(self):
        if self._open_url is not None:
            return await self._open_url()
        from app.services.slack_client import open_socket_connection

        return await open_socket_connection()

    def _open_socket(self, url: str):
        if self._connect is not None:
            return self._connect(url)
        from websockets.asyncio.client import connect

        # ping_interval keeps a NAT/Tailscale path from going quietly dead:
        # without it a half-open socket looks connected and receives nothing.
        return connect(url, ping_interval=20, ping_timeout=20, max_queue=64)

    async def _ingest(self, event: dict) -> None:
        if self._handler is not None:
            await self._handler(event)
            return
        from app.services.slack_inbound import ingest_slack_event

        await ingest_slack_event(event)

    # ── Redis lock ───────────────────────────────────────────────────────

    async def _acquire_lock(self) -> bool:
        """One owner per fleet. Redis unreachable → fail open (see module head)."""
        try:
            from app.redis_client import get_redis

            redis = await get_redis()
            acquired = await redis.set(LOCK_KEY, self._owner, nx=True, ex=LOCK_TTL)
            if not acquired:
                # Ours already (same worker reconnecting)? Then take it back.
                current = await redis.get(LOCK_KEY)
                if _as_text(current) != self._owner:
                    return False
                await redis.set(LOCK_KEY, self._owner, ex=LOCK_TTL)
            self._holds_lock = True
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("slack socket: Redis lock unavailable (%s) — running anyway", e)
            self._holds_lock = False
            return True

    async def _renew_lock(self) -> bool:
        if not self._holds_lock:
            return True  # never had one (Redis down) — nothing to lose
        try:
            from app.redis_client import get_redis

            redis = await get_redis()
            current = await redis.get(LOCK_KEY)
            if _as_text(current) != self._owner:
                return False
            await redis.set(LOCK_KEY, self._owner, ex=LOCK_TTL)
            return True
        except Exception as e:  # noqa: BLE001 — same fail-open trade as acquiring
            logger.debug("slack socket: lock renewal failed (%s) — keeping the socket", e)
            return True

    async def _release_lock(self) -> None:
        if not self._holds_lock:
            return
        self._holds_lock = False
        try:
            from app.redis_client import get_redis

            redis = await get_redis()
            if _as_text(await redis.get(LOCK_KEY)) == self._owner:
                await redis.delete(LOCK_KEY)
        except Exception:  # noqa: BLE001 — the TTL cleans up after us
            pass


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


#: Singleton, wired into the app lifespan in main.py.
slack_socket = SlackSocketModeService()
