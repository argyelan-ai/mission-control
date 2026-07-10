"""Jarvis Telegram-Inbound (ADR-061).

Beantwortet Text- und Sprachnachrichten des Operators aus dem Telegram-Command-Chat
mit dem geteilten ``JarvisBrain`` (OpenAI Function-Calling). Dieselbe Persona,
dieselben Tools wie der Voice-Kanal — nur ueber Text statt Realtime-Sprache.

Sicherheit:
- **Hartes chat_id-Gate:** nur Nachrichten aus ``settings.telegram_chat_id``
  werden verarbeitet. Alles andere wird geloggt und ignoriert (kein Reply an
  Fremde — keine Info-Leaks, keine Bot-Ausnutzung durch Dritte).
- Tool-Calls laufen ueber den agent-scoped MC-API-Pfad mit dem Jarvis-Token
  (kein Auth-Bypass, keine Direkt-DB).

Feature-Gate: aktiv nur wenn ``JARVIS_TELEGRAM_ENABLED=true`` UND ein
``OPENAI_API_KEY`` UND ein ``JARVIS_AGENT_TOKEN`` gesetzt sind. Sonst bleibt das
Verhalten exakt wie zuvor (nur Approval-URL-Buttons).
"""

from __future__ import annotations

import json
import logging

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger("mc.jarvis_telegram")

# History: letzte Turns pro Chat, damit Jarvis Kontext ueber Nachrichten hinweg
# behaelt. TTL 24h, gedeckelt auf die letzten MAX_HISTORY_MESSAGES Eintraege.
_HISTORY_TTL = 24 * 3600
MAX_HISTORY_MESSAGES = 20

# jarvis_core liegt im Repo-Root und wird im Backend-Image als Live-Mount
# (/app/jarvis_core) bereitgestellt. Der Import ist bewusst weich: fehlt das
# Package (reines GHCR-Image ohne Mount), bleibt das Inbound-Feature inaktiv
# statt den Backend-Start zu gefaehrden.
try:
    from jarvis_core import mc_client
    from jarvis_core.brain import JarvisBrain, transcribe_audio
    from jarvis_core.channels import TELEGRAM
    from jarvis_core.persona import build_instructions

    _JARVIS_CORE_OK = True
except Exception as _exc:  # noqa: BLE001 — degrade gracefully if package absent
    logger.warning("jarvis_core not importable — Telegram inbound disabled: %s", _exc)
    _JARVIS_CORE_OK = False


def _history_key(chat_id: str) -> str:
    return f"mc:jarvis:tg:history:{chat_id}"


class JarvisTelegramHandler:
    """Orchestriert eine eingehende Telegram-Nachricht → Jarvis-Antwort.

    Der ``bot`` ist die ``TelegramBotService``-Singleton-Instanz und liefert das
    Telegram-Transport (send_message, getFile-Download). So bleiben alle
    Telegram-API-Details in einem Modul.
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @property
    def enabled(self) -> bool:
        return bool(
            _JARVIS_CORE_OK
            and settings.jarvis_telegram_enabled
            and settings.openai_api_key
            and settings.jarvis_agent_token
            and settings.telegram_chat_id
        )

    # ── History (Redis) ──────────────────────────────────────────────────

    async def _load_history(self, chat_id: str) -> list[dict[str, str]]:
        try:
            redis = await get_redis()
            raw = await redis.get(_history_key(chat_id))
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001 — history is best-effort
            logger.warning("Jarvis history load failed: %s", e)
            return []

    async def _save_history(self, chat_id: str, turns: list[dict[str, str]]) -> None:
        try:
            redis = await get_redis()
            trimmed = turns[-MAX_HISTORY_MESSAGES:]
            await redis.set(_history_key(chat_id), json.dumps(trimmed), ex=_HISTORY_TTL)
        except Exception as e:  # noqa: BLE001 — history is best-effort
            logger.warning("Jarvis history save failed: %s", e)

    # ── Main entry ───────────────────────────────────────────────────────

    async def handle_message(self, message: dict) -> None:
        """Verarbeitet EIN Telegram ``message``-Update (Text oder Voice).

        Hartes chat_id-Gate zuerst — nur der Operator-Chat wird bedient.
        """
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id or chat_id != str(settings.telegram_chat_id):
            from_user = message.get("from", {}) or {}
            logger.warning(
                "Jarvis inbound from unauthorized chat %s (user=%s) — ignored",
                chat_id or "?",
                from_user.get("username", "unknown"),
            )
            return  # NIE an Fremde antworten

        text = (message.get("text") or "").strip()
        voice_prefix = ""

        if not text and message.get("voice"):
            transcript = await self._transcribe_voice(message["voice"])
            if transcript is None:
                await self.bot.send_message(
                    "Ich konnte die Sprachnachricht nicht verstehen — versuch's nochmal "
                    "oder tipp's kurz."
                )
                return
            text = transcript
            # Transkript echoen, damit der Operator STT-Fehler sofort sieht.
            voice_prefix = f"🎤 Verstanden: „{transcript}“\n\n"

        if not text:
            # Weder Text noch Voice (z.B. Sticker/Photo) — still ignorieren.
            logger.info("Jarvis inbound: non-text/voice message ignored")
            return

        try:
            reply = await self._run_brain(chat_id, text)
        except Exception as e:  # noqa: BLE001 — never crash the poll loop
            logger.exception("Jarvis brain failed")
            await self.bot.send_message(
                "Da ist bei mir gerade was schiefgelaufen — versuch's gleich nochmal."
            )
            return

        await self.bot.send_message((voice_prefix + reply).strip() or "…")

    # ── Voice → text ─────────────────────────────────────────────────────

    async def _transcribe_voice(self, voice: dict) -> str | None:
        """Laedt die Telegram-Sprachnotiz (ogg/opus) + transkribiert via OpenAI."""
        file_id = voice.get("file_id")
        if not file_id:
            return None
        audio = await self.bot.get_file_bytes(file_id)
        if not audio:
            logger.warning("Jarvis voice: file download failed for %s", file_id)
            return None
        try:
            return await transcribe_audio(
                audio,
                filename="voice.ogg",
                api_key=settings.openai_api_key,
                model=settings.jarvis_stt_model,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Jarvis voice transcription failed: %s", e)
            return None

    # ── Brain ────────────────────────────────────────────────────────────

    async def _run_brain(self, chat_id: str, text: str) -> str:
        history = await self._load_history(chat_id)
        brain = JarvisBrain(
            api_key=settings.openai_api_key,
            model=settings.jarvis_text_model,
            client=mc_client,
            channel=TELEGRAM,
            system_prompt=build_instructions(TELEGRAM),
        )
        try:
            result = await brain.respond(text, history=history)
        finally:
            await brain.aclose()

        # History fortschreiben (nur sichtbare user/assistant-Turns).
        await self._save_history(chat_id, history + result.new_turns)

        reply = result.text
        if not reply and result.actions:
            # Modell lieferte keinen Text, hat aber Aktionen ausgefuehrt — knappe
            # Fallback-Bestaetigung, damit der Operator nie eine leere Antwort sieht.
            names = ", ".join(a["name"] for a in result.actions)
            reply = f"Erledigt ({names})."
        return reply or "Ok."
