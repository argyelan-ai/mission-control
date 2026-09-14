"""Resolve Jarvis' bound voice provider for the voice-worker (ADR-082).

The voice-worker is a separate docker-compose service. It cannot see the
backend's settings object, and it must never be handed an API key — it
already holds its own in its env. So the only thing that crosses the process
boundary is the answer to one question: which provider, which model, and
which voice did the operator bind?

Design rule: this function never raises and never returns key material. The
worker calls it at the start of every call; if it fails or nothing is bound,
Jarvis must still speak using its env defaults rather than go silent.
"""
from __future__ import annotations

import logging
import os

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.harness_compat import VOICE_RUNTIME_TYPES

logger = logging.getLogger(__name__)

# jarvis_core liegt im Repo-Root (Live-Mount im Backend-Image, ADR-061) — wie
# jarvis_briefing.py/jarvis_telegram.py importiert dieses Modul es weich statt
# das Fehlen des Mounts in einen 500 auf /voice/config zu verwandeln (der
# Docstring oben verspricht "nie raisen"). classify_voice_api ist eine reine
# Regel ohne Seiteneffekte; faellt der Mount, uebernimmt die lokale Kopie
# exakt dieselbe Klassifikation — siehe jarvis_core.voice_provider fuer die
# kanonische Version + Begruendung.
try:
    from jarvis_core.voice_provider import classify_voice_api
except ImportError as _exc:  # pragma: no cover — only fires without the mount
    logger.warning(
        "jarvis_core not importable — using the local classify_voice_api "
        "fallback: %s", _exc,
    )

    def classify_voice_api(provider: str, model: str | None) -> str:
        if provider == "openai" and (model or "").strip().lower().startswith("gpt-live"):
            return "live"
        return "realtime"

#: Voice IDs are disjunct per provider (OpenAI's "marin" means nothing to xAI's
#: plugin, and vice versa) — a shared env var would silently break whichever
#: arm isn't currently selected. One env var per provider keeps them isolated.
_VOICE_ID_ENV = {
    "openai": "VOICE_OPENAI_VOICE_ID",
    "xai": "VOICE_XAI_VOICE_ID",
}


async def resolve_voice_config(agent: Agent, session: AsyncSession) -> dict:
    """The bound voice provider for ``agent``, fail-soft.

    Always returns ``{"provider", "model", "voice_id", "runtime_slug",
    "updated_at", "api"}``. When nothing usable is bound, ``provider`` falls
    back to "openai" (the voice-worker's own default) with the rest ``None``
    — the caller (voice_worker) then keeps its env defaults and just skips
    the override. A warning is logged for every fallback case so drift is
    visible in the backend log even though the HTTP response stays a plain
    200.

    ``api`` names the wire protocol the bound model actually speaks
    ("realtime" | "live", see ``jarvis_core.voice_provider.classify_voice_api``
    — today only "realtime" has a transport in the worker image; "live"
    (OpenAI's Live API, disjoint from Realtime) is classified so a caller
    can refuse a live-only binding loudly instead of connecting to the wrong
    endpoint. This response never decides what the worker DOES about it —
    that call is the worker's, since it knows its own image's capabilities.
    """
    if agent.runtime_id is None:
        logger.warning(
            "resolve_voice_config: agent %s has no runtime binding — worker "
            "falls back to its env defaults",
            agent.slug or agent.name,
        )
        return _fallback()

    runtime = await session.get(Runtime, agent.runtime_id)
    if runtime is None:
        # FK is ON DELETE SET NULL, so this is a narrow race rather than a
        # lasting state, but the worker still needs an answer it can act on.
        logger.warning(
            "resolve_voice_config: agent %s's bound runtime %s is gone",
            agent.slug or agent.name, agent.runtime_id,
        )
        return _fallback()

    provider = VOICE_RUNTIME_TYPES.get((runtime.runtime_type or "").strip())
    if provider is None:
        # Someone bound Jarvis to a chat runtime. is_compatible() should have
        # refused that at switch time; if it ever gets through, refusing here
        # is what keeps the worker from passing a nonsense provider name to
        # the plugin factory.
        logger.warning(
            "resolve_voice_config: agent %s is bound to runtime %s of type "
            "%r, which is not a voice runtime",
            agent.slug or agent.name, runtime.slug, runtime.runtime_type,
        )
        return _fallback()

    if runtime.enabled is False:
        logger.warning(
            "resolve_voice_config: runtime %s is disabled", runtime.slug,
        )
        return _fallback()

    voice_id_env = _VOICE_ID_ENV.get(provider)
    voice_id = (os.environ.get(voice_id_env, "").strip() or None) if voice_id_env else None
    model = (runtime.model_identifier or "").strip() or None

    return {
        "provider": provider,
        "model": model,
        "voice_id": voice_id,
        "runtime_slug": runtime.slug,
        "updated_at": runtime.updated_at.isoformat() if runtime.updated_at else None,
        "api": classify_voice_api(provider, model),
    }


def _fallback() -> dict:
    return {
        "provider": "openai",
        "model": None,
        "voice_id": None,
        "runtime_slug": None,
        "updated_at": None,
        "api": "realtime",
    }
