"""Step-up re-authentication: a fresh password confirmation for dangerous
admin actions (ADR-094 decision 9).

Why this exists
---------------
A login lasts 7 days. Starting a self-update or rolling one back replaces
the running MC and can lose data written since the last backup, so a
forgotten unlocked phone must not be enough. The operator types the
password again; the result is a **step-up token**:

* minted by ``POST /api/v1/auth/step-up`` (routers/auth.py) after the
  password is checked — behind the same per-IP brake as ``/login`` plus a
  per-account one,
* random and opaque (``secrets.token_urlsafe(32)``); Redis only stores its
  SHA-256, so a Redis dump holds nothing spendable,
* bound to the user AND to the login generation (``token_version``) — a
  logout or password change kills it,
* valid for :data:`STEP_UP_TTL_SECONDS` (5 minutes),
* consumed by the first protected request (Redis ``GETDEL``, atomic). It is
  burnt even when presented by the wrong user.

A route opts in with ``Depends(require_recent_auth())`` and the client sends
the token in the :data:`STEP_UP_HEADER` header. Combine it with
``require_role(Role.ADMIN)`` where the action is admin-only — step-up proves
*who is at the keyboard right now*, not what they may do.

Refused, by design:

* ``LOCAL_AUTH_TOKEN`` and the host-side MCP service token. Both resolve to
  "the first admin" without anybody typing a password — there is no human to
  re-authenticate, and knowing that admin's password must not upgrade a
  shared script token.
* Accounts without a password (v1: the UI hides the button and points to
  the terminal). Checked when minting AND when spending.
* The HttpOnly session cookie and a ``?token=`` query JWT. The cookie rides
  along on every same-site request (a preview page on another localhost
  port included); the UI tab always sends the Authorization header.

Status codes: a missing, spent, expired or foreign step-up token answers
**403** with ``detail.code == "step_up_required"`` — never 401, because the
UI's ``request()`` wrapper treats any 401 outside ``/auth/`` as "session
expired" and signs the operator out.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from app.auth import (
    AUTH_BEARER_JWT,
    AUTH_LOCAL_TOKEN,
    AUTH_SERVICE_JWT,
    auth_method,
    require_user,
)
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

STEP_UP_TTL_SECONDS = 300
STEP_UP_HEADER = "X-MC-Step-Up"
_KEY_PREFIX = "mc:step-up:"

# Machine-readable refusal codes (detail.code). The UI maps them to text.
REQUIRED = "step_up_required"
WRONG_PASSWORD = "step_up_wrong_password"
NO_PASSWORD = "step_up_no_password"
LOCAL_TOKEN = "step_up_local_token"
HEADER_REQUIRED = "step_up_header_required"
UNAVAILABLE = "step_up_unavailable"

_MESSAGES = {
    REQUIRED: "Confirm your password again to continue.",
    WRONG_PASSWORD: "Wrong password.",
    NO_PASSWORD: "This account has no password. Use the terminal for this action.",
    LOCAL_TOKEN: "Only a real admin login can confirm this action, not LOCAL_AUTH_TOKEN.",
    HEADER_REQUIRED: "Step-up needs the Authorization header of a logged-in session.",
    UNAVAILABLE: "Step-up is unavailable right now (Redis unreachable).",
}


def refusal(status_code: int, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": _MESSAGES[code]}
    )


def unavailable_reason(request: Request, user) -> str | None:
    """Why ``user`` on this request can NOT step up, or ``None`` if they can.

    Shared by the mint endpoint, the availability probe and
    :func:`require_recent_auth`, so the three never disagree."""
    method = auth_method(request)
    if method in (AUTH_LOCAL_TOKEN, AUTH_SERVICE_JWT):
        return LOCAL_TOKEN
    if method != AUTH_BEARER_JWT:
        return HEADER_REQUIRED
    if not getattr(user, "password_hash", None):
        return NO_PASSWORD
    return None


@dataclass(frozen=True)
class _Grant:
    user_id: str
    token_version: int


def _key(token: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()


async def issue(*, user_id: str, token_version: int) -> str:
    """Mint a step-up token. The caller has already verified the password
    and :func:`unavailable_reason`. Raises when Redis is unreachable."""
    token = secrets.token_urlsafe(32)
    payload = json.dumps({"uid": user_id, "tv": token_version})
    redis = await get_redis()
    await redis.set(_key(token), payload, ex=STEP_UP_TTL_SECONDS)
    return token


async def _redeem(token: str) -> _Grant | None:
    """Consume ``token``. ``None`` = unknown, expired or already spent.
    Raises when Redis is unreachable (the caller answers 503, never a pass)."""
    if not token or len(token) > 256:
        return None
    redis = await get_redis()
    raw = await redis.getdel(_key(token))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return _Grant(user_id=str(data["uid"]), token_version=int(data["tv"]))
    except (TypeError, ValueError, KeyError):
        return None


def require_recent_auth():
    """FastAPI dependency factory: the caller confirmed their password within
    the last :data:`STEP_UP_TTL_SECONDS` and spends that confirmation now.

    Returns the authenticated user. Order of checks: login (401 via
    require_user) → credential kind / password (403 with the reason) →
    step-up token (403 ``step_up_required``; 503 when Redis is down)."""

    async def _check(request: Request, current_user=Depends(require_user)):
        reason = unavailable_reason(request, current_user)
        if reason is not None:
            raise refusal(status.HTTP_403_FORBIDDEN, reason)

        token = request.headers.get(STEP_UP_HEADER, "").strip()
        if not token:
            raise refusal(status.HTTP_403_FORBIDDEN, REQUIRED)
        try:
            grant = await _redeem(token)
        except Exception as exc:  # noqa: BLE001 — Redis down
            logger.warning("step-up redeem failed: %s", type(exc).__name__)
            raise refusal(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from exc
        if (
            grant is None
            or grant.user_id != str(current_user.id)
            or grant.token_version != (current_user.token_version or 0)
        ):
            if grant is not None:
                logger.info("step-up token presented by another user/session — burnt")
            raise refusal(status.HTTP_403_FORBIDDEN, REQUIRED)
        return current_user

    return _check
