"""Live record for FURTHER chats (live finding 23.11.2026).

The agent's own chat is written live by the chat tailer's sink
(``chat_record.LiveRecorder``): the tailer runs while somebody has the
agent's chat stream open, and it follows the ONE session the agent's chat
shows. A further chat has neither:

* its view reads the record (``useRecordChat``: polls
  ``/chat-record/chats/{id}/history``) and never opens the agent's stream, so
  no tailer runs for it;
* even with a tailer running, the tailer follows the default session — an
  omp further chat writes its own copy under ``.mc/`` next to it (the pool
  keeps the default pointer on the default session on purpose), a Claude
  further chat writes under ``claude-config/chats/<chat id>/``.

So its events reached the record only through the 10-minute importer sweep
(``chat_record_import.ChatRecordSync``) — the operator's message stayed
"unconfirmed" for minutes after the agent had answered.

:class:`ChatFollower` closes that gap from the send side: every message sent
to a further chat (``acp_chat_chats.resolve_chat``) starts — or extends — a
small task that imports that chat's own session file(s) as they grow, about
once a second, until the chat has been quiet for :data:`IDLE_STOP_SECONDS`
(longer than the sweep interval, so the sweep takes over seamlessly). The
writer is the importer itself (``import_session_file``): same keys as the
live sink and the sweep, idempotent, only changed rows are written; an
unchanged file costs one ``stat``.

Nothing here runs while ``CHAT_RECORD_ENABLED`` is off. The database is only
opened once the chat's transcript folder exists on disk.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import chat_record

logger = logging.getLogger(__name__)

#: How often a followed chat's files are checked for growth.
POLL_SECONDS = 1.0
#: A followed chat whose files did not grow for this long is left to the
#: importer sweep (default interval 600 s) — longer than it, so no gap.
IDLE_STOP_SECONDS = 15 * 60.0
#: Once its file is known, the chat's file list is looked up again every
#: this many polls (a switch moves the chat to another session, omp's
#: bridge copy can appear after omp's own file).
RESOLVE_EVERY = 5


class _Follow:
    def __init__(self, agent: Any, adapter: Any, chat_id: uuid.UUID) -> None:
        self.agent = agent
        self.adapter = adapter
        self.chat_id = chat_id
        self.last_change = time.monotonic()
        self.sizes: dict[str, int] = {}
        self.files: list[Path] = []
        self.task: asyncio.Task | None = None


class ChatFollower:
    """Per further chat one background task that keeps its record live."""

    def __init__(self, session_factory: Callable[[], AsyncSession] | None = None) -> None:
        self._factory = session_factory
        self._follows: dict[uuid.UUID, _Follow] = {}
        #: Rows written / failed imports (tests and logs).
        self.written = 0
        self.errors = 0

    def _new_session(self) -> AsyncSession:
        if self._factory is not None:
            return self._factory()
        from app.database import async_session_maker

        return async_session_maker()

    def follow(self, agent: Any, chat_id: uuid.UUID | str) -> None:
        """Keep ``chat_id`` (a further chat of ``agent``) live in the record
        for the next :data:`IDLE_STOP_SECONDS` of quiet. Cheap and idempotent:
        a chat already followed only gets its quiet clock reset. Never raises."""
        try:
            if not chat_record.enabled() or agent is None or getattr(agent, "id", None) is None:
                return
            cid = chat_id if isinstance(chat_id, uuid.UUID) else uuid.UUID(str(chat_id))
            current = self._follows.get(cid)
            if current is not None and current.task is not None and not current.task.done():
                current.last_change = time.monotonic()
                return
            from app.services.chat_record_import import agent_snapshot
            from app.services.transcript_adapters import adapter_for

            snap = agent_snapshot(agent)
            state = _Follow(snap, adapter_for(snap), cid)
            state.task = asyncio.get_running_loop().create_task(
                self._run(state), name=f"chat_record_follow:{cid}"
            )
            self._follows[cid] = state
        except Exception:  # the record never takes a send down
            logger.warning("chat record: could not follow chat %s", chat_id, exc_info=True)

    def following(self, chat_id: uuid.UUID | str) -> bool:
        cid = chat_id if isinstance(chat_id, uuid.UUID) else uuid.UUID(str(chat_id))
        state = self._follows.get(cid)
        return state is not None and state.task is not None and not state.task.done()

    async def stop(self) -> None:
        """Ends every follow (shutdown, tests)."""
        tasks = [s.task for s in self._follows.values() if s.task is not None]
        self._follows.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self, state: _Follow) -> None:
        tick = 0
        try:
            while chat_record.enabled():
                if time.monotonic() - state.last_change > IDLE_STOP_SECONDS:
                    return
                if not state.files or tick % RESOLVE_EVERY == 0:
                    if await self._resolve(state) is False:
                        return
                await self._import_grown(state)
                tick += 1
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.errors += 1
            logger.warning("chat record: follow of chat %s ended", state.chat_id, exc_info=True)
        finally:
            if self._follows.get(state.chat_id) is state:
                self._follows.pop(state.chat_id, None)

    async def _resolve(self, state: _Follow) -> bool | None:
        """Looks the chat's files up again. ``False`` = the chat is gone
        (deleted, archived): stop following."""
        from app.models.chat_record import Chat
        from app.services.chat_record_import import further_chat_root, further_chat_files

        root = further_chat_root(state.agent, state.adapter, state.chat_id)
        if root is None or not await asyncio.to_thread(root.is_dir):
            return None  # nothing on disk yet — no database round trip
        async with self._new_session() as db:
            chat = await db.get(Chat, state.chat_id)
            if chat is None or chat.archived_at is not None or chat.agent_default:
                return False
            native = chat.native_session_id
        state.files = await asyncio.to_thread(
            further_chat_files, state.agent, state.adapter, state.chat_id, native
        )
        return True

    async def _import_grown(self, state: _Follow) -> None:
        from app.services import chat_record_import as imp

        for path in list(state.files):
            try:
                size = (await asyncio.to_thread(path.stat)).st_size
            except OSError:
                continue
            if state.sizes.get(str(path)) == size:
                continue
            state.sizes[str(path)] = size
            state.last_change = time.monotonic()
            async with self._new_session() as db:
                try:
                    written = await imp.import_session_file(db, state.agent, state.adapter, path)
                    await db.commit()
                except Exception:
                    # Tried again once the file grows; the sweep marks it.
                    await db.rollback()
                    self.errors += 1
                    logger.warning("chat record: live import of %s failed", path.name, exc_info=True)
                    continue
            if written > 0:
                self.written += written


#: The process-wide follower (the send route's ``resolve_chat`` feeds it).
FOLLOWER = ChatFollower()
