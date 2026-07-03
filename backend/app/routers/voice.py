"""Voice agent token endpoint.

Signs LiveKit JWTs so the browser (the operator) can authenticate against the
LiveKit server and join the voice room. The Python voice worker (a separate
Docker service) joins the same room as an agent and talks to the operator
via the xAI Realtime API.

Auth model:
- POST /voice/token: requires a logged-in user (only the operator should talk)
- Token TTL: 1 hour
- Room: a fresh name per call, "voice-{user_id}-{ts}-{rand4}". Reason:
  LiveKit dispatches its worker job only ONCE per room (more precisely: on the
  CreateRoom event). If the operator ends a call and reconnects with the same
  room name → the browser joins fine, but LiveKit doesn't invoke the worker
  again → the operator hears nothing. Also: the xAI Realtime session is held
  in the worker subprocess per job — if 2 connects share the same subprocess,
  a "failed to insert item already exists" warning shows up (item IDs
  collide). Fresh room = fresh subprocess = fresh xAI session. Live symptom
  reproduced on the evening of 2026-05-14.
- Identity: user ID as display name
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import jwt
from pydantic import BaseModel, field_validator

from app.auth import require_agent, require_user
from app.models.user import User
from app.redis_client import get_redis
from app.scopes import Scope, require_scope
from app.utils import slugify

logger = logging.getLogger("mc.voice")

router = APIRouter(prefix="/api/v1", tags=["voice"])

# Whitelisted filter keys for the voice graph-highlight bridge. Any other key
# from xAI function-tool output is rejected with 422. Prevents typo-induced
# silent no-ops on the frontend (e.g. xAI sends 'fitler' instead of 'filter')
# AND limits the attack surface of the Redis-published payload.
_ALLOWED_HIGHLIGHT_KEYS = {"agent", "type", "tag", "project", "date_from", "date_to"}

# Redis pub/sub channel that the /vault/voice-highlight WS endpoint forwards
# verbatim to connected frontend clients (M.4 T2 wired the subscriber side).
_VOICE_HIGHLIGHT_CHANNEL = "voice:graph-highlight"

# Redis pub/sub channel for Jarvis "Display"-Cards (M.5: voice-display).
# Card-types: memory|url|file|task. Card-payload schema validated by
# VoiceDisplayCard below. Frontend stack lives in the VoiceDrawer (voice widget).
_VOICE_DISPLAY_CHANNEL = "voice:display"
_VOICE_DISPLAY_KINDS = {"memory", "url", "file", "task"}

LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
# Optional override (e.g. when LiveKit runs behind a completely separate domain).
# Default empty → URL is derived dynamically from the request (see issue_voice_token).
LIVEKIT_PUBLIC_URL_OVERRIDE = os.environ.get("LIVEKIT_PUBLIC_URL", "")

TOKEN_TTL_SECONDS = 3600  # 1 hour

# Path Caddy proxies to LiveKit signaling (see Caddyfile).
LIVEKIT_SIGNAL_PATH = "/livekit-signal"


class VoiceTokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str


def _build_livekit_jwt(identity: str, room: str) -> str:
    """Builds a LiveKit access token (HS256 JWT).

    LiveKit specification: https://docs.livekit.io/home/get-started/authentication/
    Required claims: iss (= api_key), sub (= identity), nbf, exp, video.roomJoin, video.room.
    """
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LiveKit not configured (missing LIVEKIT_API_KEY / LIVEKIT_API_SECRET in env).",
        )

    now = int(time.time())
    payload = {
        "iss": LIVEKIT_API_KEY,
        "sub": identity,
        "nbf": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "name": identity,
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    return jwt.encode(payload, LIVEKIT_API_SECRET, algorithm="HS256")


def _derive_ws_url(request: Request) -> str:
    """LiveKit WebSocket URL matching the request origin.

    If the frontend comes in over https://<your-host>, we need wss://
    (browsers block mixed content). If it comes over http://localhost,
    ws:// is enough. Caddy proxies both to /livekit-signal.
    """
    if LIVEKIT_PUBLIC_URL_OVERRIDE:
        return LIVEKIT_PUBLIC_URL_OVERRIDE

    # Trust X-Forwarded-Proto (Caddy sets it) over request.url.scheme.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    ws_scheme = "wss" if proto == "https" else "ws"
    return f"{ws_scheme}://{host}{LIVEKIT_SIGNAL_PATH}"


@router.post("/voice/token", response_model=VoiceTokenResponse)
async def issue_voice_token(
    request: Request,
    current_user: Annotated[User, Depends(require_user)],
):
    """Issues a LiveKit access token + server URL for the browser client.

    Room isolated per user ('voice-{user_id}'). The voice worker joins the
    same room as an agent and carries out the conversation via xAI Realtime.
    URL is built dynamically from the request origin (localhost vs Tailscale domain).
    """
    user_id_str = str(current_user.id)
    # Fresh room per token request: LiveKit dispatches the worker job only
    # once per room. Operator reconnect with the same room → no new job →
    # no audio. With a timestamp + 4-hex suffix, rooms stay unique even for
    # tokens issued in parallel. The frontend calls /voice/token every time it
    # connects — so fresh room == fresh call.
    room = f"voice-{user_id_str}-{int(time.time())}-{uuid.uuid4().hex[:4]}"
    identity = current_user.email or user_id_str

    token = _build_livekit_jwt(identity=identity, room=room)
    return VoiceTokenResponse(
        token=token,
        url=_derive_ws_url(request),
        room=room,
        identity=identity,
    )


# ── Voice → Graph-Highlight Bridge (M.4 T5) ───────────────────────────────────


class VoiceGraphHighlight(BaseModel):
    """Filter payload from voice worker → Redis → frontend 3D graph.

    Values may be `str` (single-select) or `list[str]` (multi-select OR-match).
    Keys are whitelisted to avoid silent no-ops from xAI function-tool typos
    and to limit what a compromised voice-agent token could publish.
    """

    filter: dict[str, str | list[str]]

    @field_validator("filter")
    @classmethod
    def _validate_filter_keys(cls, v: dict) -> dict:
        if not v:
            raise ValueError("filter must contain at least one key")
        unknown = set(v.keys()) - _ALLOWED_HIGHLIGHT_KEYS
        if unknown:
            raise ValueError(f"unknown filter keys: {sorted(unknown)}")
        for k, val in v.items():
            if not isinstance(val, (str, list)):
                raise ValueError(f"filter[{k}] must be str or list[str]")
            if isinstance(val, list) and not all(isinstance(x, str) for x in val):
                raise ValueError(f"filter[{k}] list must contain only strings")
        return v


@router.post(
    "/voice/graph-highlight",
    dependencies=[Depends(require_scope(Scope.VAULT_READ))],
)
async def voice_graph_highlight(
    payload: VoiceGraphHighlight,
    current_agent=Depends(require_agent),
):
    """Publish a graph-highlight command to the frontend via Redis.

    Called by the voice worker (M.4 T5) when xAI invokes the `highlight_graph`
    function-tool. The /vault/voice-highlight WS endpoint (M.4 T2) is already
    subscribed to the `voice:graph-highlight` channel and forwards the JSON
    payload verbatim to connected frontend clients, which apply the filter to
    the Three.js memory graph (frontend wiring in M.4 T9, out of scope here).

    Fail-soft: if the Redis publish raises, we return `{ok: False, error: ...}`
    with HTTP 200 instead of a 500 so the voice worker can phrase a graceful
    fallback ("konnte den Graph gerade nicht hervorheben") rather than crashing
    the tool-call flow.
    """
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    requested_by = slugify(current_agent.name) if current_agent else "voice"
    message = {
        "filter": payload.filter,
        "requested_at": now_iso,
        "requested_by": requested_by,
    }

    try:
        redis = await get_redis()
        await redis.publish(_VOICE_HIGHLIGHT_CHANNEL, json.dumps(message))
    except Exception as exc:  # noqa: BLE001 — fail-soft for voice UX
        logger.warning("voice graph-highlight publish failed: %s", exc)
        return {"ok": False, "error": str(exc), "published_at": None}

    return {"ok": True, "published_at": now_iso}


# ── Voice → Display-Card Bridge (M.5 voice-display) ────────────────────────


class VoiceDisplayCard(BaseModel):
    """Card push from voice worker → Redis → VoiceDrawer stack in the frontend.

    When the operator says "show me the project briefing", Jarvis finds the
    matching hit in the vault and publishes a memory card. The frontend renders
    the card in the drawer (between BarVisualizer and Controls).

    The card schema is intentionally loose — `data` can carry entirely
    different fields depending on kind (vault_path for memory, url for url,
    filename + size for file, task_id + status for task). The frontend
    discriminates by ``kind`` and renders the matching card component.

    Whitelisting to 4 kinds limits the attack surface + prevents typo-
    induced silent no-ops on the frontend.
    """

    kind: str
    data: dict
    title: str | None = None  # Optional display title (otherwise derived from data)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in _VOICE_DISPLAY_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(_VOICE_DISPLAY_KINDS)}, got {v!r}"
            )
        return v

    @field_validator("data")
    @classmethod
    def _validate_data(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("data must be a dict")
        # Cap payload size to avoid the Drawer rendering pages of text.
        # 4 KB is plenty for any of the four kinds.
        if len(json.dumps(v)) > 4096:
            raise ValueError("data payload exceeds 4KB")
        return v


@router.post(
    "/voice/display",
    dependencies=[Depends(require_scope(Scope.VAULT_READ))],
)
async def voice_display(
    payload: VoiceDisplayCard,
    current_agent=Depends(require_agent),
):
    """Publish a display-card command to the frontend via Redis.

    Called by the voice worker when xAI invokes a ``show_*`` function-tool.
    The /vault/voice-display WS endpoint subscribes ``voice:display`` and
    forwards the JSON verbatim to connected clients, which append the card
    to the VoiceDrawer-Stack with a stagger animation.

    Fail-soft like graph-highlight: Redis hiccup returns 200 with ok=False
    so the voice worker can narrate a graceful fallback rather than crash
    the tool flow.
    """
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    requested_by = slugify(current_agent.name) if current_agent else "voice"
    message = {
        "kind": payload.kind,
        "data": payload.data,
        "title": payload.title,
        "requested_at": now_iso,
        "requested_by": requested_by,
    }

    try:
        redis = await get_redis()
        await redis.publish(_VOICE_DISPLAY_CHANNEL, json.dumps(message))
    except Exception as exc:  # noqa: BLE001 — fail-soft for voice UX
        logger.warning("voice display publish failed: %s", exc)
        return {"ok": False, "error": str(exc), "published_at": None}

    return {"ok": True, "published_at": now_iso, "kind": payload.kind}
