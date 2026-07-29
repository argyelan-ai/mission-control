"""Slack client foundation — token lookup + connection test.

Slack is a second team-chat channel next to Telegram. MC runs self-hosted
behind Tailscale with no public URL, so Slack cannot call MC via a Request
URL: MC opens the connection itself via **Socket Mode**. That needs two
tokens, and mixing them up is the single most likely setup mistake:

    slack_bot_token   xoxb-...   OAuth & Permissions -> Bot User OAuth Token
                                 Used for every Web API call (auth.test,
                                 chat.postMessage, ...).
    slack_app_token   xapp-...   Basic Information -> App-Level Tokens,
                                 scope `connections:write`. Used only to open
                                 the Socket Mode websocket.

Both are System-Tokens ("how MC itself talks to the world", ADR-033) and
therefore live in the `secrets` table (Fernet-encrypted, admin-only), not in
`credentials`. They are read through `secrets_helper`, the same path
`x_publisher.py` uses — there is no second mechanism.

Security invariant: no token value ever leaves this module. Neither the
result object nor any log line carries a token or a fragment of one; only
key names, prefixes we detected as *wrong*, and Slack's own error codes.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.secrets_helper import get_secret_plaintext_by_key

log = logging.getLogger("mc.slack")

SLACK_API_BASE = "https://slack.com/api"
BOT_TOKEN_KEY = "slack_bot_token"
APP_TOKEN_KEY = "slack_app_token"

BOT_TOKEN_PREFIX = "xoxb-"
APP_TOKEN_PREFIX = "xapp-"

_REQUEST_TIMEOUT = 10.0

# Slack's error codes are terse. Translate the ones an operator can actually
# act on; anything else is passed through verbatim so nothing gets swallowed.
_SLACK_ERROR_HINTS: dict[str, str] = {
    "invalid_auth": (
        "Slack rejected the bot token (invalid_auth). It was revoked, or it "
        "belongs to a different workspace — copy it again from "
        "OAuth & Permissions -> Bot User OAuth Token."
    ),
    "not_authed": "No token was sent to Slack (not_authed).",
    "account_inactive": (
        "The bot account is deactivated in this workspace (account_inactive). "
        "Reinstall the app to the workspace."
    ),
    "token_revoked": (
        "This bot token was revoked (token_revoked). Reinstall the app and "
        "paste the new token."
    ),
    "token_expired": "This bot token has expired (token_expired). Reinstall the app.",
    "missing_scope": (
        "The bot token is missing a required scope (missing_scope). Add the "
        "scopes listed in the setup guide, then reinstall the app."
    ),
    "ratelimited": "Slack is rate-limiting this app (ratelimited). Try again in a minute.",
    # Posting-specific. `not_in_channel` is the single most likely mistake after
    # setup — the app is installed, the token is valid, the channel exists, and
    # nothing arrives, because nobody invited the bot.
    "not_in_channel": (
        "The bot is not a member of that channel (not_in_channel). Open the "
        "channel in Slack and run: /invite @Mission Control"
    ),
    "channel_not_found": (
        "Slack does not know that channel (channel_not_found). Check "
        "SLACK_DEFAULT_CHANNEL — use the channel ID (C…) or #channel-name, and "
        "note that a private channel is invisible to the app until it is invited."
    ),
    "is_archived": "That channel is archived (is_archived). Pick a live channel.",
    "msg_too_long": "Slack refused the message because it is too long (msg_too_long).",
}


def explain_slack_error(code: str) -> str:
    """Slack's error code in words an operator can act on.

    Unknown codes are passed through verbatim rather than swallowed — a code we
    have never seen must still be readable in the log.
    """
    return _SLACK_ERROR_HINTS.get(code, f"Slack rejected the request: {code}")


@dataclass
class SlackConnectionResult:
    """Structured outcome of a connection test. Contains no secret material."""

    connected: bool
    team: str | None = None
    bot_user: str | None = None
    bot_token_set: bool = False
    app_token_set: bool = False
    # Socket Mode is only ready when a plausible xapp- token is present.
    socket_mode_ready: bool = False
    # Problem with the bot token / the Slack call itself.
    error: str | None = None
    # Problem with the app-level token — deliberately a SEPARATE field so a
    # missing Socket-Mode token is never reported as "bot token invalid".
    app_token_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _describe_wrong_prefix(value: str, *, field: str) -> str | None:
    """Returns a spoken-language message when a token sits in the wrong field.

    Swapping the two tokens is the most likely operator error, and Slack would
    answer a swapped bot token with a bare `invalid_auth` — useless. We catch
    it before the network call.
    """
    if field == "bot":
        if value.startswith(APP_TOKEN_PREFIX):
            return (
                "This looks like the app-level token (xapp-...) in the bot token "
                "field. The bot token starts with xoxb- and comes from "
                "OAuth & Permissions -> Bot User OAuth Token."
            )
        if not value.startswith(BOT_TOKEN_PREFIX):
            return (
                "The bot token has an unexpected format — it must start with "
                "xoxb- (OAuth & Permissions -> Bot User OAuth Token)."
            )
        return None

    if value.startswith(BOT_TOKEN_PREFIX):
        return (
            "This looks like the bot token (xoxb-...) in the app-level token "
            "field. The app-level token starts with xapp- and is created under "
            "Basic Information -> App-Level Tokens with the connections:write scope."
        )
    if not value.startswith(APP_TOKEN_PREFIX):
        return (
            "The app-level token has an unexpected format — it must start with "
            "xapp- (Basic Information -> App-Level Tokens, scope connections:write)."
        )
    return None


def _check_app_token(raw: str | None) -> tuple[bool, str | None]:
    """(socket_mode_ready, app_token_error) — never raises."""
    if not raw:
        return False, (
            "No app-level token set. Socket Mode needs one, and without it MC "
            "cannot receive Slack messages. Create it under Basic Information "
            "-> App-Level Tokens with the connections:write scope."
        )
    problem = _describe_wrong_prefix(raw.strip(), field="app")
    if problem:
        return False, problem
    return True, None


async def _call_auth_test(bot_token: str) -> dict:
    """POSTs Slack's auth.test. Returns the parsed body, or a synthetic
    `{"ok": False, "error": ...}` when the call itself failed."""
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{SLACK_API_BASE}/auth.test",
                headers={"Authorization": f"Bearer {bot_token}"},
            )
    except httpx.HTTPError as exc:
        # str(exc) on httpx errors carries the URL, never the auth header.
        log.warning("slack auth.test transport error: %s", type(exc).__name__)
        return {"ok": False, "error": f"could not reach Slack ({type(exc).__name__})"}

    try:
        return response.json()
    except ValueError:
        return {"ok": False, "error": f"unreadable Slack response (HTTP {response.status_code})"}


async def test_connection(session: AsyncSession) -> SlackConnectionResult:
    """Verifies the stored Slack tokens and reports what an operator must fix.

    Bot token and app token are judged independently: a workspace can be
    connected (bot token fine) while Socket Mode is still not ready (app token
    missing), and the result says exactly that.
    """
    bot_raw = await get_secret_plaintext_by_key(session, BOT_TOKEN_KEY)
    app_raw = await get_secret_plaintext_by_key(session, APP_TOKEN_KEY)

    bot_token = bot_raw.strip() if bot_raw else ""
    socket_mode_ready, app_token_error = _check_app_token(app_raw)

    result = SlackConnectionResult(
        connected=False,
        bot_token_set=bool(bot_token),
        app_token_set=bool(app_raw and app_raw.strip()),
        socket_mode_ready=socket_mode_ready,
        app_token_error=app_token_error,
    )

    if not bot_token:
        result.error = (
            "No bot token set. Add the Bot User OAuth Token (xoxb-...) from "
            "OAuth & Permissions."
        )
        return result

    prefix_problem = _describe_wrong_prefix(bot_token, field="bot")
    if prefix_problem:
        result.error = prefix_problem
        return result

    body = await _call_auth_test(bot_token)

    if body.get("ok"):
        result.connected = True
        result.team = body.get("team")
        result.bot_user = body.get("user")
        log.info(
            "slack auth.test ok — team=%s bot=%s socket_mode_ready=%s",
            result.team,
            result.bot_user,
            socket_mode_ready,
        )
        return result

    code = str(body.get("error") or "unknown_error")
    result.error = explain_slack_error(code)
    log.warning("slack auth.test failed — error=%s", code)
    return result


# ── Sending (ADR-072: the transport behind the Slack ChatAdapter) ─────────
#
# The team-chat adapter calls `SlackTransport` and nothing else; everything
# that knows about tokens, HTTP and Slack's error vocabulary stays here.
#
# Why a token CACHE. `chat_adapter.ChatAdapter.send()` deliberately takes no DB
# session (the neutral pipeline hands over a room + a message, not a
# transaction), but Slack's bot token lives in the `secrets` table (ADR-033).
# Opening a session per message would be a database round trip per chat line.
# So the token is read once and kept for `_TOKEN_CACHE_TTL`; a token rotated in
# Settings is picked up within a minute, and `invalidate_bot_token_cache()`
# makes that immediate for callers that know they changed it.

_TOKEN_CACHE_TTL = 60.0

# (expires_at, token-or-None). None = never looked up.
_token_cache: tuple[float, str | None] | None = None


def invalidate_bot_token_cache() -> None:
    """Forget the cached bot token (call after storing a new one)."""
    global _token_cache
    _token_cache = None


def bot_token_looks_present() -> bool:
    """Synchronous view of "is there a bot token at all?".

    `ChatAdapter.is_configured()` is sync and is asked for every message, so it
    cannot hit the database. Unknown (nothing looked up yet) counts as present:
    the alternative — reporting "not configured" until the first send — would
    mean the channel never sends a first message and never learns anything.
    The first real send does the authoritative lookup and, if the token is
    missing, flips this to False.
    """
    if _token_cache is None:
        return True
    return bool(_token_cache[1])


async def get_bot_token(session: AsyncSession | None = None) -> str | None:
    """The stored bot token, cached for `_TOKEN_CACHE_TTL` seconds.

    Opens its own short-lived session when the caller has none. Never raises:
    a database hiccup degrades to "no token" and the send reports it.
    """
    global _token_cache
    now = time.monotonic()
    if _token_cache is not None and _token_cache[0] > now:
        return _token_cache[1]

    try:
        if session is not None:
            raw = await get_secret_plaintext_by_key(session, BOT_TOKEN_KEY)
        else:
            from app.database import async_session_maker

            async with async_session_maker() as own:
                raw = await get_secret_plaintext_by_key(own, BOT_TOKEN_KEY)
    except Exception as exc:  # noqa: BLE001 — chat must never break on the DB
        # Deliberately does not say "token": the static guard in
        # test_slack_connection.py flags any log line that mentions one, and
        # that guard is worth more than a nicer word here.
        log.warning("slack credential lookup failed: %s", type(exc).__name__)
        return None

    token = raw.strip() if raw else None
    _token_cache = (now + _TOKEN_CACHE_TTL, token or None)
    return _token_cache[1]


@dataclass(frozen=True)
class SlackPostResult:
    """Outcome of one chat.postMessage. Carries no token material."""

    ok: bool
    # Slack's message timestamp — the id of the thread this message starts.
    ts: str | None = None
    # Slack's raw error code (for tests/logs) plus the operator-facing sentence.
    code: str | None = None
    error: str | None = None


class SlackTransport:
    """Thin `chat.postMessage` wrapper over httpx.

    No `slack_sdk`: the rest of MC talks to Slack with plain httpx
    (`_call_auth_test` above), and one dependency-free path is easier to keep
    honest than two. Never raises — every failure comes back as
    `SlackPostResult(ok=False, ...)`, because a chat outage must not break
    agent work (ADR-072).
    """

    async def post_message(
        self,
        *,
        channel: str,
        text: str,
        username: str | None = None,
        icon_emoji: str | None = None,
        thread_ts: str | None = None,
        silent: bool = True,
    ) -> SlackPostResult:
        """Post one message.

        `username` + `icon_emoji` are how an agent speaks under its own name and
        face; both require the `chat:write.customize` scope (without it Slack
        silently posts under the app's own identity — see docs/setup/slack.md).

        `silent` is the ping decision the neutral pipeline already made. Slack
        has no `disable_notification`; the equivalent lever is
        `reply_broadcast` — a threaded reply stays inside its thread (quiet)
        unless it is broadcast back into the channel (loud). Outside a thread
        there is nothing to decide: the message lands in the channel either way.
        """
        token = await get_bot_token()
        if not token:
            return SlackPostResult(
                ok=False,
                code="no_token",
                error=(
                    "No Slack bot token stored. Add the Bot User OAuth Token "
                    "(xoxb-…) under Settings → Slack."
                ),
            )

        payload: dict = {"channel": channel, "text": text}
        if username:
            payload["username"] = username
        if icon_emoji:
            payload["icon_emoji"] = icon_emoji
        if thread_ts:
            payload["thread_ts"] = thread_ts
            if not silent:
                payload["reply_broadcast"] = True

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(
                    f"{SLACK_API_BASE}/chat.postMessage",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
            body = response.json()
        except httpx.HTTPError as exc:
            log.warning("slack chat.postMessage transport error: %s", type(exc).__name__)
            return SlackPostResult(
                ok=False,
                code="transport_error",
                error=f"could not reach Slack ({type(exc).__name__})",
            )
        except ValueError:
            return SlackPostResult(
                ok=False, code="bad_response", error="unreadable Slack response"
            )

        if body.get("ok"):
            return SlackPostResult(ok=True, ts=body.get("ts"))

        code = str(body.get("error") or "unknown_error")
        message = explain_slack_error(code)
        log.warning("slack chat.postMessage failed — error=%s: %s", code, message)
        if code in ("invalid_auth", "token_revoked", "account_inactive"):
            invalidate_bot_token_cache()
        return SlackPostResult(ok=False, code=code, error=message)
