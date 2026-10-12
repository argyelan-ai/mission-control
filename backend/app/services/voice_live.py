"""Live voice in the chat — session minting (ADR-091 draft, voice PR 1).

The voice is the chat agent's ears and mouth, never its hands (ADR-089 §6).
The browser opens WebRTC straight to OpenAI Realtime; this module only mints
the short-lived client secret for that, with the whole session config
(model, voice, VAD, persona) fixed server-side. Three properties carry the
design and are pinned by ``tests/test_voice_live.py``:

1. ``settings.openai_api_key`` never leaves this module — not in a response,
   not in a log line, not in an exception message. Upstream error bodies are
   reduced to a fixed reason code; their text is dropped.
2. The switch (``voice_live_enabled``) and the key are checked together:
   off or no key = the feature does not exist (the router answers 404).
3. The persona is versioned text with a snapshot test, so a wording change is
   a visible diff, never a silent drift.

PR 1 has no tools: the voice can listen and talk, it cannot pass anything to
the agent yet (``send_to_agent`` arrives in PR 2). The persona says so
honestly instead of pretending.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from app.config import settings

logger = logging.getLogger("mc.voice_live")

OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
# Where the browser posts its SDP offer with the client secret (unified
# WebRTC interface). Sent to the browser so the URL lives in one place.
OPENAI_CALLS_URL = "https://api.openai.com/v1/realtime/calls"

# The secret only has to be valid when the browser creates the call; a live
# call keeps running after it expires. 60 s covers a slow phone handshake and
# keeps a leaked secret nearly worthless.
CLIENT_SECRET_TTL_S = 60

# Input transcription drives the "per Stimme" bubbles with the OpenAI
# speech-to-text model MC already configures (JARVIS_STT_MODEL). Language
# left unset on purpose: the operator switches between German and English.

PERSONA_VERSION = "v0"

Lang = Literal["de", "en"]


class VoiceLiveError(Exception):
    """Minting failed. ``reason`` is a fixed code the UI maps to a message;
    ``status`` is the HTTP status the router answers with. Never carries
    upstream text (it could echo request material)."""

    def __init__(self, reason: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def is_enabled() -> bool:
    return bool(settings.voice_live_enabled and settings.openai_api_key)


_LANG_LINE = {
    "de": "The operator's app is set to German. Start in German; if they speak English, switch to English.",
    "en": "The operator's app is set to English. Start in English; if they speak German, switch to German.",
}


def build_instructions(agent_name: str, lang: Lang) -> str:
    """Persona v0 — ears and mouth, no hands yet (rules from the Grok
    behaviour table, ``grok-verhalten.md`` §4, and bauplan B2)."""
    name = (agent_name or "the agent").strip()[:80]
    return "\n".join(
        [
            "# Personality",
            f"You are the voice of {name}, an agent in Mission Control. You are its ears and mouth, "
            "not its hands: the agent does the work in the chat, you talk with the operator.",
            "Calm, friendly, direct. Speak like a capable colleague on the phone.",
            "",
            "# Language and length",
            _LANG_LINE[lang],
            "Always answer in the language the operator just used. One or two short sentences. "
            "No lists, no markdown, no code read aloud.",
            "",
            "# Backchannel",
            "When the operator gives you something to pass on, acknowledge it in a few words.",
            "",
            "# Interruptions",
            "If the operator talks while you speak, stop and listen. Being interrupted is normal; "
            "never repeat the whole previous answer.",
            "",
            "# What you can do in this version",
            "You cannot pass messages to the agent yet and you cannot see its progress. "
            f"If the operator asks you to tell {name} something or to do work, say honestly that "
            "passing on by voice is not built yet and that they should type it in the chat.",
            "Small talk and general questions you may answer yourself.",
            "",
            "# Hard rules",
            "1. You have no hands. Never claim you did, started, sent or changed anything.",
            "2. Never say something is done, finished or successful. You cannot know it.",
            f"3. Never invent what {name} is doing or has done. If asked, say you cannot see it yet.",
            "4. Approvals are never given by voice. If the operator says yes to an approval, "
            "ask them to tap it in the chat.",
            "5. Never read out keys, tokens, passwords or file contents.",
        ]
    )


def build_session_config(agent_name: str, lang: Lang) -> dict:
    return {
        "type": "realtime",
        "model": settings.voice_live_model,
        "instructions": build_instructions(agent_name, lang),
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": settings.jarvis_stt_model},
                "turn_detection": {
                    "type": "semantic_vad",
                    "create_response": True,
                    "interrupt_response": True,
                },
            },
            "output": {"voice": settings.voice_live_voice},
        },
    }


@dataclass(frozen=True)
class MintedSession:
    client_secret: str
    expires_at: int
    model: str
    voice: str
    calls_url: str = OPENAI_CALLS_URL


def _transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests (httpx.MockTransport). ``None`` = real network."""
    return None


async def mint_client_secret(agent_name: str, lang: Lang) -> MintedSession:
    if not is_enabled():
        raise VoiceLiveError("disabled", 404)
    body = {
        "expires_after": {"anchor": "created_at", "seconds": CLIENT_SECRET_TTL_S},
        "session": build_session_config(agent_name, lang),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=_transport()) as client:
            resp = await client.post(
                OPENAI_CLIENT_SECRETS_URL,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        # The exception text can contain the request — log only its type.
        logger.warning("voice_live mint: upstream unreachable (%s)", type(exc).__name__)
        raise VoiceLiveError("upstream_unreachable", 502) from None

    if resp.status_code != 200:
        code = ""
        try:
            err = resp.json().get("error") or {}
            code = str(err.get("code") or err.get("type") or "")[:64]
        except (ValueError, AttributeError):
            pass
        logger.warning("voice_live mint: upstream %s %s", resp.status_code, code or "-")
        if resp.status_code == 429 and code == "insufficient_quota":
            raise VoiceLiveError("no_credits", 402)
        if resp.status_code == 429:
            raise VoiceLiveError("rate_limited", 429)
        if resp.status_code in (401, 403):
            raise VoiceLiveError("upstream_auth", 502)
        raise VoiceLiveError("upstream_error", 502)

    try:
        data = resp.json()
        secret = data["value"]
        expires_at = int(data.get("expires_at") or time.time() + CLIENT_SECRET_TTL_S)
    except (ValueError, KeyError, TypeError):
        logger.warning("voice_live mint: unexpected upstream body")
        raise VoiceLiveError("upstream_error", 502) from None
    # Guard against ever handing the long-lived key to a browser: a client
    # secret is a different, short value ("ek_…").
    if not isinstance(secret, str) or not secret or secret == settings.openai_api_key:
        logger.warning("voice_live mint: upstream returned no usable client secret")
        raise VoiceLiveError("upstream_error", 502)
    logger.info(
        "voice_live mint: ok model=%s persona=%s lang=%s", settings.voice_live_model, PERSONA_VERSION, lang
    )
    return MintedSession(
        client_secret=secret,
        expires_at=expires_at,
        model=settings.voice_live_model,
        voice=settings.voice_live_voice,
    )
