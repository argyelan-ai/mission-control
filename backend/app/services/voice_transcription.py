"""The one place a chat channel gets its speech-to-text from.

Telegram wired jarvis_core's STT chain into its bot in P2.4; when Slack voice
arrived it needed the exact same thing. Copying the wiring would have been the
third copy of a "shared" chain (the Jarvis channel itself being the first) —
and the drift between two copies of one rule is precisely what bit this
codebase three times on 2026-07-28. So the wiring lives here and the channels
import it.

Returns None (not a raiser) when STT cannot run at all — no OpenAI key, or the
./jarvis_core mount is absent. Callers treat that as "channel has no ears" and
fall back or tell the operator; they must never crash on it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("mc.voice_transcription")


def get_voice_transcriber():
    """Async ``(audio_bytes, filename) -> str | None``, or None when STT is
    unavailable. Bound to the shared jarvis_core chain — the exact voice path
    the Jarvis channel uses, never a second STT stack.

    Two configurations, same protocol:

      * ``STT_BASE_URL`` set — an OpenAI-compatible server of the operator's
        own (Parakeet v3 on the Mac Mini, see scripts/stt-server). His voice
        never leaves the machine, and NO cloud key is required: demanding one
        here would defeat the reason local STT exists. When both are
        configured, local wins — the URL was set deliberately.
      * otherwise — the OpenAI cloud with ``openai_api_key``, as before.
    """
    from app.config import settings

    local_url = (settings.stt_base_url or "").strip()
    if not local_url and not settings.openai_api_key:
        return None
    try:
        from jarvis_core import brain
    except Exception:  # noqa: BLE001 — the ./jarvis_core mount may be absent
        return None

    async def _transcribe(audio: bytes, filename: str = "voice.ogg") -> str | None:
        try:
            # Called through the module (not a closure-bound name) so tests —
            # and a hot-reloaded jarvis_core — see the current function.
            if local_url:
                return await brain.transcribe_audio(
                    audio,
                    filename=filename,
                    # A local server does not check the key, but the OpenAI
                    # client insists on one — any non-empty value satisfies it
                    # without pretending a real credential exists.
                    api_key=settings.openai_api_key or "local",
                    model=(settings.stt_model or "").strip()
                    or settings.jarvis_stt_model,
                    base_url=local_url,
                )
            return await brain.transcribe_audio(
                audio,
                filename=filename,
                api_key=settings.openai_api_key,
                model=settings.jarvis_stt_model,
            )
        except Exception as e:  # noqa: BLE001 — a bad clip must not kill ingest
            logger.warning("voice transcription failed: %s", type(e).__name__)
            return None

    return _transcribe
