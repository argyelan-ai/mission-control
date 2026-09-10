"""Which provider/model/voice a call uses — pure decision, no plugins (ADR-082).

Deliberately free of livekit imports so this runs in the ordinary backend test
job. The plugin construction that needs livekit stays in voice_worker/main.py.
That split matters: a rule like this used to live in a module that could only
be tested with livekit installed, and its tests then skipped silently in CI
and reported green (see mc-jarvis-voice-runtime-binding-2026-08-21 memory).

The governing rule: **Jarvis does not go silent.** Every unclear state
resolves to something that can still talk. Only a total absence of API keys
raises, because there is genuinely nothing left to try.
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

#: Voice per arm — the two providers' voice names are disjoint ("marin" exists
#: at OpenAI, "ara" at xAI), so a shared variable broke whichever arm did not
#: know the name, and only at connect time.
_VOICE_ENV = {"openai": "VOICE_OPENAI_VOICE_ID", "xai": "VOICE_XAI_VOICE_ID"}
_VOICE_DEFAULT = {"openai": "marin", "xai": "ara"}

#: Fallback model per arm. None = let the plugin pick its own default (xai).
_MODEL_DEFAULT = {"openai": "gpt-realtime-2.1", "xai": None}


def classify_voice_api(provider: str, model: str | None) -> str:
    """Which wire API a (provider, model) pair speaks: "realtime" | "live".

    OpenAI's Live API (v1/live/sessions — WebSocket/WebRTC/SIP, voice model
    decoupled from the backend/"Responses" model via client delegation) is a
    DIFFERENT protocol from the Realtime API this worker's livekit plugin
    speaks — same provider, disjoint wire format (checked live against
    OpenAI's docs, 2026-09-10). ``gpt-live-*`` model names are the only
    signal available for which one a bound runtime row means. Everything
    else — including every xAI model today — speaks the realtime protocol.

    Shared between backend (``voice_runtime.resolve_voice_config``, which
    reports it in the API response) and worker (which reclassifies the
    FINAL resolved provider/model independently, since a key-fallback can
    change the arm after the backend's answer was already fixed) — one rule,
    never two copies that can drift.
    """
    if provider == "openai" and (model or "").strip().lower().startswith("gpt-live"):
        return "live"
    return "realtime"


@dataclass(frozen=True)
class VoiceChoice:
    provider: str
    model: str | None
    voice: str
    source: str  # "mc" | "env" | "env-fallback" | "key-fallback"
    api: str = "realtime"  # "realtime" | "live" (ADR-082 follow-up)

    def as_log(self) -> str:
        return (
            f"voice config: provider={self.provider} "
            f"model={self.model or '<plugin-default>'} voice={self.voice} "
            f"api={self.api} source={self.source}"
        )


def _has_key(provider: str, env: dict[str, str]) -> bool:
    return bool((env.get(_KEY_ENV[provider]) or "").strip())


def resolve_voice_choice(mc_config: dict | None, env: dict[str, str] | None = None) -> VoiceChoice:
    """Decide what this call speaks with.

    ``mc_config`` is the (possibly None on total fetch failure) JSON body of
    GET /api/v1/agent/voice/config. A binding only counts as real when the
    backend actually resolved one — ``runtime_slug`` is set — because
    voice_runtime.resolve_voice_config()'s fail-soft default names "openai"
    even when nothing is bound, and treating THAT as a genuine MC choice would
    silently override an operator's env-only setup with a value that carries
    no information. ``env`` defaults to ``os.environ``.

    Raises RuntimeError only when no API key exists at all.
    """
    env = os.environ if env is None else env
    mc_config = mc_config or {}
    mc_bound = bool((mc_config.get("runtime_slug") or "").strip())

    env_provider = (env.get("VOICE_PROVIDER") or "openai").strip().lower()
    mc_provider = (mc_config.get("provider") or "").strip().lower()
    requested = mc_provider if mc_bound and mc_provider else env_provider
    source = "mc" if mc_bound and mc_provider else "env"

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
    mc_model = (mc_config.get("model") or "").strip() if (mc_bound and mc_provider) else ""
    model = mc_model if chosen == requested and mc_model else None
    env_model = (env.get("VOICE_MODEL") or "").strip() if chosen == env_provider else ""

    # Same rule for voice: MC's voice_id only survives while its arm is still
    # chosen; otherwise the worker's own per-provider env, then the hardcoded
    # default.
    mc_voice = (mc_config.get("voice_id") or "").strip() if (mc_bound and mc_provider) else ""
    voice = mc_voice if chosen == requested and mc_voice else ""

    final_model = model or env_model or _MODEL_DEFAULT[chosen]

    return VoiceChoice(
        provider=chosen,
        model=final_model,
        voice=voice or (env.get(_VOICE_ENV[chosen]) or "").strip() or _VOICE_DEFAULT[chosen],
        source=source,
        # Classified on the FINAL (chosen, final_model) pair, not on whatever
        # MC originally named — a key-fallback can switch arms after MC's
        # answer was fixed, and the api must describe what actually got
        # chosen, not what was asked for.
        api=classify_voice_api(chosen, final_model),
    )
