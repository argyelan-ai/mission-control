"""Telegram Reports Bot — direkter Info-Delivery-Channel fuer Agent-Deliverables.

Getrennt vom Approval-Bot (`telegram_bot.py`), damit Kommandozentrale und
Briefing-Board semantisch unterschieden sind:
- Approval-Bot (`TELEGRAM_BOT_TOKEN`): Operator-Decisions mit Buttons, URL-Callbacks
- Reports-Bot  (`TELEGRAM_REPORTS_BOT_TOKEN`): passive FYI, keine Buttons, keine Reply-Erwartung

Minimaler Service — nur `send()`. Kein Polling, keine Button-Workflows.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger("mc.telegram_reports")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"
TELEGRAM_MEDIA_GROUP_API = "https://api.telegram.org/bot{token}/sendMediaGroup"
TELEGRAM_DOCUMENT_API = "https://api.telegram.org/bot{token}/sendDocument"

# Telegram Bot API Limit fuer sendDocument: 50 MB pro Datei
TELEGRAM_DOCUMENT_MAX_BYTES = 50 * 1024 * 1024


class TelegramReportsService:
    """Singleton-Sender fuer den Reports-Bot."""

    def __init__(self) -> None:
        self._token = settings.telegram_reports_bot_token or None
        self._chat_id = settings.telegram_reports_chat_id or None

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(
        self,
        text: str,
        *,
        parse_mode: str = "HTML",
        disable_link_preview: bool = True,
    ) -> dict | None:
        """Sendet die Nachricht an den Reports-Chat des Operators.

        Returns das Telegram API-Response-Dict bei Erfolg, None bei Skip (nicht konfiguriert)
        oder HTTPException bei Fehler.
        """
        if not self.configured:
            logger.debug(
                "Reports-Bot nicht konfiguriert — "
                "TELEGRAM_REPORTS_BOT_TOKEN + TELEGRAM_REPORTS_CHAT_ID setzen."
            )
            return None

        url = TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_link_preview,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload)

        data = response.json()
        if not data.get("ok"):
            logger.warning(
                "Reports-Bot send fehlgeschlagen: %s",
                data.get("description", "unknown error"),
            )
            # Description kann Markdown-Parse-Error sein — hochgereicht fuer CLI-Feedback
            return data

        return data

    async def send_photo(
        self,
        photo_path: str,
        caption: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict | None:
        """Sendet ein einzelnes Bild an den Reports-Chat."""
        if not self.configured:
            logger.debug("send_photo skipped: Reports-Bot nicht konfiguriert")
            return None

        import os as _os
        if not _os.path.isfile(photo_path):
            logger.warning("send_photo skipped: Datei existiert nicht: %s", photo_path)
            return None

        url = TELEGRAM_PHOTO_API.format(token=self._token)
        data: dict = {"chat_id": self._chat_id}
        if caption:
            # Telegram-Caption ist auf 1024 Zeichen begrenzt
            data["caption"] = caption[:1024]
            data["parse_mode"] = parse_mode

        async with httpx.AsyncClient(timeout=30.0) as client:
            with open(photo_path, "rb") as f:
                files = {"photo": (_os.path.basename(photo_path), f, "image/png")}
                response = await client.post(url, data=data, files=files)

        result = response.json()
        if not result.get("ok"):
            logger.warning(
                "Reports-Bot send_photo fehlgeschlagen (%s): %s",
                photo_path, result.get("description", "unknown"),
            )
        return result

    async def send_document(
        self,
        document_path: str,
        caption: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict | None:
        """Sendet eine Datei (PDF/Office/etc.) als Telegram-Document.

        Im Gegensatz zu send_photo wird die Datei nicht komprimiert (Photos
        durchlaufen Telegram's Image-Pipeline). Ideal fuer PDF, Excel,
        PowerPoint, Word, ZIP, JSON, etc.

        MIME-Type wird via mimetypes-Modul erkannt; Fallback `application/octet-stream`.
        Max. 50 MB pro Datei (Telegram Bot API Limit).
        """
        if not self.configured:
            logger.debug("send_document skipped: Reports-Bot nicht konfiguriert")
            return None

        import os as _os
        import mimetypes as _mimetypes

        if not _os.path.isfile(document_path):
            logger.warning("send_document skipped: Datei existiert nicht: %s", document_path)
            return None

        size = _os.path.getsize(document_path)
        if size > TELEGRAM_DOCUMENT_MAX_BYTES:
            logger.warning(
                "send_document skipped: Datei %s ist %d Bytes (> %d Bytes Telegram-Limit)",
                document_path, size, TELEGRAM_DOCUMENT_MAX_BYTES,
            )
            return {
                "ok": False,
                "description": (
                    f"file too large: {size} bytes exceeds Telegram limit "
                    f"of {TELEGRAM_DOCUMENT_MAX_BYTES} bytes (50 MB)"
                ),
            }

        mime_type, _ = _mimetypes.guess_type(document_path)
        if not mime_type:
            mime_type = "application/octet-stream"

        url = TELEGRAM_DOCUMENT_API.format(token=self._token)
        data: dict = {"chat_id": self._chat_id}
        if caption:
            data["caption"] = caption[:1024]
            data["parse_mode"] = parse_mode

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(document_path, "rb") as f:
                files = {"document": (_os.path.basename(document_path), f, mime_type)}
                response = await client.post(url, data=data, files=files)

        result = response.json()
        if not result.get("ok"):
            logger.warning(
                "Reports-Bot send_document fehlgeschlagen (%s): %s",
                document_path, result.get("description", "unknown"),
            )
        return result

    async def send_media_group(
        self,
        photo_paths: list[str],
        caption: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict | None:
        """Sendet mehrere Bilder als Gruppe (max. 10). Caption geht auf das ERSTE Bild.

        Erlaubt bis zu 10 Photos in einer gruppierten Nachricht — ideal fuer
        multi-viewport Screenshots (desktop + mobile + scroll-positionen).
        """
        if not self.configured:
            return None

        import os as _os
        # Valide Dateien filtern
        files_to_send = [p for p in photo_paths if _os.path.isfile(p)]
        if not files_to_send:
            logger.warning("send_media_group skipped: keine valid photo files in %s", photo_paths)
            return None
        if len(files_to_send) == 1:
            # Media-Group verlangt min. 2 — fallback auf single send_photo
            return await self.send_photo(files_to_send[0], caption, parse_mode)
        if len(files_to_send) > 10:
            files_to_send = files_to_send[:10]
            logger.warning("send_media_group: mehr als 10 Bilder, trimmed auf 10")

        import json as _json
        url = TELEGRAM_MEDIA_GROUP_API.format(token=self._token)

        # Media-Array bauen — Telegram erwartet attach://<field-name> Referenzen
        media = []
        files_payload = {}
        for i, path in enumerate(files_to_send):
            field = f"photo_{i}"
            entry = {"type": "photo", "media": f"attach://{field}"}
            if i == 0 and caption:
                # Caption nur auf erstem Element, Telegram-Limit 1024
                entry["caption"] = caption[:1024]
                entry["parse_mode"] = parse_mode
            media.append(entry)
            files_payload[field] = (_os.path.basename(path), open(path, "rb"), "image/png")

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    url,
                    data={"chat_id": self._chat_id, "media": _json.dumps(media)},
                    files=files_payload,
                )
            result = response.json()
            if not result.get("ok"):
                logger.warning(
                    "Reports-Bot send_media_group fehlgeschlagen: %s",
                    result.get("description", "unknown"),
                )
            return result
        finally:
            # File-Handles schliessen (files_payload hat open()'d)
            for _, file_tuple in files_payload.items():
                try:
                    file_tuple[1].close()
                except Exception:
                    pass


telegram_reports = TelegramReportsService()
