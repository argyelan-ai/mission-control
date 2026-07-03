"""Shared helper functions for the entire backend.

Provides timezone-aware datetime helpers and a safe task wrapper,
so asyncio.create_task() can no longer fail silently.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
import re

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Returns the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def strip_tz(dt: datetime) -> datetime:
    """Removes tzinfo for DB comparisons (PostgreSQL stores naive datetimes)."""
    return dt.replace(tzinfo=None)


def ensure_aware(dt: datetime) -> datetime:
    """Ensures that a datetime object is timezone-aware.

    Naive date values are interpreted as UTC. Already timezone-aware
    date values remain unchanged.

    Args:
        dt: The datetime object to check.

    Returns:
        datetime: The given date with UTC tzinfo if it was previously naive,
        otherwise unchanged.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


_background_tasks: set[asyncio.Task] = set()


def create_tracked_task(
    coro,
    name: str | None = None,
) -> asyncio.Task:
    """Creates a tracked background task with error logging.

    The task is held in a global set so it doesn't disappear due to
    garbage collection. Unhandled exceptions are logged on completion
    instead of being silently swallowed.

    Args:
        coro: Awaitable to start as a background task.
        name: Optional task name for logging and debugging.

    Returns:
        asyncio.Task: The created and already registered background task.
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
