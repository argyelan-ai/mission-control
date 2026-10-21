"""Chat resets in the activity feed — who asked, or that MC did not.

Incident 21.10.2026: an operator's chat got a new, empty session and nobody
could say who sent it. The backend's logs had rotated, nothing durable named
the origin, and the chat daemon can be reached without MC (``docker exec …
acp_chat_ctl.py new_session``, or the agent's own tools, which run as the
socket's owner). Every chat reset is now ONE activity event
``chat.session_reset`` (agent page feed, ``GET /activity``), from two sources:

* **Chat input** (``POST /agents/{id}/chat/input``, ``/new`` or ``/clear``):
  the router writes the event at once — who asked (the chat.* actor shape of
  ``routers/agent_chat._actor``), the way, the session it replaces. For a
  headless agent the backend sends a ``requestId`` with ``new_session``.
* **The chat daemon's record** (``resets`` in its state mirror,
  docker/omp-bridge/acp_chat.py): every reset job with its outcome and every
  reset the daemon did on its own. ``ChatResetAudit`` reads it every
  ``chat_reset_audit_interval`` seconds and

  - completes the event of a request it names (``requestId``): done (the new
    session id), failed, taken back, dropped by a restart — the event never
    keeps saying "runs when the reply ends" for a reset that never ran;
  - writes an event for a reset nobody in MC asked for: ``system`` (a daemon
    start or a respawned omp child could not resume the conversation) or
    ``outside_mc`` (``new_session`` without a request id — a shell or the
    agent's own tools). A request id MC has no event for is ``unknown``.

Everything read from the mirror comes from the agent's own mount: only ids
and words of a fixed, short shape are kept (``_clean_row``); anything else is
dropped, never stored. The daemon writes the matching card into the new
conversation (``new_conversation``).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select

from app.config import settings
from app.services.acp_chat_transport import headless_chat_kind, read_acp_chat_state

logger = logging.getLogger(__name__)

#: The activity event of a chat reset.
CHAT_SESSION_RESET_EVENT = "chat.session_reset"

#: Who handled the command MC passed on (``detail.handled_by``): the chat
#: daemon (MC turned it into ``new_session``), the agent's terminal (a TUI
#: agent: its CLI runs it), the agent's own ACP command of that name.
HANDLED_BY_DAEMON = "chat_daemon"
HANDLED_BY_TERMINAL = "terminal"
HANDLED_BY_HARNESS = "harness"

#: The ways in (``detail.via``).
VIA_CHAT_INPUT = "chat_input"
VIA_DAEMON_START = "daemon_start"
VIA_CHILD_RESTART = "child_restart"
VIA_CONTROL_SOCKET = "control_socket"

#: Senders that are no logged-in user (``detail.actor``; the others come
#: from ``routers/agent_chat._actor``).
ACTOR_SYSTEM = "system"
ACTOR_OUTSIDE_MC = "outside_mc"
ACTOR_UNKNOWN = "unknown"

#: Shapes kept from the daemon's answer and its mirror (the agent's mount).
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RESET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_REASON_RE = re.compile(r"[a-z_]{1,40}")
_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DAEMON_VIAS = {"new_session": VIA_CONTROL_SOCKET, "start": VIA_DAEMON_START,
                "child_restart": VIA_CHILD_RESTART}
_OUTCOMES = ("done", "failed", "withdrawn", "lost")
#: The daemon keeps 20; more is never read.
_ROWS_READ_MAX = 50
#: Events compared against one agent's rows (newest first).
_EVENTS_READ_MAX = 200
#: A row naming a request MC has no event for yet: the router writes it
#: right after the daemon answered. Only after this long is the row
#: "unknown" — before, it is the normal race with that write.
REQUEST_GRACE_SECONDS = 120.0

#: How a failed resume reads in a title.
_REASON_WORDS = {
    "load_failed": "loading it failed",
    "model_missing": "its model is not set up",
    "transcript_missing": "its history was not found",
    "no_pointer": "it was unclear which conversation was open",
}


def bounded_id(value: Any, shape: re.Pattern = _SESSION_ID_RE) -> str | None:
    """``value`` when it is a string of ``shape``, else None — a session or
    request id the container hands back is stored only in a short, plain
    form (critic: a 200 000-character id went into the event as is)."""
    if isinstance(value, str) and shape.fullmatch(value):
        return value
    return None


@dataclass(frozen=True)
class ChatReset:
    """A reset ``send_text`` passed on — what the router records."""

    command: str  # "/new" | "/clear"
    previous_session_id: str | None  # the conversation it replaces, when known
    queued: bool  # held behind a running reply, runs when it ends
    request_id: str | None = None  # sent with new_session (headless agents)
    queue_id: str | None = None  # the daemon's job id of a held reset
    handled_by: str = HANDLED_BY_DAEMON


# ── titles ───────────────────────────────────────────────────────────────────


def actor_label(detail: dict) -> str:
    """Who, in words, from the chat.* actor fields of an event's detail."""
    kind = detail.get("actor")
    name = detail.get("actorName")
    acting_as = detail.get("actingAs")
    if kind == ACTOR_SYSTEM:
        return "the chat daemon"
    if kind == ACTOR_OUTSIDE_MC:
        return "a sender outside MC"
    if kind == ACTOR_UNKNOWN or not name:
        return "an unknown sender"
    return f"{name} (as {acting_as})" if acting_as else str(name)


def reset_title(detail: dict) -> str:
    """The event's title, from its detail — one function, so an event that
    is completed later reads like one written that way."""
    who = actor_label(detail)
    command = detail.get("command") or "/new"
    outcome = detail.get("outcome")
    via = detail.get("via")
    if via in (VIA_DAEMON_START, VIA_CHILD_RESTART):
        why = _REASON_WORDS.get(str(detail.get("reason")), "it could not be resumed")
        where = "the chat restarted" if via == VIA_DAEMON_START else "the agent process restarted"
        return f"New chat conversation: {where} and the previous one did not resume — {why}"
    if via == VIA_CONTROL_SOCKET:
        return (f"Chat reset by {who} — new_session through the chat daemon's control "
                "socket, not sent by MC")
    if detail.get("handled_by") == HANDLED_BY_TERMINAL:
        return f"Chat reset {command} sent by {who} to the agent's terminal"
    if detail.get("handled_by") == HANDLED_BY_HARNESS:
        return f"Chat reset {command} sent by {who} to the agent's own {command} command"
    if outcome == "queued":
        return f"Chat reset {command} requested by {who} — runs when the current reply ends"
    if outcome == "failed":
        return f"Chat reset {command} by {who} failed — the current conversation goes on"
    if outcome == "withdrawn":
        return f"Chat reset {command} requested by {who} — taken back before it ran"
    if outcome == "lost":
        return f"Chat reset {command} requested by {who} — dropped by a chat restart before it ran"
    if detail.get("kept") is False:
        return f"Chat reset {command} by {who} — new conversation, the previous copy was not found"
    return f"Chat reset {command} by {who} — new conversation, the previous one is kept"


def chat_input_event(reset: ChatReset, actor: dict) -> tuple[str, dict]:
    """Title and detail of the event for a reset sent through chat input.
    ``actor``: ``routers/agent_chat._actor`` (who sent the request)."""
    if reset.handled_by != HANDLED_BY_DAEMON:
        outcome = "sent"  # the agent's own CLI runs it; MC sees no more
    else:
        outcome = "queued" if reset.queued else "accepted"
    detail = {
        **actor,
        "via": VIA_CHAT_INPUT,
        "command": reset.command,
        "handled_by": reset.handled_by,
        "outcome": outcome,
        "queued": reset.queued,
        "request_id": bounded_id(reset.request_id, _REQUEST_ID_RE),
        "queue_id": bounded_id(reset.queue_id, _REQUEST_ID_RE),
        "previous_session_id": bounded_id(reset.previous_session_id),
        "new_session_id": None,
    }
    return reset_title(detail), detail


# ── the daemon's record ──────────────────────────────────────────────────────


def _clean_row(raw: Any) -> dict | None:
    """One ``resets`` row as the daemon writes it, or None — a known field of
    another shape drops the row (the mirror is on the agent's mount)."""
    if not isinstance(raw, dict):
        return None
    shapes = (("id", _RESET_ID_RE), ("at", _AT_RE), ("previousSessionId", _SESSION_ID_RE),
              ("sessionId", _SESSION_ID_RE), ("reason", _REASON_RE),
              ("requestId", _REQUEST_ID_RE))
    row: dict = {}
    for key, shape in shapes:
        value = raw.get(key)
        if value is None:
            continue
        if not bounded_id(value, shape):
            return None
        row[key] = value
    if "id" not in row or raw.get("via") not in _DAEMON_VIAS or raw.get("outcome") not in _OUTCOMES:
        return None
    row["via"], row["outcome"] = raw["via"], raw["outcome"]
    if isinstance(raw.get("kept"), bool):
        row["kept"] = raw["kept"]
    return row


def reset_rows(state: dict | None) -> list[dict]:
    """The usable rows of a state mirror's ``resets``, oldest first."""
    rows = state.get("resets") if isinstance(state, dict) else None
    if not isinstance(rows, list):
        return []
    cleaned = [_clean_row(r) for r in rows[-_ROWS_READ_MAX:]]
    dropped = sum(1 for r in cleaned if r is None)
    if dropped:
        logger.warning("chat reset audit: %d reset row(s) of an unexpected shape ignored", dropped)
    return [r for r in cleaned if r is not None]


def _age_seconds(row: dict, now: datetime) -> float | None:
    try:
        at = datetime.strptime(row["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    return (now - at).total_seconds()


def _row_fields(row: dict) -> dict:
    """What a row adds to an event's detail."""
    fields = {
        "reset_id": row["id"],
        "at": row.get("at"),
        "previous_session_id": row.get("previousSessionId"),
        "new_session_id": row.get("sessionId") if row["outcome"] == "done" else None,
        "reason": row.get("reason"),
    }
    if "kept" in row:
        fields["kept"] = row["kept"]
    return fields


async def reconcile_agent(session, agent, state: dict | None,
                          *, now: datetime | None = None) -> set[str]:
    """Apply one agent's ``resets`` to its activity events (see module doc).

    Returns the ids of the rows that are settled — nothing more will ever be
    done for them. A row naming a request that has no event yet is not
    settled within ``REQUEST_GRACE_SECONDS``. Idempotent: an event names the
    row it took (``reset_id``)."""
    from app.models.activity import ActivityEvent
    from app.services.activity import emit_event

    rows = reset_rows(state)
    if not rows:
        return set()
    now = now or datetime.now(timezone.utc)
    events = (await session.exec(
        select(ActivityEvent)
        .where(ActivityEvent.agent_id == agent.id,
               ActivityEvent.event_type == CHAT_SESSION_RESET_EVENT)
        .order_by(ActivityEvent.created_at.desc())
        .limit(_EVENTS_READ_MAX)
    )).all()
    by_request: dict[str, Any] = {}
    taken: set[str] = set()
    for event in events:
        detail = event.detail or {}
        if isinstance(detail.get("request_id"), str):
            by_request.setdefault(detail["request_id"], event)
        if isinstance(detail.get("reset_id"), str):
            taken.add(detail["reset_id"])

    settled: set[str] = set()
    for row in rows:
        if row["id"] in taken:
            settled.add(row["id"])
            continue
        request_id = row.get("requestId")
        event = by_request.get(request_id) if request_id else None
        if event is not None and not (event.detail or {}).get("reset_id"):
            # The request's own event: what became of it.
            detail = {**(event.detail or {}), **_row_fields(row), "outcome": row["outcome"]}
            if detail.get("previous_session_id") is None:
                detail["previous_session_id"] = (event.detail or {}).get("previous_session_id")
            event.detail = detail
            event.title = reset_title(detail)
            session.add(event)
            await session.commit()
            taken.add(row["id"])
            settled.add(row["id"])
            continue
        if row["outcome"] != "done":
            # Nothing changed in the chat, and no request of MC is waiting.
            settled.add(row["id"])
            continue
        if request_id and event is None:
            age = _age_seconds(row, now)
            if age is not None and age < REQUEST_GRACE_SECONDS:
                continue  # the router's own write may still come
        if row["via"] == "new_session":
            # No request id: not MC. A request id MC has no (free) event for:
            # nobody MC knows — a reused or invented id is no proof of MC.
            actor = {"actor": ACTOR_UNKNOWN if request_id else ACTOR_OUTSIDE_MC}
        else:
            actor = {"actor": ACTOR_SYSTEM}
        detail = {
            **actor,
            "via": _DAEMON_VIAS[row["via"]],
            "command": None,
            "handled_by": HANDLED_BY_DAEMON,
            "outcome": row["outcome"],
            "queued": False,
            "request_id": request_id,
            "queue_id": None,
            **_row_fields(row),
        }
        try:
            await emit_event(session, CHAT_SESSION_RESET_EVENT, reset_title(detail),
                             severity="warning", agent_id=agent.id, detail=detail)
        except Exception:  # noqa: BLE001 — the next pass tries again
            logger.warning("chat reset audit: event not written (agent=%s, row=%s)",
                           getattr(agent, "slug", None), row["id"], exc_info=True)
            await session.rollback()
            continue
        taken.add(row["id"])
        settled.add(row["id"])
    return settled


class ChatResetAudit:
    """The background pass over every headless agent's state mirror."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        # Rows already settled, per agent: an unchanged mirror costs no
        # database read. In memory only — after a restart the first pass
        # reads the events once (``reset_id`` keeps it idempotent).
        self._settled: dict[str, set[str]] = {}

    async def sweep_once(self, session) -> int:
        """One pass. Returns how many rows were newly settled."""
        from app.models.agent import Agent

        agents = (await session.exec(select(Agent))).all()
        count = 0
        for agent in agents:
            if headless_chat_kind(agent) is None:
                continue
            key = str(agent.id)
            try:
                state = await asyncio.to_thread(read_acp_chat_state, agent)
            except Exception:  # noqa: BLE001 — one unreadable agent never stops the pass
                logger.debug("chat reset audit: state of %s unreadable", key, exc_info=True)
                continue
            rows = reset_rows(state)
            known = self._settled.get(key, set())
            if all(r["id"] in known for r in rows):
                continue
            settled = await reconcile_agent(session, agent, state)
            count += len(settled - known)
            # Only rows still in the mirror — the set never grows past it.
            self._settled[key] = {r["id"] for r in rows} & (known | settled)
        return count

    async def start(self) -> None:
        interval = settings.chat_reset_audit_interval
        if self._running or not interval or interval <= 0 or interval >= 99999:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval), name="chat_reset_audit")
        logger.info("chat reset audit started (interval=%ss)", interval)

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
        from sqlmodel.ext.asyncio.session import AsyncSession

        from app.database import engine

        while self._running:
            await asyncio.sleep(interval)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    await self.sweep_once(session)
            except Exception:  # noqa: BLE001 — never kill the loop
                logger.exception("chat reset audit pass failed")


chat_reset_audit = ChatResetAudit()
