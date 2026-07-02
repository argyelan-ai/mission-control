"""Gemeinsame Hilfsfunktionen fuer das gesamte Backend.

Stellt timezone-aware datetime-Helfer und einen sicheren Task-Wrapper bereit,
damit kein asyncio.create_task() mehr still fehlschlagen kann.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
import re

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Gibt die aktuelle UTC-Zeit als timezone-aware datetime zurueck."""
    return datetime.now(timezone.utc)


def strip_tz(dt: datetime) -> datetime:
    """Entfernt tzinfo fuer DB-Vergleiche (PostgreSQL speichert naive datetimes)."""
    return dt.replace(tzinfo=None)


def ensure_aware(dt: datetime) -> datetime:
    """Stellt sicher, dass ein datetime-Objekt timezone-aware ist.

    Naive Datumswerte werden als UTC interpretiert. Bereits timezone-aware
    Datumswerte bleiben unveraendert.

    Args:
        dt: Das zu pruefende datetime-Objekt.

    Returns:
        datetime: Das uebergebene Datum mit UTC-tzinfo, falls es zuvor naiv war,
        sonst unveraendert.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


_background_tasks: set[asyncio.Task] = set()


def create_tracked_task(
    coro,
    name: str | None = None,
) -> asyncio.Task:
    """Erzeugt einen verfolgten Background-Task mit Fehler-Logging.

    Der Task wird in einem globalen Set gehalten, damit er nicht durch
    Garbage Collection verschwindet. Unbehandelte Exceptions werden beim
    Abschluss geloggt statt still verschluckt.

    Args:
        coro: Awaitable, das als Background-Task gestartet wird.
        name: Optionaler Task-Name fuer Logging und Debugging.

    Returns:
        asyncio.Task: Der erzeugte und bereits registrierte Background-Task.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            task_name = t.get_name() or "unnamed"
            logger.error(
                "Background task '%s' failed: %s",
                task_name,
                exc,
                exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task


def slugify(text: str, max_length: int = 80) -> str:
    """Convert text to URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    text = text.strip("-")
    return text[:max_length]
