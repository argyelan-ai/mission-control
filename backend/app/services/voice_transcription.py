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
    unavailable. Bound to the shared jarvis_core chain (`jarvis_stt_model`) —
    the exact voice path the Jarvis channel uses, never a second STT stack.
    """
    from app.config import settings

    if not settings.openai_api_key:
        return None
    try:
        from jarvis_core.brain import transcribe_audio
    except Exception:  # noqa: BLE001 — the ./jarvis_core mount may be absent
        return None

    async def _transcribe(audio: bytes, filename: str = "voice.ogg") -> str | None:
        try:
            return await transcribe_audio(
                audio,
                filename=filename,
                api_key=settings.openai_api_key,
                model=settings.jarvis_stt_model,
            )
        except Exception as e:  # noqa: BLE001 — a bad clip must not kill ingest
            logger.warning("voice transcription failed: %s", type(e).__name__)
            return None

    return _transcribe
