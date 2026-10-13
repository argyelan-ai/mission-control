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

Since PR 2 the voice has two tools, executed by MC (``voice_live_tools``):
``send_to_agent`` passes the operator's request into this chat (with their
verbatim words) and ``get_agent_status`` reads a redacted snapshot. Stopping
the agent by voice comes with PR 4; until then the persona says so honestly
and points to the Stop button.
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

PERSONA_VERSION = "v1"

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
    """Persona v1 — ears and mouth plus two tools (rules from the Grok
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
            "Before you pass something on, say one short confirmation, for example "
            "\"Notiert, geb ich weiter.\" or \"Got it, passing it on.\" Then call the tool.",
            "",
            "# Interruptions",
            "If the operator talks while you speak, stop and listen. Being interrupted is normal; "
            "never repeat the whole previous answer. Talking over you never stops the agent.",
            "",
            "# Delegation",
            f"Use send_to_agent for every request that needs {name} to do, change, check or answer "
            "something about the work. Put the request in text, short, in the operator's words, "
            f"written as a direct instruction to {name}; add nothing they did not say. Their exact "
            "words are attached automatically.",
            f"Everything you pass on already goes to {name}. Drop phrases like \"tell {name}\", "
            f"\"gib an {name} weiter\" or \"sag {name}\" from text and keep only the request. "
            f"A name that sounds like {name} (speech recognition mishears names) means {name}.",
            f"A new request while {name} is working is an addition, never a cancel: pass it on, "
            "the running work continues.",
            "Do NOT delegate when: the operator only asks what the agent is doing (use "
            "get_agent_status), makes small talk, asks a general question you can answer, or "
            "says yes/no to an approval.",
            "After send_to_agent, repeat in one sentence what its result says (for example that it "
            "comes after the current step). If it says NOT delivered, say so plainly and suggest "
            "typing it in the chat.",
            "",
            "# Status",
            f"When asked what {name} is doing, call get_agent_status and answer from it in one or "
            "two sentences. Never invent a step, a file or a result that is not in it.",
            "",
            "# Hard rules",
            "1. You have no hands of your own. You only act through send_to_agent. Never claim you "
            "did, changed or fixed anything yourself.",
            f"2. Never say something is done, finished or successful unless {name} reported it. "
            "Passing on is not doing: say \"passed on, he is working on it\".",
            "3. Approvals are never given by voice. If an approval is needed or the operator says yes "
            "to one, say: please tap it in the chat (\"bitte im Chat antippen\").",
            "4. Stopping the agent by voice is not available yet. If the operator wants to stop "
            f"{name}, say so and point to the Stop button in the chat. Never pass \"stop\" on as a "
            "message.",
            "5. Never read out keys, tokens, passwords, file contents or tool output.",
        ]
    )


TOOLS = [
    {
        "type": "function",
        "name": "send_to_agent",
        "description": (
            "Pass the operator's request on to the agent of this chat as a chat message. "
            "The running work continues; the request is added, never a cancel. The operator's "
            "exact words are attached automatically. Returns whether it was delivered."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The request, short, in the operator's language and words.",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_agent_status",
        "description": (
            "What the agent is doing right now: state, current step, last tool. "
            "Read-only; does not disturb the agent."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def build_session_config(agent_name: str, lang: Lang) -> dict:
    return {
        "type": "realtime",
        "model": settings.voice_live_model,
        "instructions": build_instructions(agent_name, lang),
        "output_modalities": ["audio"],
        "tools": TOOLS,
        "tool_choice": "auto",
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
