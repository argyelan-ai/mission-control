"""Which provider/model/voice a call uses — pure decision, no plugins (ADR-074).

Deliberately free of livekit imports so this runs in the ordinary backend test
job. The plugin construction that needs livekit stays in voice_worker/main.py.
That split is not cosmetic: the rule "MC beats env" and the never-go-silent
fallbacks used to live in a module that could only be tested with livekit
installed, so their tests skipped silently in CI and reported green.

The governing rule: **Jarvis does not go silent.** Every unclear state resolves
to something that can still talk. Only a total absence of API keys raises,
because there is genuinely nothing left to try.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("jarvis_core.voice_provider")

#: Providers this worker can build. Also applied to whatever MC sends: a value
#: the backend knows but this (possibly older) image does not must never reach
#: the plugin factory.
PROVIDERS = ("openai", "xai")

_KEY_ENV = {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}

#: Voice per arm. The two providers' voice names are disjoint — "cedar" exists
#: at OpenAI, "ara" at xAI — so one shared variable broke whichever arm did not
#: know the name, and only at connect time.
_VOICE_ENV = {"openai": "VOICE_OPENAI_VOICE_ID", "xai": "VOICE_XAI_VOICE_ID"}
_VOICE_DEFAULT = {"openai": "marin", "xai": "ara"}

#: Fallback model per arm. None = let the plugin pick its own default (xai).
_MODEL_DEFAULT = {"openai": "gpt-realtime-2.1", "xai": None}


@dataclass(frozen=True)
class VoiceChoice:
    provider: str
    model: str | None
    voice: str
    source: str  # "mc" | "env" | "env-fallback" | "key-fallback"

    def as_log(self) -> str:
        return (
            f"voice provider={self.provider} model={self.model or '<plugin-default>'} "
            f"voice={self.voice} source={self.source}"
        )


def _has_key(provider: str, env: dict[str, str]) -> bool:
    return bool((env.get(_KEY_ENV[provider]) or "").strip())


def resolve_voice_choice(
    provider: str | None = None,
    model: str | None = None,
    env: dict[str, str] | None = None,
) -> VoiceChoice:
    """Decide what this call speaks with.

    ``provider``/``model`` come from the runtime binding in MC (None when the
    backend did not answer or nothing is bound). ``env`` defaults to os.environ.

    Raises RuntimeError only when no API key exists at all.
    """
    env = os.environ if env is None else env

    env_provider = (env.get("VOICE_PROVIDER") or "openai").strip().lower()
    requested = (provider or env_provider).strip().lower()
    source = "mc" if provider else "env"

    if requested not in PROVIDERS:
        fallback = env_provider if env_provider in PROVIDERS else "openai"
        logger.error(
            "unknown voice provider %r (from %s) — falling back to %r",
            requested, source, fallback,
        )
        requested, source = fallback, "env-fallback"

    chosen = requested
    if not _has_key(chosen, env):
        other = next((p for p in PROVIDERS if p != chosen and _has_key(p, env)), None)
        if other is None:
            raise RuntimeError(
                "Neither OPENAI_API_KEY nor XAI_API_KEY is set — the voice worker "
                "cannot reach any realtime provider."
            )
        logger.error(
            "voice provider %r has no API key — switching to %r so Jarvis stays "
            "reachable. Set the missing key, or rebind the runtime in MC.",
            chosen, other,
        )
        chosen, source = other, "key-fallback"

    # A model name only ever belongs to the arm it was chosen for. Two ways it
    # can become foreign, and both end the same way — the provider rejects an
    # unknown model at connect, i.e. a silent Jarvis:
    #   * MC named a model, then we switched arms (missing key, or a provider
    #     this image does not know yet).
    #   * VOICE_MODEL is set for the env arm, and we are not on that arm.
    # So a model survives only while its own arm is still the chosen one.
    if chosen != (provider or env_provider).strip().lower():
        model = None
    env_model = (env.get("VOICE_MODEL") or "").strip() if chosen == env_provider else ""

    return VoiceChoice(
        provider=chosen,
        model=(model or "").strip() or env_model or _MODEL_DEFAULT[chosen],
        voice=(env.get(_VOICE_ENV[chosen]) or "").strip() or _VOICE_DEFAULT[chosen],
        source=source,
    )
