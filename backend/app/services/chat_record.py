"""MC's own chat record (ADR-103) — the write and read side.

The record stores the events ``services/transcript_chat.py`` already produces
(message, tool, thinking, command, todo, usage, plus the omp extras) in
``chat_events``, one row per event, grouped into ``chat_sessions`` (one per
native harness session) under a ``chats`` row. Two writers feed it:

* the importer (``chat_record_import``) reads whole session files through
  ``read_history`` — the same reader the history page uses;
* the live tailer (``ChatTailerManager``) hands every event it publishes to
  a :class:`LiveRecorder`, which writes it in the background.

Both derive the SAME dedupe key for the same event (:class:`KeyCounter`), so
an overlap, a re-run or a tailer restart never doubles a row: the write is an
upsert on ``(chat_id, source_key)``. A tool card that gets its result later
is the same key with a fuller payload — the upsert replaces it.

Volatile events (``tool_output``, previews, pane state) are never stored, as
in the history page. Nothing here runs unless ``settings.chat_record_enabled``.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, or_, text, update
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.chat_record import Chat, ChatEvent, ChatSession
from app.utils import utcnow

logger = logging.getLogger(__name__)

#: Events that are live-only by contract — never part of the record.
VOLATILE_KINDS = frozenset(
    {"tool_output", "preview", "state", "session_changed", "_tool_result", "_command_result"}
)

#: Cap for a tool's ``result`` text inside the record (ADR-103 §record rules);
#: the full text stays in the native file.
MAX_RESULT_CHARS = 32 * 1024
#: Cap for message / thinking text — far above a normal turn, only a pasted
#: log or a runaway answer reaches it.
MAX_TEXT_CHARS = 128 * 1024
#: How much text of one event goes into the search column.
MAX_SEARCH_CHARS = 8 * 1024

_UPSERT_CHUNK = 500


def enabled() -> bool:
    return bool(getattr(settings, "chat_record_enabled", False))


# ── Keys, caps, search text ──────────────────────────────────────────────


class KeyCounter:
    """Hands out the dedupe key of each event, in reading order.

    * a tool call with an id: ``tool:<toolUseId>`` — stable, and the same
      key when the tailer republishes the card with its result;
    * a command: ``<uuid>:command:0`` — the tailer republishes it with its
      output under the same key;
    * everything else: ``<uuid>:<kind>:<n>`` — the n-th event of that kind
      inside one transcript entry. One entry is always parsed as a whole
      (both readers dedupe on the entry id), so n is the same for the
      importer and for the live tailer.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = defaultdict(int)

    def key(self, ev: dict[str, Any]) -> str | None:
        uid = ev.get("uuid")
        kind = ev.get("kind")
        if not kind:
            return None
        if kind == "tool" and ev.get("toolUseId"):
            return f"tool:{ev['toolUseId']}"[:300]
        if not uid:
            return None
        if kind == "command":
            return f"{uid}:command:0"[:300]
        n = self._counts[(uid, kind)]
        self._counts[(uid, kind)] = n + 1
        return f"{uid}:{kind}:{n}"[:300]

    @staticmethod
    def update_key(ev: dict[str, Any]) -> str | None:
        """Key of a card that is REPUBLISHED (tool with result, command with
        output) — never advances a counter."""
        if ev.get("kind") == "tool" and ev.get("toolUseId"):
            return f"tool:{ev['toolUseId']}"[:300]
        if ev.get("kind") == "command" and ev.get("uuid"):
            return f"{ev['uuid']}:command:0"[:300]
        return None


def is_recordable(ev: dict[str, Any]) -> bool:
    kind = ev.get("kind")
    if not kind or kind in VOLATILE_KINDS:
        return False
    return bool(ev.get("uuid") or (kind == "tool" and ev.get("toolUseId")))


def capped_payload(ev: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``ev`` with oversized texts cut and flagged."""
    payload = dict(ev)
    result = payload.get("result")
    if isinstance(result, str) and len(result) > MAX_RESULT_CHARS:
        payload["result"] = result[:MAX_RESULT_CHARS]
        payload["resultTruncated"] = True
    text = payload.get("text")
    if isinstance(text, str) and len(text) > MAX_TEXT_CHARS:
        payload["text"] = text[:MAX_TEXT_CHARS]
        payload["textTruncated"] = True
    return payload


def _strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    return value


def search_text_of(ev: dict[str, Any]) -> str | None:
    kind = ev.get("kind")
    parts: list[str] = []
    if kind in ("message", "thinking"):
        parts.append(str(ev.get("text") or ""))
    elif kind == "tool":
        parts.append(str(ev.get("title") or ev.get("name") or ""))
    elif kind == "command":
        parts.append(str(ev.get("command") or ""))
        if isinstance(ev.get("result"), str):
            parts.append(ev["result"])
    elif kind == "todo":
        parts.extend(str(i.get("content") or "") for i in ev.get("items") or [] if isinstance(i, dict))
    else:
        text = ev.get("text") or ev.get("message")
        if isinstance(text, str):
            parts.append(text)
    joined = "\n".join(p for p in parts if p).strip()
    return joined[:MAX_SEARCH_CHARS] or None


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Chats and sessions ───────────────────────────────────────────────────


def _insert_for(session: AsyncSession):
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    return pg_insert if dialect == "postgresql" else sqlite_insert


def _default_q(agent_id: uuid.UUID):
    return select(Chat).where(Chat.legacy_agent_id == agent_id, Chat.agent_default.is_(True))


async def ensure_agent_chat(session: AsyncSession, agent: Any) -> Chat:
    """The DEFAULT chat of an existing agent (one per agent): it takes every
    native session that no other chat of the agent pinned."""
    existing = (await session.exec(_default_q(agent.id))).first()
    if existing is not None:
        return existing
    insert = _insert_for(session)
    stmt = (
        insert(Chat.__table__)
        .values(
            id=uuid.uuid4(),
            title=(getattr(agent, "name", None) or getattr(agent, "slug", None) or "Chat")[:200],
            harness=getattr(agent, "harness", None),
            runtime_id=getattr(agent, "runtime_id", None),
            model=getattr(agent, "model", None),
            workspace_path=getattr(agent, "workspace_path", None),
            legacy_agent_id=agent.id,
            agent_default=True,
            next_seq=1,
            pinned=False,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(
            index_elements=["legacy_agent_id"],
            index_where=text("agent_default"),
        )
    )
    await session.execute(stmt)
    return (await session.exec(_default_q(agent.id))).one()


async def chat_for_native(session: AsyncSession, agent: Any, native_session_id: str) -> Chat:
    """The chat a native session of ``agent`` belongs to: the chat that
    pinned it (S2), otherwise the agent's default chat."""
    pinned = (
        await session.exec(
            select(Chat).where(
                Chat.legacy_agent_id == agent.id,
                Chat.native_session_id == native_session_id[:200],
            )
        )
    ).first()
    if pinned is not None:
        return pinned
    return await ensure_agent_chat(session, agent)


async def ensure_session(
    session: AsyncSession,
    chat: Chat,
    *,
    native_session_id: str,
    harness: str | None,
    runtime_id: uuid.UUID | None,
    model: str | None,
    place: str | None,
    transcript_path: str | None,
    cwd: str | None = None,
    started_at: datetime | None = None,
) -> ChatSession:
    def _q():
        return select(ChatSession).where(
            ChatSession.chat_id == chat.id,
            ChatSession.native_session_id == native_session_id[:200],
        )

    row = (await session.exec(_q())).first()
    if row is not None:
        return row
    insert = _insert_for(session)
    stmt = (
        insert(ChatSession.__table__)
        .values(
            id=uuid.uuid4(),
            chat_id=chat.id,
            harness=harness,
            runtime_id=runtime_id,
            model=model,
            place=place,
            native_session_id=native_session_id[:200],
            transcript_path=transcript_path,
            cwd=cwd,
            imported_bytes=0,
            started_at=started_at,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["chat_id", "native_session_id"])
    )
    await session.execute(stmt)
    return (await session.exec(_q())).one()


async def _reserve_seq(session: AsyncSession, chat_id: uuid.UUID, n: int) -> int:
    """Reserves ``n`` sequence numbers for ``chat_id`` and returns the first.

    One UPDATE: Postgres holds the row lock until commit, so two writers
    never get the same range."""
    await session.execute(
        update(Chat.__table__)
        .where(Chat.__table__.c.id == chat_id)
        .values(next_seq=Chat.__table__.c.next_seq + n)
    )
    value = (
        await session.execute(sa_select(Chat.__table__.c.next_seq).where(Chat.__table__.c.id == chat_id))
    ).scalar_one()
    return int(value) - n


def _has_result(ev: dict[str, Any]) -> bool:
    return ev.get("result") is not None


def _dedupe(items: Iterable[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """One item per key, at the place the key first showed up.

    A long or compacted Claude Code session repeats a ``tool_use`` id under a
    new entry uuid (10 of 832 real files, critic 2026-11-18). Both become
    ``tool:<id>``; Postgres refuses two rows with the same conflict key in
    one ``INSERT .. ON CONFLICT DO UPDATE``. The later payload wins, unless
    it would drop a tool result the earlier one already had."""
    order: list[str] = []
    chosen: dict[str, dict[str, Any]] = {}
    for key, ev in items:
        prev = chosen.get(key)
        if prev is None:
            order.append(key)
            chosen[key] = ev
        elif _has_result(ev) or not _has_result(prev):
            chosen[key] = ev
    return [(key, chosen[key]) for key in order]


async def upsert_events(
    session: AsyncSession,
    chat_id: uuid.UUID,
    session_row_id: uuid.UUID,
    items: Iterable[tuple[str, dict[str, Any]]],
) -> int:
    """Writes ``(source_key, event)`` pairs. New keys get new rows in order;
    a known key gets the newer payload (a tool card with its result).

    Only rows that really change are written: a known key whose payload hash
    is unchanged is left alone (no rewrite, no new seq), and a tool card that
    already has its result is never replaced by one without. Sequence numbers
    are reserved for new keys only. Returns the number of rows written
    (inserted or changed). Does not commit."""
    rows: list[dict[str, Any]] = []
    last_ts: datetime | None = None
    for key, ev in _dedupe(items):
        payload = capped_payload(ev)
        # U+0000 shows up in real tool output; Postgres text and JSONB refuse
        # it (measurement run 2026-11-18). Dropped from the JSON escape form.
        encoded = json.dumps(_strip_nul(payload), ensure_ascii=False, default=str, sort_keys=True)
        ts = parse_ts(ev.get("ts"))
        if ts is not None and (last_ts is None or ts > last_ts):
            last_ts = ts
        rows.append(
            {
                "source_key": key,
                "ts": ts,
                "kind": str(ev.get("kind"))[:40],
                "role": (ev.get("role") or None) and str(ev.get("role"))[:20],
                "payload": json.loads(encoded),
                "search_text": (search_text_of(ev) or "").replace("\x00", "") or None,
                "payload_bytes": len(encoded.encode("utf-8")),
                "payload_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            }
        )
    if not rows:
        return 0
    insert = _insert_for(session)
    table = ChatEvent.__table__
    written = 0
    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        existing = {
            key: digest
            for key, digest in (
                await session.execute(
                    sa_select(table.c.source_key, table.c.payload_hash).where(
                        table.c.chat_id == chat_id,
                        table.c.source_key.in_([r["source_key"] for r in chunk]),
                    )
                )
            ).all()
        }
        new_rows = [r for r in chunk if r["source_key"] not in existing]
        changed = [
            r for r in chunk
            if r["source_key"] in existing and existing[r["source_key"]] != r["payload_hash"]
        ]
        # A card that already carries its result keeps it.
        bare_tools = [r["source_key"] for r in changed if r["kind"] == "tool" and not _has_result(r["payload"])]
        if bare_tools:
            kept = {
                key
                for key, payload in (
                    await session.execute(
                        sa_select(table.c.source_key, table.c.payload).where(
                            table.c.chat_id == chat_id, table.c.source_key.in_(bare_tools)
                        )
                    )
                ).all()
                if isinstance(payload, dict) and _has_result(payload)
            }
            changed = [r for r in changed if r["source_key"] not in kept]
        for r in changed:
            await session.execute(
                update(table)
                .where(table.c.chat_id == chat_id, table.c.source_key == r["source_key"])
                .values(
                    payload=r["payload"],
                    search_text=r["search_text"],
                    payload_bytes=r["payload_bytes"],
                    payload_hash=r["payload_hash"],
                )
            )
        written += len(changed)
        if not new_rows:
            continue
        first = await _reserve_seq(session, chat_id, len(new_rows))
        now = utcnow()
        values = [
            {
                "id": uuid.uuid4(),
                "chat_id": chat_id,
                "session_id": session_row_id,
                "seq": first + i,
                "created_at": now,
                **row,
            }
            for i, row in enumerate(new_rows)
        ]
        stmt = insert(table).values(values)
        # Another writer can insert the same key between the SELECT above
        # and this INSERT (importer and live sink at once): then the newer
        # payload wins, but only when it differs.
        stmt = stmt.on_conflict_do_update(
            index_elements=["chat_id", "source_key"],
            set_={
                "payload": stmt.excluded.payload,
                "search_text": stmt.excluded.search_text,
                "payload_bytes": stmt.excluded.payload_bytes,
                "payload_hash": stmt.excluded.payload_hash,
            },
            where=table.c.payload_hash.is_distinct_from(stmt.excluded.payload_hash),
        )
        result = await session.execute(stmt)
        rowcount = getattr(result, "rowcount", -1)
        written += rowcount if isinstance(rowcount, int) and rowcount >= 0 else len(new_rows)
    if last_ts is not None:
        await session.execute(
            update(Chat.__table__)
            .where(Chat.__table__.c.id == chat_id)
            .where(
                or_(
                    Chat.__table__.c.last_activity_at.is_(None),
                    Chat.__table__.c.last_activity_at < last_ts,
                )
            )
            .values(last_activity_at=last_ts)
        )
    return written


# ── Live sink ────────────────────────────────────────────────────────────


def place_of(agent: Any) -> str:
    return "host" if getattr(agent, "agent_runtime", None) == "host" else "container"


class LiveRecorder:
    """Background writer for one tailer. ``record`` never blocks the tailer:
    it snapshots the event and queues it; one worker task writes in order.

    On start the worker first backfills EVERY session file of the agent
    (the tailer joins at the END of its file, and sessions that rolled over
    while nobody watched — a model or runtime switch — would otherwise be
    missing until the next scheduled sweep). Each later file the tailer
    moves to is backfilled the first time an event of it arrives. The
    backfill is the importer, so its keys match the live ones and an
    unchanged file costs one ``stat``.

    omp under ACP: events of omp's own file are not written while the
    bridge's copy of the same session exists; when the tailer moves to the
    bridge file, its backfill replaces the rows of omp's file (independent
    entry ids — keeping both would show every turn twice).
    """

    def __init__(
        self,
        agent: Any,
        adapter: Any,
        initial_path: Path | None,
        session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        self._agent = agent
        self._adapter = adapter
        from app.services.chat_record_import import agent_snapshot

        self._snapshot = agent_snapshot(agent)
        self._factory = session_factory
        self._queue: asyncio.Queue = asyncio.Queue()
        self._counters: dict[str, KeyCounter] = {}
        self._session_rows: dict[str, tuple[uuid.UUID, uuid.UUID, bool]] = {}
        self._initial_path = initial_path
        #: Paths whose backfill ran: ``True`` = write its events, ``False`` =
        #: skip them (gate, or omp's own file shadowed by the bridge's).
        self._settled: dict[str, bool] = {}
        #: Set once the start-up backfill is done (tests wait on it).
        self.backfilled = asyncio.Event()
        self._task = asyncio.create_task(self._worker())
        self.written = 0
        self.errors = 0

    def _new_session(self) -> AsyncSession:
        if self._factory is not None:
            return self._factory()
        from app.database import async_session_maker

        return async_session_maker()

    def record(self, path: Path, ev: dict[str, Any], *, republish: bool = False) -> None:
        if not is_recordable(ev):
            return
        if republish:
            key = KeyCounter.update_key(ev)
        else:
            key = self._counters.setdefault(str(path), KeyCounter()).key(ev)
        if key is None:
            return
        self._queue.put_nowait((path, key, copy.deepcopy(ev)))

    def close(self) -> None:
        """Ends the worker after it has written everything queued so far."""
        self._queue.put_nowait(None)

    async def wait_idle(self) -> None:
        """Tests: wait until everything queued is written."""
        await self._queue.join()

    async def _session_row(self, db: AsyncSession, path: Path) -> tuple[uuid.UUID, uuid.UUID]:
        native = self._adapter.session_id_for(path) if hasattr(self._adapter, "session_id_for") else path.stem
        cached = self._session_rows.get(str(path))
        if cached is not None:
            chat_id, row_id, is_default = cached
            if not is_default:
                # A pinned chat keeps its native session for good.
                return chat_id, row_id
            # A pin (S2) can take this native session away from the default
            # chat while the tailer runs. Route by native id, not by the
            # cached row: the row may have moved, or be a stray the race
            # created in the default chat.
            if await pinned_chat_id(db, self._agent.id, native) is None:
                return chat_id, row_id
            self._session_rows.pop(str(path), None)
        chat = await chat_for_native(db, self._agent, native)
        row = await ensure_session(
            db,
            chat,
            native_session_id=native,
            harness=getattr(self._agent, "harness", None),
            runtime_id=getattr(self._agent, "runtime_id", None),
            model=getattr(self._agent, "model", None),
            place=place_of(self._agent),
            transcript_path=str(path),
        )
        if not chat.agent_default:
            await merge_strays(db, chat)
            # merge_strays may have adopted an older row as the chat's own.
            row = (
                await db.exec(
                    select(ChatSession).where(
                        ChatSession.chat_id == chat.id, ChatSession.native_session_id == native[:200]
                    )
                )
            ).one()
        self._session_rows[str(path)] = (chat.id, row.id, bool(chat.agent_default))
        return chat.id, row.id

    async def _backfill_file(self, path: Path) -> bool:
        """Imports ``path`` once (idempotent); ``False`` = do not write its
        live events."""
        from app.services import chat_record_import as imp

        result = None
        async with self._new_session() as db:
            try:
                result = await imp.import_session_file(db, self._agent, self._adapter, path)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                self.errors += 1
                logger.warning("chat record: backfill of %s failed", path.name, exc_info=True)
                await imp.mark_failed(db, self._snapshot, self._adapter, path, exc)
        ok = result not in (imp.SKIPPED_GATE, imp.SKIPPED_SHADOW)
        self._settled[str(path)] = ok
        return ok

    async def _backfill_all(self) -> None:
        from app.services import chat_record_import as imp

        async with self._new_session() as db:
            stats = imp.ImportStats()
            await imp.import_agent(db, self._agent, stats, adapter=self._adapter)
            self.errors += len(stats.errors)
        if self._initial_path is not None:
            await self._backfill_file(self._initial_path)

    async def _worker(self) -> None:
        try:
            await self._backfill_all()
        except Exception:
            self.errors += 1
            logger.warning("chat record: backfill failed", exc_info=True)
        self.backfilled.set()
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                path, key, ev = item
                ok = self._settled.get(str(path))
                if ok is None:
                    ok = await self._backfill_file(path)
                if not ok:
                    continue
                async with self._new_session() as db:
                    chat_id, session_row_id = await self._session_row(db, path)
                    self.written += await upsert_events(db, chat_id, session_row_id, [(key, ev)])
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The record must never take the chat down: count, log, go on.
                self.errors += 1
                logger.warning("chat record: live write failed", exc_info=True)
            finally:
                self._queue.task_done()


def live_recorder_for(agent: Any, adapter: Any, initial_path: Path | None) -> LiveRecorder | None:
    """The tailer's sink, or ``None`` (flag off, or no agent row)."""
    if not enabled() or agent is None or getattr(agent, "id", None) is None:
        return None
    return LiveRecorder(agent, adapter, initial_path)


# ── Many chats per harness (S2) ──────────────────────────────────────────

#: How a harness continues one exact native session, per harness name of
#: ``transcript_adapters``. ``cli``: Claude Code, ``--session-id <id>`` on
#: the first start and ``--resume <id>`` later (both flags in
#: ``claude --help``). ``acp``: omp over ACP, ``session/new`` and then
#: ``session/load <id>`` (``docker/omp-bridge/acp_chat.py``). A harness not
#: listed cannot hold a second chat yet.
RESUME_STYLE: dict[str, str] = {"claude": "cli", "omp": "acp"}

#: A native session id travels into a CLI flag and an ACP call: a plain
#: token, never a path or anything a shell could split.
NATIVE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"


class ChatRecordError(Exception):
    status = 400

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ChatRecordInvalid(ChatRecordError):
    status = 422


class ChatRecordConflict(ChatRecordError):
    status = 409


def harness_name(agent: Any) -> str:
    from app.services.transcript_adapters import adapter_for

    harness = getattr(agent, "harness", None)
    # adapter_for falls back to Claude for an unknown harness; a chat must
    # not — an unknown harness has no session to pin.
    if harness and harness not in RESUME_STYLE:
        return harness
    return adapter_for(agent).name


async def create_chat(session: AsyncSession, user: Any, agent: Any, *, title: str | None = None) -> Chat:
    """A further chat on ``agent``'s harness, owned by ``user``. Claude Code
    gets its native session id now (MC starts the CLI with it); omp gets it
    from ``session/new`` later (:func:`pin_native_session`). Does not commit."""
    harness = harness_name(agent)
    style = RESUME_STYLE.get(harness)
    if style is None:
        raise ChatRecordInvalid("harness_cannot_pin_sessions")
    # The default chat first, so routing always has a home for the
    # sessions nobody pinned.
    await ensure_agent_chat(session, agent)
    owner = getattr(user, "id", None)
    if owner is not None:
        from app.models.user import User

        # LOCAL_AUTH_TOKEN's synthetic admin has no users row (FK).
        if await session.get(User, owner) is None:
            owner = None
    chat = Chat(
        id=uuid.uuid4(),
        title=((title or "").strip() or getattr(agent, "name", None) or "Chat")[:200],
        owner_user_id=owner,
        harness=harness,
        runtime_id=getattr(agent, "runtime_id", None),
        model=getattr(agent, "model", None),
        workspace_path=getattr(agent, "workspace_path", None),
        legacy_agent_id=agent.id,
        agent_default=False,
        native_session_id=str(uuid.uuid4()) if style == "cli" else None,
        next_seq=1,
        pinned=False,
        created_at=utcnow(),
    )
    session.add(chat)
    await session.flush()
    return chat


async def _move_events(
    session: AsyncSession, from_session_id: uuid.UUID, target: Chat, to_session_id: uuid.UUID
) -> int:
    """Moves the events of session row ``from_session_id`` that sit outside
    ``target`` into ``target`` under session row ``to_session_id``; they get
    fresh sequence numbers of the target chat. A key the target already
    holds wins, the copy goes. Returns the events moved."""
    table = ChatEvent.__table__
    events = (
        await session.execute(
            sa_select(table.c.id, table.c.source_key)
            # Rows already in the target stay as they are.
            .where(table.c.session_id == from_session_id, table.c.chat_id != target.id)
            .order_by(table.c.ts.asc().nulls_first(), table.c.seq.asc())
        )
    ).all()
    if not events:
        return 0
    taken = {
        k for (k,) in (
            await session.execute(
                sa_select(table.c.source_key).where(
                    table.c.chat_id == target.id,
                    table.c.source_key.in_([e.source_key for e in events]),
                )
            )
        ).all()
    }
    stale = [e.id for e in events if e.source_key in taken]
    if stale:
        await session.execute(delete(table).where(table.c.id.in_(stale)))
    movers = [e for e in events if e.source_key not in taken]
    if movers:
        first = await _reserve_seq(session, target.id, len(movers))
        for i, ev in enumerate(movers):
            await session.execute(
                update(table)
                .where(table.c.id == ev.id)
                .values(chat_id=target.id, session_id=to_session_id, seq=first + i)
            )
    return len(movers)


async def _move_session(session: AsyncSession, row: ChatSession, target: Chat) -> int:
    """Moves one session row and its events into ``target``. Returns the
    events moved."""
    moved = await _move_events(session, row.id, target, row.id)
    await session.execute(
        update(ChatSession.__table__)
        .where(ChatSession.__table__.c.id == row.id)
        .values(chat_id=target.id, imported_bytes=0)
    )
    row.chat_id = target.id
    row.imported_bytes = 0
    return moved


async def merge_strays(session: AsyncSession, chat: Chat) -> tuple[int, int]:
    """Makes a pinned chat the ONLY home of its native session, by native id
    and not by row: every ``chat_sessions`` row of the agent with that
    native id in another chat is moved in (or merged into the chat's own
    row and deleted), and events of the chat's own row that a writer put
    into another chat come over too. Covers the pin race in both shapes —
    a writer that cached the row before the pin moved it, and a writer that
    resolved the default chat before the pin and created a NEW row there.
    Only pinned chats; the default chat has nothing to collect. Returns
    ``(events moved, rows or events repaired)``. Does not commit."""
    native = chat.native_session_id
    if chat.agent_default or not native or chat.legacy_agent_id is None:
        return 0, 0
    own = (
        await session.exec(
            select(ChatSession).where(ChatSession.chat_id == chat.id, ChatSession.native_session_id == native)
        )
    ).first()
    strays = (
        await session.exec(
            select(ChatSession)
            .join(Chat, Chat.id == ChatSession.chat_id)
            .where(
                Chat.legacy_agent_id == chat.legacy_agent_id,
                ChatSession.native_session_id == native,
                ChatSession.chat_id != chat.id,
            )
            .order_by(ChatSession.created_at.asc())
        )
    ).all()
    moved = touched = 0
    table = ChatEvent.__table__
    for row in strays:
        touched += 1
        if own is None:
            moved += await _move_session(session, row, chat)
            own = row
            continue
        if row.transcript_path and own.transcript_path and row.transcript_path != own.transcript_path:
            # Another file of the same session (omp's own file vs the
            # bridge's copy): its entry ids differ, merging would double
            # every turn. Drop the copy, re-read the chat's own file.
            await session.execute(delete(table).where(table.c.session_id == row.id))
        else:
            moved += await _move_events(session, row.id, chat, own.id)
        # Anything left under the stray row is a key the chat already has.
        await session.execute(delete(table).where(table.c.session_id == row.id))
        await session.execute(delete(ChatSession.__table__).where(ChatSession.__table__.c.id == row.id))
        own.imported_bytes = 0
        session.add(own)
    if own is not None:
        # A writer that cached the row before a pin moved it.
        own_strays = await _move_events(session, own.id, chat, own.id)
        moved += own_strays
        touched += own_strays
    if touched:
        await session.flush()
    return moved, touched


async def pin_native_session(session: AsyncSession, chat: Chat, native_session_id: str) -> int:
    """Binds ``chat`` to one native session of its agent (omp: the id ACP
    ``session/new`` returned). Idempotent for the same id. Every row of that
    session another chat of the agent already recorded (the sweep was
    faster, or a writer raced the pin) moves over with its events
    (:func:`merge_strays`). Returns the events moved. Does not commit."""
    import re

    native = native_session_id or ""
    if not re.match(NATIVE_ID_PATTERN, native):
        raise ChatRecordInvalid("bad_native_session_id")
    if chat.agent_default or chat.legacy_agent_id is None:
        raise ChatRecordConflict("default_chat")
    if chat.native_session_id == native:
        return 0
    if chat.native_session_id is not None:
        raise ChatRecordConflict("already_pinned")
    other = (
        await session.exec(
            select(Chat).where(
                Chat.legacy_agent_id == chat.legacy_agent_id,
                Chat.native_session_id == native,
                Chat.id != chat.id,
            )
        )
    ).first()
    if other is not None:
        raise ChatRecordConflict("pinned_by_other_chat")
    chat.native_session_id = native
    session.add(chat)
    await session.flush()
    moved, touched = await merge_strays(session, chat)
    if touched:
        await session.execute(
            update(Chat.__table__)
            .where(Chat.__table__.c.id == chat.id)
            .values(last_activity_at=func.coalesce(Chat.__table__.c.last_activity_at, utcnow()))
        )
    return moved


async def pinned_chat_id(session: AsyncSession, agent_id: uuid.UUID, native_session_id: str) -> uuid.UUID | None:
    """The chat that pinned this native session of the agent, or ``None``
    (= the default chat). One lookup on ``uq_chats_agent_native``."""
    return (
        await session.execute(
            sa_select(Chat.__table__.c.id).where(
                Chat.__table__.c.legacy_agent_id == agent_id,
                Chat.__table__.c.native_session_id == native_session_id[:200],
            )
        )
    ).scalar_one_or_none()


async def update_chat(
    session: AsyncSession,
    chat: Chat,
    *,
    title: str | None = None,
    archived: bool | None = None,
    pinned: bool | None = None,
) -> Chat:
    """Rename, archive / unarchive, pin a chat. The default chat of an agent
    cannot be archived: the importer would keep writing into a hidden chat.
    Does not commit."""
    if title is not None:
        clean = title.strip()
        if not clean:
            raise ChatRecordInvalid("empty_title")
        chat.title = clean[:200]
    if archived is not None:
        if archived and chat.agent_default:
            raise ChatRecordConflict("default_chat")
        chat.archived_at = utcnow() if archived else None
    if pinned is not None:
        chat.pinned = bool(pinned)
    session.add(chat)
    await session.flush()
    return chat


async def resume_plan(session: AsyncSession, chat: Chat) -> dict[str, Any]:
    """What the harness side must do to put this chat's own session in
    front of the model: ``start`` a new one or ``resume`` the pinned one.
    Only data — nothing is started here."""
    style = RESUME_STYLE.get(chat.harness or "")
    native = chat.native_session_id
    started = False
    if native:
        started = (
            await session.exec(
                select(ChatSession.id).where(
                    ChatSession.chat_id == chat.id,
                    ChatSession.native_session_id == native,
                )
            )
        ).first() is not None
    plan: dict[str, Any] = {
        "chatId": str(chat.id),
        "harness": chat.harness,
        "style": style,
        "nativeSessionId": native,
        "agentDefault": chat.agent_default,
        "action": "resume" if started or (style == "acp" and native) else "start",
    }
    if chat.agent_default:
        # The default chat follows the agent's process as today (newest file).
        plan["action"] = "follow_agent"
    elif style == "cli" and native:
        plan["claudeArgs"] = ["--resume", native] if started else ["--session-id", native]
    elif style == "acp":
        plan["acp"] = {"method": "session/load", "sessionId": native} if native else {"method": "session/new"}
    return plan


def chat_out(chat: Chat) -> dict[str, Any]:
    return {
        "id": str(chat.id),
        "title": chat.title,
        "harness": chat.harness,
        "model": chat.model,
        "runtimeId": str(chat.runtime_id) if chat.runtime_id else None,
        "legacyAgentId": str(chat.legacy_agent_id) if chat.legacy_agent_id else None,
        "agentDefault": bool(chat.agent_default),
        "nativeSessionId": chat.native_session_id,
        "ownerUserId": str(chat.owner_user_id) if chat.owner_user_id else None,
        "pinned": chat.pinned,
        "archived": chat.archived_at is not None,
        "lastActivityAt": chat.last_activity_at.isoformat() if chat.last_activity_at else None,
    }


# ── Read side ────────────────────────────────────────────────────────────


def _visible(user: Any):
    """Chats without an owner (imported from agents) are visible to every
    signed-in user, like the agent chats today; owned chats to their owner
    and admins."""
    if getattr(user, "role", None) == "admin":
        return True
    return or_(Chat.owner_user_id.is_(None), Chat.owner_user_id == getattr(user, "id", None))


async def list_chats(
    session: AsyncSession, user: Any, *, limit: int = 100, agent_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    counts = (
        select(
            ChatEvent.chat_id.label("chat_id"),
            func.count().label("events"),
        )
        .group_by(ChatEvent.chat_id)
        .subquery()
    )
    sessions = (
        select(
            ChatSession.chat_id.label("chat_id"),
            func.count().label("sessions"),
            func.count(ChatSession.last_error).label("failed"),
        )
        .group_by(ChatSession.chat_id)
        .subquery()
    )
    q = (
        select(Chat, counts.c.events, sessions.c.sessions, sessions.c.failed)
        .outerjoin(counts, counts.c.chat_id == Chat.id)
        .outerjoin(sessions, sessions.c.chat_id == Chat.id)
        .where(Chat.archived_at.is_(None))
        .order_by(Chat.last_activity_at.desc().nulls_last(), Chat.created_at.desc())
        .limit(limit)
    )
    if agent_id is not None:
        q = q.where(Chat.legacy_agent_id == agent_id)
    visible = _visible(user)
    if visible is not True:
        q = q.where(visible)
    out = []
    for chat, events, n_sessions, failed in (await session.exec(q)).all():
        out.append(
            {
                **chat_out(chat),
                "events": int(events or 0),
                "sessions": int(n_sessions or 0),
                # Sessions whose last import failed — "record incomplete".
                "failedSessions": int(failed or 0),
            }
        )
    return out


async def get_chat(session: AsyncSession, user: Any, chat_id: uuid.UUID) -> Chat | None:
    q = select(Chat).where(Chat.id == chat_id)
    visible = _visible(user)
    if visible is not True:
        q = q.where(visible)
    return (await session.exec(q)).first()


def _event_out(ev: ChatEvent) -> dict[str, Any]:
    return {
        "id": str(ev.id),
        "seq": ev.seq,
        "sessionId": str(ev.session_id),
        "event": ev.payload,
    }


async def chat_history(
    session: AsyncSession,
    chat_id: uuid.UUID,
    *,
    limit: int = 200,
    before: uuid.UUID | None = None,
) -> dict[str, Any]:
    """The newest ``limit`` events of a chat (oldest first), or the page
    right before event ``before`` — order is (ts, seq) across all sessions."""
    q = select(ChatEvent).where(ChatEvent.chat_id == chat_id)
    if before is not None:
        anchor = (await session.exec(
            select(ChatEvent).where(ChatEvent.chat_id == chat_id, ChatEvent.id == before)
        )).first()
        if anchor is None:
            return {"events": [], "hasMore": False, "sessions": []}
        if anchor.ts is None:
            q = q.where(and_(ChatEvent.ts.is_(None), ChatEvent.seq < anchor.seq) | ChatEvent.ts.isnot(None))
        else:
            q = q.where(
                or_(
                    ChatEvent.ts < anchor.ts,
                    and_(ChatEvent.ts == anchor.ts, ChatEvent.seq < anchor.seq),
                )
            )
    q = q.order_by(ChatEvent.ts.desc().nulls_first(), ChatEvent.seq.desc()).limit(limit + 1)
    rows = list((await session.exec(q)).all())
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    sessions = (await session.exec(
        select(ChatSession)
        .where(ChatSession.chat_id == chat_id)
        .order_by(ChatSession.started_at.asc().nulls_last())
    )).all()
    return {
        "events": [_event_out(r) for r in rows],
        "hasMore": has_more,
        "sessions": [
            {
                "id": str(s.id),
                "harness": s.harness,
                "model": s.model,
                "place": s.place,
                "nativeSessionId": s.native_session_id,
                "startedAt": s.started_at.isoformat() if s.started_at else None,
                "error": s.last_error,
                "errorAt": s.last_error_at.isoformat() if s.last_error_at else None,
            }
            for s in sessions
        ],
    }


async def search(session: AsyncSession, user: Any, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Search across every visible chat. Postgres: full-text (``simple``
    configuration, GIN index from migration 0227); SQLite (tests): a plain
    case-insensitive substring match."""
    query = (query or "").strip()
    if not query:
        return []
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    q = select(ChatEvent, Chat.title).join(Chat, Chat.id == ChatEvent.chat_id).where(Chat.archived_at.is_(None))
    if dialect == "postgresql":
        doc = func.to_tsvector("simple", func.coalesce(ChatEvent.search_text, ""))
        tsq = func.plainto_tsquery("simple", query)
        q = q.where(doc.op("@@")(tsq)).order_by(func.ts_rank(doc, tsq).desc(), ChatEvent.ts.desc())
    else:
        q = q.where(ChatEvent.search_text.ilike(f"%{query}%")).order_by(ChatEvent.ts.desc())
    visible = _visible(user)
    if visible is not True:
        q = q.where(visible)
    q = q.limit(limit)
    out = []
    for ev, title in (await session.exec(q)).all():
        text = ev.search_text or ""
        idx = text.lower().find(query.lower().split()[0]) if query.split() else -1
        start = max(0, idx - 60) if idx >= 0 else 0
        out.append(
            {
                "chatId": str(ev.chat_id),
                "chatTitle": title,
                "eventId": str(ev.id),
                "kind": ev.kind,
                "ts": ev.ts.isoformat() if ev.ts else None,
                "snippet": text[start : start + 200],
            }
        )
    return out
