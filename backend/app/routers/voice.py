"""Voice-Agent Token-Endpoint.

Signiert LiveKit-JWTs damit der Browser (der Operator) sich am LiveKit-Server
anmelden und in den Voice-Room joinen kann. Der Python Voice-Worker
(separater Docker-Service) joint denselben Room als Agent und spricht
mit dem Operator via xAI Realtime API.

Auth-Modell:
- POST /voice/token: braucht eingeloggten User (nur der Operator soll sprechen)
- Token-TTL: 1 Stunde
- Room: pro Call frischer Name "voice-{user_id}-{ts}-{rand4}". Reason:
  LiveKit dispatched seinen Worker-Job nur EINMAL pro Room (genauer: beim
  CreateRoom-Event). Wenn der Operator einen Call beendet und reconnected mit
  demselben Room-Namen → Browser joint zwar, aber LiveKit ruft den Worker
  nicht erneut auf → der Operator hört nichts. Plus: xAI Realtime-Session wird im
  Worker-Subprocess pro Job gehalten — wenn 2 Connects sich denselben
  Subprocess teilen, kommt "failed to insert item already exists" Warning
  (Item-IDs kollidieren). Frischer Room = frischer Subprocess = frische
  xAI-Session. Live-Symptom reproduziert 2026-05-14 abends.
- Identity: User-ID als display name
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
# VoiceDisplayCard below. Frontend stack lebt im VoiceDrawer (Voice-Widget).
_VOICE_DISPLAY_CHANNEL = "voice:display"
_VOICE_DISPLAY_KINDS = {"memory", "url", "file", "task"}

LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
# Optionaler Override (z.B. wenn LiveKit hinter ganz separater Domain laeuft).
# Default leer → URL wird dynamisch aus request derived (siehe issue_voice_token).
LIVEKIT_PUBLIC_URL_OVERRIDE = os.environ.get("LIVEKIT_PUBLIC_URL", "")

TOKEN_TTL_SECONDS = 3600  # 1 Stunde

# Path den Caddy zu LiveKit-Signaling proxiert (siehe Caddyfile).
LIVEKIT_SIGNAL_PATH = "/livekit-signal"


class VoiceTokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str


def _build_livekit_jwt(identity: str, room: str) -> str:
    """Baut ein LiveKit-Access-Token (HS256-JWT).

    LiveKit-Specification: https://docs.livekit.io/home/get-started/authentication/
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
    """LiveKit-WebSocket-URL passend zum Request-Origin.

    Wenn der Frontend ueber https://<your-host> kommt, brauchen wir wss://
    (Browsers blocken Mixed-Content). Wenn ueber http://localhost kommt's,
    reicht ws://. Caddy proxiert beides auf /livekit-signal.
    """
    if LIVEKIT_PUBLIC_URL_OVERRIDE:
        return LIVEKIT_PUBLIC_URL_OVERRIDE

    # Trust X-Forwarded-Proto (Caddy setzt es) ueber request.url.scheme.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    ws_scheme = "wss" if proto == "https" else "ws"
    return f"{ws_scheme}://{host}{LIVEKIT_SIGNAL_PATH}"


@router.post("/voice/token", response_model=VoiceTokenResponse)
async def issue_voice_token(
    request: Request,
    current_user: Annotated[User, Depends(require_user)],
):
    """Gibt einen LiveKit-Access-Token + Server-URL für den Browser-Client aus.

    Pro User isolierter Room ('voice-{user_id}'). Voice-Worker joint
    denselben Room als Agent und führt das Gespräch über xAI Realtime.
    URL wird dynamisch aus Request-Origin gebaut (localhost vs Tailscale-Domain).
    """
    user_id_str = str(current_user.id)
    # Frischer Room pro Token-Request: LiveKit dispatched den Worker-Job nur
    # einmal pro Room. Operator-Reconnect mit gleichem Room → kein neuer Job →
    # kein Audio. Mit Timestamp + 4-Hex-Suffix sind Rooms eindeutig auch bei
    # parallel issued Tokens. Frontend ruft /voice/token jedes Mal beim Connect
    # auf — also fresh room == fresh call.
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
    """Card-Push from voice worker → Redis → VoiceDrawer-Stack im Frontend.

    Wenn der Operator sagt "zeig mir das Projekt-Briefing", findet Jarvis im
    Vault den passenden Hit und publisht eine Memory-Card. Frontend rendert
    die Card im Drawer (zwischen BarVisualizer und Controls).

    Card-Schema ist absichtlich locker — `data` darf je nach Kind ganz
    unterschiedliche Felder mitbringen (vault_path bei memory, url bei url,
    filename + size bei file, task_id + status bei task). Frontend macht
    discrimination per ``kind`` und rendert die passende Card-Komponente.

    Whitelist auf 4 Kinds limitiert den Angriffsraum + verhindert typo-
    induzierte silent no-ops auf dem Frontend.
    """

    kind: str
    data: dict
    title: str | None = None  # Optional Anzeige-Titel (sonst aus data abgeleitet)

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
