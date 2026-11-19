"""Importer: existing harness session files -> MC's chat record (ADR-103).

Reads every chat session file of every agent that has a transcript folder
(Claude Code, omp, Hermes — whatever ``transcript_adapters`` can read) and
writes it into ``chats`` / ``chat_sessions`` / ``chat_events``: one default chat per
agent (plus the chats that pinned one of its native sessions, S2), one chat session per native session, the events through the same
reader the history page uses (``read_history``).

* **Files stay untouched.** The importer only opens them for reading.
  (Tool pictures go into MC's media store exactly as when the history page
  is opened — content-addressed, so a re-run writes nothing new.)
* **Idempotent.** Events are upserted on their dedupe key, and only rows
  that really change are written; a session file whose size has not
  changed since the last run is skipped without reading.
* **Failures are visible.** A file that fails is marked on its session row
  (``last_error``, ``failed_bytes``) and shown by the read API; it is tried
  again only once it has changed.
* **Same gates as the chat.** A file the adapter's privacy gate rejects
  (``transcript_allowed``) is never imported; for omp only the chat
  sessions are imported (never task sessions), each session once (the
  bridge's file when there are two).

Runs on its own every ``chat_record_import_interval`` seconds in the
background services (:class:`ChatRecordSync`) while the flag is on — the
live sink only writes while somebody has the chat open, this sweep covers
every chat nobody watches. By hand: ``python -m app.services.chat_record_import``
(``--dry-run`` only counts).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import chat_record
from app.utils import utcnow

logger = logging.getLogger(__name__)

_READ_ALL = 10**9

#: ``import_session_file`` results that are not an event count.
SKIPPED_GATE = -1
SKIPPED_UNCHANGED = -2
#: The last import of this file failed and the file has not changed since.
SKIPPED_FAILED_UNCHANGED = -3
#: omp: omp's own file of a session the bridge also writes — the bridge's
#: file is the one in the record (independent entry ids, never both).
SKIPPED_SHADOW = -4


@dataclass
class ImportStats:
    agents: int = 0
    chats: int = 0
    files_seen: int = 0
    files_imported: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_gate: int = 0
    files_empty: int = 0
    files_failed_unchanged: int = 0
    events: int = 0
    bytes_read: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def session_files(adapter: Any, tdir: Path) -> list[Path]:
    """The chat session files under ``tdir``, oldest first, each native
    session once."""
    if not tdir.is_dir():
        return []
    if adapter.name == "omp":
        from app.services import omp_chat

        chosen: dict[str, Path] = {}
        for pattern in omp_chat._SESSION_GLOBS:
            for path in tdir.glob(pattern):
                if not path.is_file() or omp_chat.is_task_session(path):
                    continue
                preferred = omp_chat.bridge_sibling(path)
                chosen[omp_chat.session_id_for(preferred)] = preferred
        files = list(chosen.values())
    else:
        from app.services.transcript_chat import is_command_only_session

        files = [p for p in tdir.glob("*.jsonl") if p.is_file() and not is_command_only_session(p)]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def _read_events(agent: Any, adapter: Any, path: Path) -> tuple[list[dict[str, Any]], str | None, dict | None]:
    from app.services import chat_media
    from app.services.transcript_chat import read_history

    history = read_history(path, adapter, limit=_READ_ALL, media=chat_media.context_for(agent, path))
    return history["events"], history["session"].get("startedAt"), history.get("todos")


def _omp_shadowed(adapter: Any, path: Path) -> bool:
    if adapter.name != "omp":
        return False
    from app.services import omp_chat

    return omp_chat.bridge_sibling(path) != path


def _omp_moves_to_bridge(adapter: Any, recorded: str | None, path: Path) -> bool:
    """The session was recorded from omp's own file and now has the bridge's
    copy: the old rows carry the other file's entry ids and must go."""
    if adapter.name != "omp" or not recorded or recorded == str(path):
        return False
    from app.services import omp_chat

    return omp_chat.is_bridge_file(path) and not omp_chat.is_bridge_file(Path(recorded))


async def _session_row_for(db: AsyncSession, agent: Any, adapter: Any, path: Path):
    native = adapter.session_id_for(path) if hasattr(adapter, "session_id_for") else path.stem
    chat = await chat_record.chat_for_native(db, agent, native)
    try:
        cwd = adapter.session_cwd(path) if hasattr(adapter, "session_cwd") else None
    except Exception:
        cwd = None
    row = await chat_record.ensure_session(
        db,
        chat,
        native_session_id=native,
        harness=getattr(agent, "harness", None) or adapter.name,
        runtime_id=getattr(agent, "runtime_id", None),
        model=getattr(agent, "model", None),
        place=chat_record.place_of(agent),
        transcript_path=str(path),
        cwd=cwd,
    )
    return chat, row


async def import_session_file(
    db: AsyncSession, agent: Any, adapter: Any, path: Path, *, force: bool = False
) -> int:
    """Imports one session file of ``agent``; returns the events written,
    or one of the ``SKIPPED_*`` values. Does not commit."""
    if not adapter.transcript_allowed(agent, path):
        return SKIPPED_GATE
    if await asyncio.to_thread(_omp_shadowed, adapter, path):
        return SKIPPED_SHADOW
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    chat, row = await _session_row_for(db, agent, adapter, path)
    if not chat.agent_default:
        # A pinned chat (S2): collect every row / event of its native
        # session a writer racing the pin left in another chat. Before the
        # bridge check, so a merged copy of omp's own file is replaced too.
        moved, touched = await chat_record.merge_strays(db, chat)
        if touched:
            force = True
    if await asyncio.to_thread(_omp_moves_to_bridge, adapter, row.transcript_path, path):
        from app.models.chat_record import ChatEvent

        await db.execute(delete(ChatEvent.__table__).where(ChatEvent.__table__.c.session_id == row.id))
        row.transcript_path = str(path)
        row.imported_bytes = 0
        force = True
    if not force and row.imported_bytes == size:
        return SKIPPED_UNCHANGED
    if not force and row.last_error and row.failed_bytes == size:
        return SKIPPED_FAILED_UNCHANGED
    events, started_at, todos = await asyncio.to_thread(_read_events, agent, adapter, path)
    if todos and chat_record.is_recordable(todos):
        # read_history keeps only the NEWEST list (a slot, not a timeline);
        # the record keeps that one — the live tailer adds every later one.
        events = sorted([*events, todos], key=lambda e: e.get("ts") or "")
    counter = chat_record.KeyCounter()
    items = []
    for ev in events:
        if not chat_record.is_recordable(ev):
            continue
        key = counter.key(ev)
        if key is not None:
            items.append((key, ev))
    written = await chat_record.upsert_events(db, chat.id, row.id, items)
    row.imported_bytes = size
    row.last_error = None
    row.failed_bytes = 0
    row.last_error_at = None
    if row.started_at is None and started_at:
        row.started_at = chat_record.parse_ts(started_at)
    db.add(row)
    return written


_AGENT_FIELDS = ("id", "name", "slug", "harness", "runtime_id", "model", "workspace_path", "agent_runtime")


def agent_snapshot(agent: Any) -> SimpleNamespace:
    """The agent fields the record needs, read once. After a rollback an ORM
    row is expired and a lazy load would need a DB round trip outside the
    async context (MissingGreenlet) — the failure path uses this copy."""
    return SimpleNamespace(**{f: getattr(agent, f, None) for f in _AGENT_FIELDS})


async def mark_failed(db: AsyncSession, agent: Any, adapter: Any, path: Path, exc: BaseException) -> None:
    """Marks the session of a file whose import failed (after the rollback),
    so the gap is visible and the file is not re-read until it changes.
    ``agent`` must be an :func:`agent_snapshot` taken before the failure.
    Never raises."""
    try:
        size = path.stat().st_size
        _chat, row = await _session_row_for(db, agent, adapter, path)
        row.last_error = type(exc).__name__[:200]
        row.failed_bytes = size
        row.last_error_at = utcnow()
        db.add(row)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("chat record: could not mark %s as failed", path.name, exc_info=True)


async def import_agent(
    db: AsyncSession, agent: Any, stats: ImportStats, *, dry_run: bool = False, adapter: Any = None
) -> None:
    from app.services.transcript_adapters import adapter_for

    adapter = adapter if adapter is not None else adapter_for(agent)
    tdir = adapter.resolve_transcript_dir(agent)
    if tdir is None:
        return
    files = await asyncio.to_thread(session_files, adapter, tdir)
    if not files:
        return
    stats.agents += 1
    # Read once: after a rollback the row is expired and a lazy load would
    # need a DB round trip outside the async context.
    snap = agent_snapshot(agent)
    slug = snap.slug or "?"
    for path in files:
        stats.files_seen += 1
        if dry_run:
            stats.bytes_read += path.stat().st_size
            continue
        try:
            written = await import_session_file(db, agent, adapter, path)
            await db.commit()
        except Exception as exc:  # one bad file never stops the run
            await db.rollback()
            stats.errors.append(f"{slug}/{path.name}: {type(exc).__name__}")
            logger.warning("chat record import failed for %s", path.name, exc_info=True)
            await mark_failed(db, snap, adapter, path, exc)
            continue
        if written == SKIPPED_FAILED_UNCHANGED:
            stats.files_failed_unchanged += 1
        elif written == SKIPPED_SHADOW:
            pass
        elif written == SKIPPED_GATE:
            stats.files_skipped_gate += 1
        elif written == SKIPPED_UNCHANGED:
            stats.files_skipped_unchanged += 1
        elif written == 0:
            stats.files_empty += 1
        else:
            stats.files_imported += 1
            stats.events += written
            stats.bytes_read += path.stat().st_size


async def import_all(db: AsyncSession, *, dry_run: bool = False) -> ImportStats:
    """Imports every agent's session files. Requires the flag."""
    from app.models.agent import Agent
    from app.models.chat_record import Chat

    if not chat_record.enabled():
        raise RuntimeError("CHAT_RECORD_ENABLED is off — nothing imported")
    stats = ImportStats()
    t0 = time.monotonic()
    agents = (await db.exec(select(Agent))).all()
    for agent in agents:
        await import_agent(db, agent, stats, dry_run=dry_run)
    stats.chats = len((await db.exec(select(Chat.id))).all())
    stats.seconds = round(time.monotonic() - t0, 3)
    return stats


class ChatRecordSync:
    """Runs the importer on a schedule in the background services.

    The live sink writes only while a viewer has the chat open (the tailer
    is cancelled with the last client). This sweep covers every chat nobody
    watches, including sessions that rolled over meanwhile. Unchanged files
    cost one ``stat`` and one row lookup each. Idles while the flag is off."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self.last: ImportStats | None = None

    async def sweep_once(self) -> ImportStats | None:
        if not chat_record.enabled():
            return None
        from app import database

        async with database.async_session_maker() as db:
            stats = await import_all(db)
        if stats.errors:
            logger.warning("chat record sweep: %d file(s) failed: %s", len(stats.errors), stats.errors[:5])
        self.last = stats
        return stats

    async def start(self) -> None:
        from app.config import settings

        interval = getattr(settings, "chat_record_import_interval", 0)
        if self._running or not interval or interval <= 0 or interval >= 99999:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval), name="chat_record_sync")
        logger.info("chat record sync started (interval=%ss)", interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval: int) -> None:
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001 — never kill the loop
                logger.exception("chat record sweep failed")


chat_record_sync = ChatRecordSync()


async def _main(dry_run: bool) -> None:
    import json

    from app.database import async_session_maker

    async with async_session_maker() as db:
        stats = await import_all(db, dry_run=dry_run)
    print(json.dumps(stats.as_dict(), indent=2))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count files, write nothing")
    args = parser.parse_args()
    asyncio.run(_main(args.dry_run))
