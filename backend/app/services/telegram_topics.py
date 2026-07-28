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
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
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


async def _load_task(session: AsyncSession, task_id) -> Task | None:
    if task_id is None:
        return None
    return (await session.exec(select(Task).where(Task.id == task_id))).one_or_none()


async def _load_project(session: AsyncSession, project_id) -> Project | None:
    if project_id is None:
        return None
    return (await session.exec(select(Project).where(Project.id == project_id))).one_or_none()


# Ein Subtask-Baum ist flach (Phase -> Task), aber ein kaputter Datenstand koennte
# zyklisch sein. Die Kette bricht darum hart ab, statt endlos zu laufen.
_MAX_PARENT_DEPTH = 10


async def _resolve_topic_owner(session: AsyncSession, thread: Thread):
    """Wem gehoert das Thema, in dem dieser Thread redet? (Marks Regeln)

    Kette:
      1. Gehoert der Task zu einem Projekt -> dem PROJEKT (ein Thema fuer alle
         seine Tasks).
      2. Ist es ein Subtask -> dem ELTERNTASK (dessen Thread bzw. dessen Projekt,
         darum rekursiv).
      3. Sonst -> dem Thread selbst (Ad-hoc-Task = eigenes Thema).

    Gibt das Objekt zurueck, an dem `telegram_topic_id` haengt: eine `Project`-
    oder eine `Thread`-Zeile.
    """
    task = await _load_task(session, thread.task_id)

    # Projekt-Bezug: am Task (kanonisch) oder am Thread (Side-Thread eines Projekts).
    project_id = task.project_id if task is not None else None
    if project_id is None:
        project_id = thread.project_id
    project = await _load_project(session, project_id)
    if project is not None:
        return project

    # Subtask: das Thema gehoert dem Elterntask.
    depth = 0
    while task is not None and task.parent_task_id is not None and depth < _MAX_PARENT_DEPTH:
        parent = await _load_task(session, task.parent_task_id)
        if parent is None:
            break
        parent_project = await _load_project(session, parent.project_id)
        if parent_project is not None:
            return parent_project
        if parent.parent_task_id is None:
            parent_thread = (
                await session.exec(select(Thread).where(Thread.task_id == parent.id))
            ).first()
            # Hat der Elterntask (noch) keinen Thread, faellt der Subtask auf sein
            # eigenes Thema zurueck — besser ein eigenes als gar keins.
            return parent_thread if parent_thread is not None else thread
        # Der Elterntask ist selbst Subtask — weiter die Kette hoch.
        task = parent
        depth += 1

    return thread


async def _topic_title_for_owner(session: AsyncSession, owner) -> str:
    if isinstance(owner, Project):
        return _truncate(owner.name)
    return await _topic_title_for_thread(session, owner)


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

    Der Besitzer des Themas folgt Marks Kette (siehe `_resolve_topic_owner`):
    Projekt-Task -> Projekt-Thema, Subtask -> Thema des Elterntasks, sonst ->
    eigenes Task-Thema.

    Idempotent (ein zweiter Aufruf legt nichts Neues an). Liefert:
      * die gespeicherte ID, wenn der Besitzer schon eine hat,
      * GENERAL_TOPIC_ID (0) fuer das Allgemein-Thema (nie via API angelegt),
      * die frisch angelegte ID sonst,
      * None, wenn Telegram (noch) nicht bereit ist (`not a forum`, 429, Fehler) —
        der Thread bleibt ungemappt, nichts wirft.
    """
    if thread.telegram_topic_id is not None:
        return thread.telegram_topic_id
    if _is_general_thread(thread):
        return GENERAL_TOPIC_ID

    owner = await _resolve_topic_owner(session, thread)
    if owner.telegram_topic_id is not None:
        return owner.telegram_topic_id

    owner_id = owner.id  # vor dem Commit festhalten: nach einem Rollback ist das
    #                      Attribut expired und ein Zugriff loeste Lazy-IO aus.
    owner_model = type(owner)
    title = await _topic_title_for_owner(session, owner)
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

    owner.telegram_topic_id = topic_id
    session.add(owner)
    try:
        await session.commit()
    except IntegrityError:
        # Guertel-und-Hosentraeger (P2.2-Review): zwei gleichzeitige Aufrufe fuer
        # denselben Besitzer koennen je ein Telegram-Thema anlegen. Scheitert unser
        # Commit am Unique-Constraint (uq_threads_telegram_topic_id bzw.
        # uq_projects_telegram_topic_id), reissen wir nicht mit einem 500 ab,
        # sondern lesen den bereits persistierten (Gewinner-)Wert und verwenden
        # ihn. Ist keiner da (Fremd-Kollision: dieselbe ID an einem anderen
        # Besitzer — bei echtem Telegram unmoeglich, da IDs global eindeutig
        # sind), degradieren wir wie bei „nicht bereit".
        await session.rollback()
        winner = (
            await session.exec(select(owner_model).where(owner_model.id == owner_id))
        ).one().telegram_topic_id
        logger.warning(
            "createForumTopic-Kollision fuer Besitzer %s (Thema %s); bestehender Wert=%s",
            owner_id, topic_id, winner,
        )
        return winner
    await session.refresh(owner)
    return topic_id


async def mark_topic_done(
    session: AsyncSession, owner, client: ForumTopicClient
) -> None:
    """Benenne das Thema zu `✓ …` um (erledigte Aufgabe). `owner` ist die Zeile,
    an der die Themen-ID haengt — ein Thread ODER ein Projekt. No-op fuer Besitzer
    ohne eigenes Thema (still gelaufen) und fuer das Allgemein-Thema. Das
    Haekchen wird nie doppelt gesetzt.

    Bewusst besitzer-generisch: WER umbenannt werden darf, entscheidet allein
    `handle_task_done` (Marks Regel — ein Projekt-Thema laeuft weiter, wenn ein
    einzelner Task fertig wird). Zwei Orte mit derselben Regel waeren zwei Orte,
    an denen sie brechen kann."""
    topic_id = owner.telegram_topic_id
    if topic_id is None or topic_id == GENERAL_TOPIC_ID:
        return

    title = await _topic_title_for_owner(session, owner)
    base = title[len(_DONE_PREFIX):] if title.startswith(_DONE_PREFIX) else title
    done_title = _truncate(f"{_DONE_PREFIX}{base}")
    try:
        await client.edit_forum_topic(topic_id, done_title)
    except TelegramTopicError as e:
        logger.warning(
            "editForumTopic fuer %s %s fehlgeschlagen: %s", type(owner).__name__, owner.id, e
        )


async def handle_task_done(
    session: AsyncSession, task: Task, *, client: ForumTopicClient | None = None
) -> None:
    """Ein Task ist `done` — kuemmere dich um sein Telegram-Thema.

    Marks Regeln, hier explizit durchgesetzt (nicht bloss als Nebenwirkung der
    Themen-Aufloesung):
      * Ad-hoc-Task -> sein EIGENES Thema wird zu `✓ …` umbenannt und der Thread
        geschlossen (`closed_at`), damit der 30-Tage-Purge ihn spaeter sieht.
      * Task eines PROJEKTS -> nichts. Das Projekt-Thema traegt alle Tasks des
        Projekts und laeuft weiter.
      * SUBTASK -> nichts. Das Thema gehoert dem Elterntask.

    Wirft nie: ein Telegram- oder DB-Fehler darf einen Task-Abschluss nicht
    kippen. Ist der Team-Chat abgeschaltet, passiert gar nichts.
    """
    try:
        if not getattr(settings, "telegram_team_chat_enabled", False):
            return
        if task.thread_id is None:
            return  # still gelaufen, es gibt keinen Thread

        thread = (
            await session.exec(select(Thread).where(Thread.id == task.thread_id))
        ).one_or_none()
        if thread is None:
            return
        if _is_general_thread(thread):
            return  # Allgemein-Thema wird nie umbenannt

        # DIE Regel: umbenannt wird nur ein Thema, das diesem Task ALLEIN gehoert.
        # Gehoert es dem Projekt (alle seine Tasks reden dort) oder dem Elterntask
        # (Subtask), laeuft es weiter — sonst haengt an einem laufenden Projekt
        # ploetzlich ein ✓, nur weil ein einzelner Task fertig wurde.
        owner = await _resolve_topic_owner(session, thread)
        if not isinstance(owner, Thread) or owner.id != thread.id:
            return

        if client is None:
            client = TelegramForumClient()
        await mark_topic_done(session, thread, client)

        if thread.closed_at is None:
            thread.closed_at = datetime.utcnow()
            session.add(thread)
            await session.commit()
    except Exception as e:  # noqa: BLE001 — nie den Task-Abschluss kippen
        logger.warning("handle_task_done fuer Task %s fehlgeschlagen: %s", task.id, e)


async def purge_old_topics(
    session: AsyncSession, client: ForumTopicClient, older_than_days: int = 30
) -> int:
    """Loesche alte Themen (`deleteForumTopic`) und setze danach
    `telegram_topic_id` auf NULL. Der Verlauf bleibt im Web vollstaendig. Gibt die
    Anzahl geloeschter Themen zurueck.

    Zwei Quellen:
      * Task-Threads, die seit mehr als `older_than_days` geschlossen sind
        (`closed_at`, gesetzt von `handle_task_done`).
      * Projekte, die seit mehr als `older_than_days` abgeschlossen/archiviert
        sind — sonst waechst die Themenliste eines langlebigen Chats ewig.

    Das Allgemein-Thema (Sentinel 0) wird nie geloescht. Ein Loesch-Fehler laesst
    die ID stehen, damit ein spaeterer Lauf es erneut versucht."""
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    threads = (
        await session.exec(
            select(Thread).where(
                Thread.telegram_topic_id.is_not(None),
                Thread.telegram_topic_id != GENERAL_TOPIC_ID,
                Thread.closed_at.is_not(None),
                Thread.closed_at < cutoff,
            )
        )
    ).all()
    projects = (
        await session.exec(
            select(Project).where(
                Project.telegram_topic_id.is_not(None),
                Project.telegram_topic_id != GENERAL_TOPIC_ID,
                Project.status.in_(("done", "archived")),
                # `completed_at` schreibt in MC KEIN Code-Pfad (geprueft
                # 28.07.2026) — ein Projekt wird per PATCH auf `done` gesetzt.
                # Haengt der Purge allein daran, loescht er nie ein Projekt-Thema.
                # `updated_at` (onupdate) traegt den Zeitpunkt dieses PATCH.
                func.coalesce(Project.completed_at, Project.updated_at) < cutoff,
            )
        )
    ).all()

    purged = 0
    for owner in [*threads, *projects]:
        try:
            await client.delete_forum_topic(owner.telegram_topic_id)
        except TelegramTopicError as e:
            logger.warning(
                "deleteForumTopic fuer %s %s fehlgeschlagen: %s",
                type(owner).__name__, owner.id, e,
            )
            continue
        owner.telegram_topic_id = None
        session.add(owner)
        purged += 1

    if purged:
        await session.commit()
    return purged


async def purge_topics_tick(older_than_days: int = 30) -> int:
    """Ein Purge-Lauf des periodischen Jobs (siehe `_telegram_topic_purge_loop`
    in main.py). Oeffnet seine eigene Session, respektiert das Feature-Flag und
    schluckt jeden Fehler — der Job darf nie sterben."""
    if not getattr(settings, "telegram_team_chat_enabled", False):
        return 0
    try:
        from app.database import engine

        async with AsyncSession(engine, expire_on_commit=False) as session:
            purged = await purge_old_topics(
                session, TelegramForumClient(), older_than_days=older_than_days
            )
        if purged:
            logger.info("telegram_topic_purge: %d Themen geloescht", purged)
        return purged
    except Exception as e:  # noqa: BLE001 — der periodische Job darf nie sterben
        logger.warning("telegram_topic_purge fehlgeschlagen: %s", e)
        return 0
