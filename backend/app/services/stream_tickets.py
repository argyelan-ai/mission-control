"""Short-lived, single-use tickets for opening SSE streams and WebSockets.

Why this exists
---------------
Browsers cannot set an ``Authorization`` header on ``EventSource`` or
``WebSocket``. The UI used to append the operator's login JWT as
``?token=<jwt>`` instead — and every proxy/access/error log that records the
request URI then held a 30-day operator credential in plain text (Caddy
error log: hundreds of lines per day). Anyone able to read the container
logs could take over the operator session.

A stream ticket replaces the JWT in the URL:

* issued by an authenticated ``POST /api/v1/auth/stream-ticket`` (the JWT
  travels in the ``Authorization`` header, which is never logged),
* random and opaque (256 bit, ``secrets.token_urlsafe(32)``) — carries no
  claims, so a leaked ticket reveals nothing,
* bound to the issuing user AND to the exact stream path it was minted for,
* valid for ``settings.stream_ticket_ttl_seconds`` (default 60 s),
* consumed on first use (Redis ``GETDEL`` — atomic, so two racing
  connections can never both redeem it). A ticket that reaches a log is
  already spent or expires within a minute.

Only the SHA-256 of the ticket is used as the Redis key, so a Redis dump
does not contain redeemable tickets either.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "mc:stream-ticket:"

# Paths a ticket may be minted for: every long-lived stream the browser opens
# (SSE endpoints end in /stream, WebSockets in /ws, /terminal,
# /voice-highlight or /voice-display). Anything else is refused at issue
# time so a ticket can never become a general-purpose credential for a
# regular REST route.
_STREAM_PATH_RE = re.compile(
    r"^/api/v1/(?:[A-Za-z0-9_.:\-]+/)*"
    r"(?:stream|ws|terminal|voice-highlight|voice-display)$"
)


@dataclass(frozen=True)
class StreamTicketClaims:
    user_id: str
    token_version: int
    legacy_admin: bool


def is_stream_path(path: str) -> bool:
    return bool(_STREAM_PATH_RE.match(path)) and ".." not in path


def _key(ticket: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(ticket.encode()).hexdigest()


async def issue_ticket(
    *,
    user_id: str,
    token_version: int,
    path: str,
    legacy_admin: bool = False,
) -> str:
    """Mint a ticket for ``path``. Caller must already have authenticated
    the user and validated ``path`` with :func:`is_stream_path`."""
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "uid": user_id,
            "tv": token_version,
            "path": path,
            "legacy": legacy_admin,
        }
    )
    redis = await get_redis()
    await redis.set(_key(ticket), payload, ex=settings.stream_ticket_ttl_seconds)
    return ticket


async def redeem_ticket(ticket: str, path: str) -> StreamTicketClaims | None:
    """Consume ``ticket`` for a connection to ``path``.

    Returns the bound claims, or ``None`` when the ticket is unknown, expired,
    already used, or was minted for a different path. The ticket is consumed
    in every case where it existed — a ticket presented on the wrong path is
    burnt, not left lying around for a second attempt.
    """
    if not ticket or len(ticket) > 256:
        return None
    try:
        redis = await get_redis()
        raw = await redis.getdel(_key(ticket))
    except Exception as exc:  # noqa: BLE001 — Redis down = no stream auth
        logger.warning("stream ticket redeem failed: %s", type(exc).__name__)
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if data.get("path") != path:
        logger.info("stream ticket presented on the wrong path — rejected")
        return None
    return StreamTicketClaims(
        user_id=str(data.get("uid", "")),
        token_version=int(data.get("tv", 0)),
        legacy_admin=bool(data.get("legacy", False)),
    )
