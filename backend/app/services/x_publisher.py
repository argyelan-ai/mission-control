"""X (Twitter) Publisher — draft validation + tweepy-backed posting.

Part of the generic Draft -> Approve -> Post flow (Approval action_type
"x_post", see routers/x_posts.py + the hook in routers/approvals.py).

Auth: OAuth 1.0a user context (tweepy.Client with consumer key/secret +
access token/secret) — required for POST /2/tweets on behalf of a user
account. Tokens are System-Tokens (ADR-033: "wie MC selbst mit der Welt
redet") and therefore live in the `secrets` table, not `credentials`.

Secret keys (set once by the operator via Settings -> Secrets, Admin-only):
    x_api_key             (Consumer/API Key)
    x_api_secret           (Consumer/API Key Secret)
    x_access_token          (Access Token)
    x_access_token_secret   (Access Token Secret)

Note on `publish_adapters.py`: the news-studio vertical (maintainer-private,
stripped from the public OSS repo — see ADR "extract news-studio into a
strippable vertical module") already has a `publish_twitter()` used for
storyboard threads, keyed on a single `twitter_bearer_token` secret. That
adapter is thread-shaped (multi-tweet, split on blank lines) and specific to
storyboards; it doesn't apply outside that vertical and a bearer token alone
cannot authenticate user-context POSTs against X API v2. This module is the
generic, always-available counterpart for single-post drafts approved via the
core Approval flow, using the correct OAuth 1.0a user-context credentials.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.secrets_helper import get_secret_plaintext_by_key

log = logging.getLogger("mc.x_publisher")

MAX_TWEET_LENGTH = 280
LINK_COST_HINT = (
    "Link erkannt — X berechnet fuer Posts mit Link zusaetzlich Kosten "
    "(ca. $0.015-0.20 je nach Volumen, Stand Juli 2026)."
)

_URL_RE = re.compile(r"https?://\S+")

_SECRET_KEYS = {
    "api_key": "x_api_key",
    "api_secret": "x_api_secret",
    "access_token": "x_access_token",
    "access_token_secret": "x_access_token_secret",
}


@dataclass
class DraftValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_link: bool = False


def validate_draft(text: str) -> DraftValidation:
    """Validates a tweet draft: length <= 280, non-empty, link cost hint."""
    errors: list[str] = []
    warnings: list[str] = []

    if not text or not text.strip():
        errors.append("Draft ist leer")
        return DraftValidation(ok=False, errors=errors)

    length = len(text)
    if length > MAX_TWEET_LENGTH:
        errors.append(f"Draft hat {length} Zeichen (max {MAX_TWEET_LENGTH})")

    has_link = bool(_URL_RE.search(text))
    if has_link:
        warnings.append(LINK_COST_HINT)

    return DraftValidation(ok=not errors, errors=errors, warnings=warnings, has_link=has_link)


class XPublisherError(Exception):
    """Raised for configuration errors (missing secrets) — never for API-side failures."""


async def _load_client(session: AsyncSession):
    """Builds a tweepy.Client from the 4 secrets. Raises XPublisherError if any is missing."""
    import tweepy  # local import: keeps tweepy optional for code paths that never post

    values: dict[str, str] = {}
    missing: list[str] = []
    for arg_name, secret_key in _SECRET_KEYS.items():
        value = await get_secret_plaintext_by_key(session, secret_key)
        if not value:
            missing.append(secret_key)
        else:
            values[arg_name] = value

    if missing:
        raise XPublisherError(
            "X-Secrets fehlen in der Vault (Settings -> Secrets, Admin): "
            + ", ".join(missing)
        )

    return tweepy.Client(
        consumer_key=values["api_key"],
        consumer_secret=values["api_secret"],
        access_token=values["access_token"],
        access_token_secret=values["access_token_secret"],
    )


def _classify_tweepy_error(exc: Exception) -> tuple[str, str]:
    """Maps a tweepy exception to (error_type, message) for a clean Result — never lets
    the caller crash on rate-limit/auth/duplicate errors."""
    import tweepy

    if isinstance(exc, tweepy.TooManyRequests):
        return "rate_limited", "X API Rate-Limit erreicht (429) — spaeter erneut versuchen."
    if isinstance(exc, tweepy.Forbidden):
        text = str(exc)
        if "duplicate" in text.lower():
            return "duplicate", "X lehnt den Post als Duplikat ab (403, duplicate content)."
        return "forbidden", f"X API verweigert den Post (403): {text[:300]}"
    if isinstance(exc, tweepy.Unauthorized):
        return "unauthorized", "X API Auth fehlgeschlagen (401) — Secrets pruefen/erneuern."
    if isinstance(exc, tweepy.BadRequest):
        return "bad_request", f"X API lehnt den Request ab (400): {str(exc)[:300]}"
    if isinstance(exc, tweepy.TweepyException):
        return "api_error", f"X API Fehler: {str(exc)[:300]}"
    return "unknown_error", f"Unerwarteter Fehler: {exc}"


async def post_text(session: AsyncSession, text: str) -> dict[str, Any]:
    """Posts a single tweet via tweepy. Returns a Result dict, never raises for
    API-side failures (rate-limit/403/duplicate/etc) — only for missing config.

    Returns:
        {"ok": True, "tweet_id": str, "url": str}
        {"ok": False, "error_type": str, "error": str}
    """
    validation = validate_draft(text)
    if not validation.ok:
        return {"ok": False, "error_type": "invalid_draft", "error": "; ".join(validation.errors)}

    try:
        client = await _load_client(session)
    except XPublisherError as e:
        return {"ok": False, "error_type": "missing_secrets", "error": str(e)}

    try:
        # tweepy.Client is sync (requests under the hood) — run off the event loop.
        response = await asyncio.to_thread(client.create_tweet, text=text)
    except Exception as exc:  # noqa: BLE001 — deliberately broad, classified below
        error_type, message = _classify_tweepy_error(exc)
        log.warning("X post failed (%s): %s", error_type, message)
        return {"ok": False, "error_type": error_type, "error": message}

    tweet_id = str(response.data["id"])
    return {
        "ok": True,
        "tweet_id": tweet_id,
        "url": f"https://x.com/i/status/{tweet_id}",
    }
