"""Telegram Reports Bot — direct info delivery channel for agent deliverables.

Separate from the approval bot (`telegram_bot.py`) so that command center and
briefing board are semantically distinct:
- Approval bot (`TELEGRAM_BOT_TOKEN`): operator decisions with buttons, URL callbacks
- Reports bot  (`TELEGRAM_REPORTS_BOT_TOKEN`): passive FYI, no buttons, no reply expected

Minimal service — only `send()`. No polling, no button workflows.
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

# Telegram Bot API limit for sendDocument: 50 MB per file
TELEGRAM_DOCUMENT_MAX_BYTES = 50 * 1024 * 1024


class TelegramReportsService:
    """Singleton sender for the reports bot."""

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
        """Sends the message to the operator's reports chat.

        Returns the Telegram API response dict on success, None on skip (not configured)
        or HTTPException on error.
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
            # Description may be a markdown parse error — surfaced for CLI feedback
            return data

        return data

    async def send_photo(
        self,
        photo_path: str,
        caption: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict | None:
        """Sends a single image to the reports chat."""
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
            # Telegram caption is limited to 1024 characters
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
        """Sends a file (PDF/Office/etc.) as a Telegram document.

        Unlike send_photo, the file is not compressed (photos go through
        Telegram's image pipeline). Ideal for PDF, Excel,
        PowerPoint, Word, ZIP, JSON, etc.

        MIME type is detected via the mimetypes module; fallback `application/octet-stream`.
        Max. 50 MB per file (Telegram Bot API limit).
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
        """Sends multiple images as a group (max. 10). Caption goes on the FIRST image.

        Allows up to 10 photos in a grouped message — ideal for
        multi-viewport screenshots (desktop + mobile + scroll positions).
        """
        if not self.configured:
            return None

        import os as _os
        # Filter valid files
        files_to_send = [p for p in photo_paths if _os.path.isfile(p)]
        if not files_to_send:
            logger.warning("send_media_group skipped: keine valid photo files in %s", photo_paths)
            return None
        if len(files_to_send) == 1:
            # Media group requires min. 2 — fall back to single send_photo
            return await self.send_photo(files_to_send[0], caption, parse_mode)
        if len(files_to_send) > 10:
            files_to_send = files_to_send[:10]
            logger.warning("send_media_group: mehr als 10 Bilder, trimmed auf 10")

        import json as _json
        url = TELEGRAM_MEDIA_GROUP_API.format(token=self._token)

        # Build media array — Telegram expects attach://<field-name> references
        media = []
        files_payload = {}
        for i, path in enumerate(files_to_send):
            field = f"photo_{i}"
            entry = {"type": "photo", "media": f"attach://{field}"}
            if i == 0 and caption:
                # Caption only on the first element, Telegram limit 1024
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
            # Close file handles (files_payload has open()'d them)
            for _, file_tuple in files_payload.items():
                try:
                    file_tuple[1].close()
                except Exception:
                    pass


telegram_reports = TelegramReportsService()
