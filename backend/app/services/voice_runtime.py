"""Resolve Jarvis' bound voice provider for the voice-worker (ADR-074).

The voice-worker is a separate container. It cannot see the backend's settings
object, and it must never be handed an API key — it already holds its own in
its env. So the only thing that crosses the process boundary is the answer to
one question: which provider and which model did the operator bind?

Design rule: this function never raises and never returns key material. The
worker calls it at the start of every call; if it fails, Jarvis must still
speak using its env defaults rather than go silent.
"""
from __future__ import annotations

import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.harness_compat import VOICE_RUNTIME_TYPES

logger = logging.getLogger(__name__)


async def resolve_voice_config(agent: Agent, session: AsyncSession) -> dict:
    """The bound voice provider, or a reason why there is none.

    Returns ``{"ok": True, "provider", "model", "runtime_slug", "display_name"}``
    or ``{"ok": False, "reason": ...}``. Callers treat every ok=False the same
    way — fall back to env — but the reason is carried so the worker's log says
    which of them happened.
    """
    if agent.runtime_id is None:
        return {"ok": False, "reason": "no_runtime_bound"}

    runtime = await session.get(Runtime, agent.runtime_id)
    if runtime is None:
        # FK is ON DELETE SET NULL, so this is a narrow race rather than a
        # lasting state, but the worker still needs an answer it can act on.
        return {"ok": False, "reason": "runtime_missing"}

    provider = VOICE_RUNTIME_TYPES.get((runtime.runtime_type or "").strip())
    if provider is None:
        # Someone bound Jarvis to a chat runtime. is_compatible() should have
        # refused that at switch time; if it ever gets through, refusing here
        # is what keeps the worker from passing a nonsense provider name to
        # the plugin factory.
        logger.warning(
            "agent %s is bound to runtime %s of type %r, which is not a voice runtime",
            agent.slug or agent.name, runtime.slug, runtime.runtime_type,
        )
        return {"ok": False, "reason": "not_a_voice_runtime"}

    if runtime.enabled is False:
        return {"ok": False, "reason": "runtime_disabled"}

    return {
        "ok": True,
        "provider": provider,
        "model": (runtime.model_identifier or "").strip() or None,
        "runtime_slug": runtime.slug,
        "display_name": runtime.display_name,
    }
