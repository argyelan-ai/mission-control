"""Channel-neutral room lifecycle fan-out (ADR-072).

Two events in MC touch every channel's conversation rooms, and both used to
call Telegram directly from neutral code:

  * a task reaches ``done``  -> the channel marks its room done
    (Telegram: rename to ``✓ …`` + close the thread),
  * the daily janitor tick   -> long-finished rooms are removed
    (Marks Regel: 30 Tage).

Both fan out over the ACTIVE channels here, so ``task_lifecycle``, ``tasks``,
``agent_task_status`` and ``main``'s purge loop no longer know a channel by
name. Neither entry point ever raises: a chat problem must not fail a task
completion or kill a periodic job.

Note the gate: these paths use ``enabled_chat_adapters()`` (switched on), NOT
``sendable_chat_adapters()`` (switched on + credentials). That is the gate the
Telegram implementation always had — its room bookkeeping runs on the feature
flag alone and degrades inside the adapter when the transport is unavailable.
"""
from __future__ import annotations

import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.chat_adapter import enabled_chat_adapters

logger = logging.getLogger("mc.chat_rooms")


async def handle_task_done(session: AsyncSession, task) -> None:
    """Ein Task ist ``done`` — jeder aktive Kanal raeumt seinen Raum auf."""
    try:
        adapters = enabled_chat_adapters()
    except Exception as e:  # noqa: BLE001 — nie den Task-Abschluss kippen
        logger.warning("chat_rooms.handle_task_done: Kanalauswahl fehlgeschlagen: %s", e)
        return
    for adapter in adapters:
        try:
            await adapter.handle_task_done(session, task)
        except Exception as e:  # noqa: BLE001 — nie den Task-Abschluss kippen
            logger.warning(
                "chat_rooms.handle_task_done via %s fehlgeschlagen: %s", adapter.key, e
            )


async def purge_rooms_tick(older_than_days: int = 30) -> int:
    """Ein Purge-Lauf des taeglichen Jobs ueber alle aktiven Kanaele.

    Gibt die Gesamtzahl entfernter Raeume zurueck; schluckt jeden Fehler — der
    Job darf nie sterben, und kein aktiver Kanal ist einfach 0."""
    try:
        adapters = enabled_chat_adapters()
    except Exception as e:  # noqa: BLE001 — der periodische Job darf nie sterben
        logger.warning("chat_rooms.purge_rooms_tick: Kanalauswahl fehlgeschlagen: %s", e)
        return 0
    purged = 0
    for adapter in adapters:
        try:
            purged += await adapter.purge_rooms(older_than_days)
        except Exception as e:  # noqa: BLE001
            logger.warning("chat_rooms.purge_rooms via %s fehlgeschlagen: %s", adapter.key, e)
    return purged
