"""Live voice in the chat — session minting (ADR-091 draft, voice PR 1).

The voice is the chat agent's ears and mouth, never its hands (ADR-089 §6).
The browser opens WebRTC straight to OpenAI Realtime; this module only mints
the short-lived client secret for that, with the whole session config
(model, voice, VAD, persona) fixed server-side. Three properties carry the
design and are pinned by ``tests/test_voice_live.py``:

1. The OpenAI key (``effective_secret("openai_api_key")``: vault first,
   ``.env`` as fallback) never leaves this module — not in a response,
   not in a log line, not in an exception message. Upstream error bodies are
   reduced to a fixed reason code; their text is dropped.
2. The switch (``voice_live_enabled``) and the key are checked together:
   off or no key = the feature does not exist (the router answers 404).
3. The persona is versioned text with a snapshot test, so a wording change is
   a visible diff, never a silent drift.

Since PR 2 the voice has tools, executed by MC (``voice_live_tools``):
``send_to_agent`` passes the operator's request into this chat (with their
verbatim words) and ``get_agent_status`` reads a redacted snapshot. Since PR 4
``stop_agent`` cancels the running turn — only when the operator's last
utterance, verbatim, is a stop command as a whole (MC re-checks the words).
Barge-in (the operator talking over the voice) is semantic VAD with
``interrupt_response``: it cancels and truncates only the voice's own answer
and never reaches the agent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

import httpx
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.user import UserSettings

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

PERSONA_VERSION = "v3"

Lang = Literal["de", "en"]


class VoiceLiveError(Exception):
    """Minting failed. ``reason`` is a fixed code the UI maps to a message;
    ``status`` is the HTTP status the router answers with. Never carries
    upstream text (it could echo request material)."""

    def __init__(self, reason: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


async def _api_key() -> str:
    from app.services.secrets_helper import effective_secret

    return await effective_secret("openai_api_key")


async def is_enabled() -> bool:
    # Switch first: with the feature off there is no reason to read the vault.
    return bool(settings.voice_live_enabled and await _api_key())


#: Used when the operator pinned a voice language in Settings: no switching.
_LANG_PINNED = {
    "de": "The operator chose German as the voice language. Always speak German, even if they speak English or another language.",
    "en": "The operator chose English as the voice language. Always speak English, even if they speak German or another language.",
}

_LANG_LINE = {
    "de": "The operator's app is set to German. Start in German; if they speak English, switch to English.",
    "en": "The operator's app is set to English. Start in English; if they speak German, switch to German.",
}

# ── voice language preference (operator request 13.10.2026) ────────────
# Settings → Profile lets the operator pin the voice to one language
# instead of following the UI locale (the default). ``AUTO`` is the stored
# sentinel for "keep following the UI" — the only value that is not itself
# a key of ``_LANG_LINE``. Adding a language: extend ``_LANG_LINE``, the
# ``Lang`` literal above, and the frontend list (``messages/*.json`` +
# ``VoiceLanguageField`` in ``frontend-v2/src/app/settings/page.tsx``).
VOICE_LANGUAGE_SETTING_KEY = "voice_language"
VOICE_LANGUAGE_AUTO = "auto"
VOICE_LANGUAGE_CHOICES: tuple[str, ...] = (VOICE_LANGUAGE_AUTO, *_LANG_LINE.keys())


def resolve_voice_language(preference: str | None, ui_lang: Lang) -> tuple[Lang, Lang | None]:
    """``(lang, transcription_hint)``. An explicit preference (a real
    language, not "auto") overrides the UI locale and also hands the
    realtime session a transcription language hint, so the voice answers in
    that language even when the UI itself is in the other one. "auto",
    unset, or an unknown/garbage stored value keeps today's behaviour:
    follow the UI, with no hint (so the operator can still switch at will)."""
    if preference in _LANG_LINE:
        lang = preference  # type: ignore[assignment]
        return lang, lang
    return ui_lang, None


async def load_voice_language_preference(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Reads the operator's stored "Voice language" choice (reuses the
    generic per-user preference store, ``GET/PUT /api/v1/settings/{key}``
    — ``routers/settings.py``). Returns ``None`` when nothing was saved yet
    (treated the same as "auto" by ``resolve_voice_language``)."""
    row = (
        await session.exec(
            select(UserSettings).where(
                UserSettings.user_id == user_id,
                UserSettings.key == VOICE_LANGUAGE_SETTING_KEY,
            )
        )
    ).first()
    if row is None or not isinstance(row.value, dict):
        return None
    value = row.value.get("language")
    return value if isinstance(value, str) else None


def build_instructions(agent_name: str, lang: Lang, pinned: bool = False) -> str:
    """Persona v3 — ears and mouth plus three tools (rules from the Grok
    behaviour table and bauplan B2); since voice PR 3 it also reads MC's
    progress items (``voice_progress``) and takes their corrections; since
    voice PR 4 it stops the agent on a pure stop command and points every
    approval to a tap in the chat."""
    name = (agent_name or "the agent").strip()[:80]
    return "\n".join(
        [
            "# Personality",
            f"You are the voice of {name}, an agent in Mission Control. You are its ears and mouth, "
            "not its hands: the agent does the work in the chat, you talk with the operator.",
            "Calm, friendly, direct. Speak like a capable colleague on the phone.",
            "",
            "# Language and length",
            (_LANG_PINNED if pinned else _LANG_LINE)[lang],
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
            "# Stopping",
            "Call stop_agent only when the operator's whole utterance is a stop command: \"Stopp\", "
            "\"Stop\", \"Halt\", \"Abbrechen\", \"Hör auf\", optionally with \"bitte\"/\"jetzt\" or "
            f"{name}'s name (\"Stopp {name}\"). Call it at once, without saying anything first.",
            "A stop word with more words is NOT a stop: \"Stopp den Container\", \"stop the tests\", "
            "\"Stopp und mach dann X\" are requests — pass them on with send_to_agent. If unsure, ask back.",
            "Say \"Gestoppt.\" (or \"Stopped.\") only when the stop_agent result says stopped=true and "
            "confirmed=true; otherwise say exactly what its result tells you. Stopping cancels his current "
            "step; nothing is undone.",
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
            f"Default: pass it on. The operator opened this mic to talk to {name}, so every "
            f"request, instruction or question goes to {name} via send_to_agent — also ones "
            "phrased as if to you (\"answer only with X\", \"what is 2+2\").",
            "Do NOT delegate only when: the operator asks what the agent is doing (use "
            "get_agent_status), greets you or checks the line (\"hallo\", \"hörst du mich?\"), "
            "says only a stop command (use stop_agent), or says yes/no to an approval.",
            "After send_to_agent, repeat in one sentence what its result says (for example that it "
            "comes after the current step). If it says NOT delivered, say so plainly and suggest "
            "typing it in the chat.",
            "",
            "# Status",
            f"When asked what {name} is doing, answer from the newest [MC progress] item in one or two "
            "sentences; if there is none or it is old, call get_agent_status. Never invent a step, a "
            "file or a result that is not in them.",
            "",
            "# Progress updates",
            "While the call runs, Mission Control adds system items that start with \"[MC progress\". "
            "They come from MC, not from the operator, and are true at the moment they arrive.",
            "When you are asked to respond right after such an item and the operator said nothing new, "
            "tell them the gist in ONE short sentence in their language (for example \"Er hat "
            "angefangen.\" or \"He is running the tests now.\"). No paths, no code, no long titles; "
            "never repeat an update you already told.",
            "Otherwise keep the items in mind for status questions and do not recite them. Never "
            "interrupt the operator to tell progress; they always go first.",
            "Only an item with agent_final=true means the agent finished; tell its shortened reply in "
            "one or two sentences and say the details are in the chat.",
            "An item that starts with \"[MC correction\" means you said something wrong: correct it "
            "at once in one short sentence.",
            "",
            "# Hard rules",
            "1. You have no hands of your own. You only act through send_to_agent. Never claim you "
            "did, changed or fixed anything yourself.",
            f"2. Never say something is done, finished or successful unless {name} reported it "
            "(an [MC progress] item with agent_final=true). Passing on is not doing: say "
            "\"passed on, he is working on it\".",
            "3. Approvals are never given by voice. When an [MC progress] item says he waits for an "
            "approval, tell the operator in one sentence what he wants and ask them to tap it in the "
            "chat (\"Bitte im Chat antippen.\"). If the operator says yes or no to it, do not pass it "
            "on: say again \"bitte im Chat antippen\".",
            "4. You stop the agent only through stop_agent, only on a pure stop command. Never pass a "
            "pure \"Stopp\" on as a message, and never claim he stopped before stop_agent confirms it.",
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
        "name": "stop_agent",
        "description": (
            "Cancel the agent's running step, like the Stop button in the chat. Call ONLY when the "
            "operator's whole last utterance is a stop command (\"Stopp\", \"Stop\", \"Halt\", "
            "\"Abbrechen\", \"Stopp <agent name>\"). Mission Control checks their exact words and "
            "refuses anything else. Returns whether he stopped."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
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


def build_session_config(agent_name: str, lang: Lang, transcription_lang: Lang | None = None) -> dict:
    transcription: dict[str, str] = {"model": settings.jarvis_stt_model}
    if transcription_lang is not None:
        # Only set on an explicit voice-language preference (never for
        # "auto"): a fixed hint would stop the operator switching between
        # German and English mid-call, which "auto" exists to preserve.
        transcription["language"] = transcription_lang
    return {
        "type": "realtime",
        "model": settings.voice_live_model,
        "instructions": build_instructions(agent_name, lang, pinned=transcription_lang is not None),
        "output_modalities": ["audio"],
        "tools": TOOLS,
        "tool_choice": "auto",
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "transcription": transcription,
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


async def mint_client_secret(
    agent_name: str, lang: Lang, transcription_lang: Lang | None = None
) -> MintedSession:
    api_key = await _api_key() if settings.voice_live_enabled else ""
    if not api_key:
        raise VoiceLiveError("disabled", 404)
    body = {
        "expires_after": {"anchor": "created_at", "seconds": CLIENT_SECRET_TTL_S},
        "session": build_session_config(agent_name, lang, transcription_lang),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=_transport()) as client:
            resp = await client.post(
                OPENAI_CLIENT_SECRETS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
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
    if not isinstance(secret, str) or not secret or secret == api_key:
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
