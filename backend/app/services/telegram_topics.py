"""Telegram forum topic <-> MC thread lifecycle.

Ein Telegram-Thema (`message_thread_id`) bildet 1:1 auf einen MC-Thread ab
(`threads.telegram_topic_id`). Dieser Service legt Themen an, benennt sie um und
loescht alte — idempotent und ausfallsicher: ein Telegram-Fehler blockiert nie
Agentenarbeit, er degradiert.

Live-Fakten (27.07.2026, am Produktiv-Bot gemessen):
  * `createForumTopic`/`editForumTopic`/`deleteForumTopic` funktionieren im
    Privatchat (Bot API 10.2, seit 14.07.2026).
  * `closeForumTopic` funktioniert NICHT (`the chat is not a supergroup forum`) —
    wird hier bewusst nicht benutzt.
  * Der Chat wird erst zum Forum, wenn der Nutzer das erste Thema anlegt. Vorher
    scheitert `createForumTopic` mit `the chat is not a forum` → sauber
    degradieren (Thema NULL lassen), nicht werfen.
  * Ratenlimit ~1 Nachricht/Sekunde pro Chat; HTTP 429 darf nicht crashen.

Der Telegram-Client wird injiziert (siehe `ForumTopicClient`), damit Tests ohne
Netz laufen. `TelegramForumClient` ist die produktive Implementierung.
"""
import logging
from datetime import datetime, timedelta
from typing import Protocol

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.board import Project
from app.models.task import Task
from app.models.thread import Thread

logger = logging.getLogger(__name__)

# Das Allgemein-Thema hat in Telegram keine eigene message_thread_id — Nachrichten
# dorthin gehen ohne den Parameter. Wir reservieren 0 als Sentinel dafuer; er wird
# nie in die DB geschrieben (der Thread bleibt telegram_topic_id IS NULL).
GENERAL_TOPIC_ID = 0

# createForumTopic.name akzeptiert 1..128 Zeichen.
TELEGRAM_TOPIC_NAME_MAX = 128

_DONE_PREFIX = "✓ "


# ── Fehlertypen ───────────────────────────────────────────────────────────

class TelegramTopicError(Exception):
    """Basisklasse fuer Fehler der Forum-Topic-API."""


class TelegramNotAForumError(TelegramTopicError):
    """`createForumTopic` bevor der Nutzer den Chat zum Forum gemacht hat."""


class TelegramRateLimitError(TelegramTopicError):
    """HTTP 429 — zu viele Anfragen an diesen Chat."""


# ── Client-Kontrakt + produktive Implementierung ──────────────────────────

class ForumTopicClient(Protocol):
    """Was der Service vom Telegram-Client braucht — in Tests gefaelscht."""

    async def create_forum_topic(self, name: str) -> int: ...
    async def edit_forum_topic(self, message_thread_id: int, name: str) -> None: ...
    async def delete_forum_topic(self, message_thread_id: int) -> None: ...


class TelegramForumClient:
    """Produktiver Client — spricht die Bot-API via httpx an.

    Uebersetzt Telegram-Antworten in die typisierten Fehler oben, damit der
    Service `not a forum` von 429 von echten Fehlern unterscheiden kann.
    """

    def __init__(self, *, token: str | None = None, chat_id: str | int | None = None):
        self._token = token or settings.telegram_bot_token
        self._chat_id = chat_id if chat_id is not None else settings.telegram_chat_id
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    async def _call(self, method: str, payload: dict) -> dict:
        client = await self._get_client()
        try:
            resp = await client.post(self._url(method), data=payload)
        except httpx.HTTPError as e:  # Netzfehler
            raise TelegramTopicError(f"{method} transport error: {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise TelegramTopicError(f"{method} non-JSON response") from e
        if data.get("ok"):
            return data.get("result") or {}
        description = str(data.get("description", ""))
        error_code = data.get("error_code")
        if resp.status_code == 429 or error_code == 429:
            raise TelegramRateLimitError(description or "Too Many Requests")
        if "not a forum" in description.lower():
            raise TelegramNotAForumError(description)
        raise TelegramTopicError(f"{method} failed: {description}")

    async def create_forum_topic(self, name: str) -> int:
        result = await self._call(
            "createForumTopic", {"chat_id": self._chat_id, "name": name}
        )
        return int(result["message_thread_id"])

    async def edit_forum_topic(self, message_thread_id: int, name: str) -> None:
        await self._call(
            "editForumTopic",
            {"chat_id": self._chat_id, "message_thread_id": message_thread_id, "name": name},
        )

    async def delete_forum_topic(self, message_thread_id: int) -> None:
        await self._call(
            "deleteForumTopic",
            {"chat_id": self._chat_id, "message_thread_id": message_thread_id},
        )


# ── Hilfsfunktionen ───────────────────────────────────────────────────────

def _truncate(name: str, limit: int = TELEGRAM_TOPIC_NAME_MAX) -> str:
    name = name.strip()
    if len(name) <= limit:
        return name
    return name[: limit - 1].rstrip() + "…"  # … zaehlt als ein Zeichen


def _is_general_thread(thread: Thread) -> bool:
    """Der Allgemein-Chat ist der DM-Thread (Mark <-> Boss) — er bekommt nie ein
    eigenes Forum-Thema, sondern schreibt in den Chat-Stamm."""
    return thread.kind == "dm"


async def _topic_title_for_thread(session: AsyncSession, thread: Thread) -> str:
    """Titel eines Themas: `#<short-id> <Task-Titel>` bzw. Projektname, sonst der
    Thread-Titel. (Es gibt keine fortlaufende Task-Nummer im Schema — die 8-stellige
    Kurz-ID ist die Codebase-Konvention, vgl. agent_comments.py.)"""
    if thread.task_id is not None:
        task = (await session.exec(select(Task).where(Task.id == thread.task_id))).one_or_none()
        if task is not None:
            return _truncate(f"#{str(task.id)[:8]} {task.title}")
    if thread.project_id is not None:
        project = (
            await session.exec(select(Project).where(Project.id == thread.project_id))
        ).one_or_none()
        if project is not None:
            return _truncate(project.name)
    if thread.title:
        return _truncate(thread.title)
    return _truncate(f"Thread {str(thread.id)[:8]}")


# ── Oeffentliche Service-API ──────────────────────────────────────────────

async def ensure_topic_for_thread(
    session: AsyncSession, thread: Thread, client: ForumTopicClient
) -> int | None:
    """Gib das Telegram-Thema dieses Threads zurueck, lege es bei Bedarf an.

    Idempotent (ein zweiter Aufruf legt nichts Neues an). Liefert:
      * die gespeicherte ID, wenn der Thread schon eins hat,
      * GENERAL_TOPIC_ID (0) fuer das Allgemein-Thema (nie via API angelegt),
      * die frisch angelegte ID sonst,
      * None, wenn Telegram (noch) nicht bereit ist (`not a forum`, 429, Fehler) —
        der Thread bleibt ungemappt, nichts wirft.
    """
    if thread.telegram_topic_id is not None:
        return thread.telegram_topic_id
    if _is_general_thread(thread):
        return GENERAL_TOPIC_ID

    title = await _topic_title_for_thread(session, thread)
    try:
        topic_id = await client.create_forum_topic(title)
    except TelegramNotAForumError:
        logger.info(
            "Telegram-Chat ist noch kein Forum; Thread %s bleibt ungemappt", thread.id
        )
        return None
    except TelegramRateLimitError:
        logger.warning(
            "Telegram hat createForumTopic ratenlimitiert; Thread %s bleibt ungemappt",
            thread.id,
        )
        return None
    except TelegramTopicError as e:
        logger.warning("createForumTopic fuer Thread %s fehlgeschlagen: %s", thread.id, e)
        return None

    thread.telegram_topic_id = topic_id
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    return topic_id


async def mark_topic_done(
    session: AsyncSession, thread: Thread, client: ForumTopicClient
) -> None:
    """Benenne das Thema zu `✓ …` um (erledigte Aufgabe). No-op fuer Threads ohne
    eigenes Thema (still gelaufen) und fuer das Allgemein-Thema. Das Haekchen wird
    nie doppelt gesetzt."""
    topic_id = thread.telegram_topic_id
    if topic_id is None or topic_id == GENERAL_TOPIC_ID:
        return

    title = await _topic_title_for_thread(session, thread)
    base = title[len(_DONE_PREFIX):] if title.startswith(_DONE_PREFIX) else title
    done_title = _truncate(f"{_DONE_PREFIX}{base}")
    try:
        await client.edit_forum_topic(topic_id, done_title)
    except TelegramTopicError as e:
        logger.warning("editForumTopic fuer Thread %s fehlgeschlagen: %s", thread.id, e)


async def purge_old_topics(
    session: AsyncSession, client: ForumTopicClient, older_than_days: int = 30
) -> int:
    """Loesche Themen geschlossener Threads, die aelter als `older_than_days` sind
    (`deleteForumTopic`), und setze danach `telegram_topic_id` auf NULL. Der
    Verlauf bleibt im Web vollstaendig. Gibt die Anzahl geloeschter Themen zurueck.

    Ein Loesch-Fehler laesst die ID stehen, damit ein spaeterer Lauf es erneut
    versucht."""
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    stmt = select(Thread).where(
        Thread.telegram_topic_id.is_not(None),
        Thread.telegram_topic_id != GENERAL_TOPIC_ID,
        Thread.closed_at.is_not(None),
        Thread.closed_at < cutoff,
    )
    threads = (await session.exec(stmt)).all()

    purged = 0
    for thread in threads:
        try:
            await client.delete_forum_topic(thread.telegram_topic_id)
        except TelegramTopicError as e:
            logger.warning(
                "deleteForumTopic fuer Thread %s fehlgeschlagen: %s", thread.id, e
            )
            continue
        thread.telegram_topic_id = None
        session.add(thread)
        purged += 1

    if purged:
        await session.commit()
    return purged
