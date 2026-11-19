"""Switching a chat's pair mid-conversation (ADR-105, plan S3 §2–§3).

A chat runs on one pair, harness × runtime × model. A switch moves it to
another pair without losing the conversation:

* **model only** (same harness, same runtime): no new process, no package
  — Claude Code gets ``/model <alias>``, omp ``config model``. The pair is
  written only when the first ``usage`` event after the request reports the
  new model (rule F12); a different model there = ``model_not_applied``.
* **restart** (other runtime or other harness): the old session ends, a new
  one starts and gets the handoff package (``chat_handoff``) built from
  MC's record. omp on the same harness tries ``session/load`` first.
* **switch back**: to the pair before the last switch, resuming that
  session natively; a package only for what happened in between.

Every switch is one ``chat_events`` row ``kind=switch`` (no migration),
written and COMMITTED before any process is touched (F14). Its payload
carries ``status`` pending → done | failed. The chat's pair and the end of
the old ``chat_sessions`` row are written only on success (F2); a failure
leaves the old session current, so ``resume_plan`` still resumes it.

Processes are started by a :class:`SwitchDriver` per harness. The claude
chat runner (S3 item A) and the omp daemon side (S3 item C) register their
own; until then the default driver serves only what exists today — the
model switch of an agent's own default chat — and refuses a restart with
``process_start_failed``. The harness side reports a restart's outcome
through :func:`report_result` (agent-token route).

Everything runs behind ``CHAT_RECORD_ENABLED`` (the router checks it).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.chat_record import Chat, ChatEvent, ChatSession
from app.services import chat_handoff, chat_record
from app.utils import utcnow

logger = logging.getLogger(__name__)

SWITCH_KIND = "switch"
#: A restart that never reported back is marked failed after this (F14).
ORPHAN_AFTER = timedelta(minutes=10)
#: What a client may ask for while a turn runs. ``after_turn`` needs a clean
#: turn-end signal per chat and comes later (plan §6) — the 409 only offers
#: what the server can do.
SUPPORTED_BUSY_MODES: tuple[str, ...] = ("stop_and_switch",)
#: Codes a failed switch may carry.
FAILURE_CODES = frozenset(
    {
        "process_start_failed",
        "new_session_failed",
        "model_unreachable",
        "provider_error",
        "model_not_applied",
        "model_not_offered",
        "orphaned",
    }
)
#: Codes the harness side may report (the rest are MC's own).
REPORTABLE_CODES = frozenset({"process_start_failed", "new_session_failed", "model_unreachable", "provider_error"})
#: Event kinds the package reads; everything else (thinking, usage, media,
#: tool output) stays out of the query.
_PACKAGE_KINDS = ("message", "tool", "todo", "elicitation", "elicitation_result")


class SwitchError(Exception):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"reason": self.code, "code": self.code, **self.extra}


class DriverUnavailable(Exception):
    """The harness has no process path for this chat (yet)."""


# ── Drivers ─────────────────────────────────────────────────────────────


class SwitchDriver(Protocol):
    async def is_busy(self, agent: Any, chat: Chat) -> bool | None:
        """``True`` while a turn runs, ``None`` when that cannot be read."""

    async def stop_turn(self, agent: Any, chat: Chat) -> None: ...

    async def set_model(self, agent: Any, chat: Chat, model: str) -> None:
        """Same harness, same runtime: change the model in the running session."""

    async def start(self, agent: Any, chat: Chat, request: dict[str, Any]) -> None:
        """End the chat's process and start the new session described by
        ``request`` (see :func:`_start_request`). Returns once the start is
        handed over; the outcome comes through :func:`report_result`."""


class AgentDefaultDriver:
    """What exists on main today: the agent's own process, which is the
    process of its DEFAULT chat. Further chats have no process of their own
    until S3 items A (Claude) and C (omp) register their drivers."""

    async def is_busy(self, agent: Any, chat: Chat) -> bool | None:
        if not chat.agent_default:
            return False  # nothing of this chat runs anywhere yet
        from app.services import agent_chat_input
        from app.services.acp_chat_transport import headless_chat_kind, read_acp_chat_state

        try:
            if headless_chat_kind(agent) is not None:
                state = read_acp_chat_state(agent)
                return isinstance(state, dict) and state.get("busy") is True
            return await agent_chat_input._pane_is_busy(agent)
        except Exception:  # noqa: BLE001 — unknown is reported as unknown
            logger.warning("chat switch: busy state of %s not readable", getattr(agent, "slug", None))
            return None

    async def stop_turn(self, agent: Any, chat: Chat) -> None:
        if not chat.agent_default:
            raise DriverUnavailable("no_chat_process")
        from app.services import agent_chat_input

        await agent_chat_input.send_keys(agent, ["Escape"])

    async def set_model(self, agent: Any, chat: Chat, model: str) -> None:
        if not chat.agent_default:
            raise DriverUnavailable("no_chat_process")
        from app.services import agent_chat_input
        from app.services.acp_chat_transport import headless_chat_kind

        if headless_chat_kind(agent) is not None:
            await agent_chat_input.set_model(agent, model)
        else:
            await agent_chat_input.send_text(agent, f"/model {model}")

    async def start(self, agent: Any, chat: Chat, request: dict[str, Any]) -> None:
        raise DriverUnavailable("no_chat_runner")


_DEFAULT_DRIVER = AgentDefaultDriver()
_DRIVERS: dict[str, SwitchDriver] = {}


def register_driver(harness: str, driver: SwitchDriver) -> None:
    """S3 items A/C plug their per-chat process path in here."""
    _DRIVERS[harness] = driver


def driver_for(harness: str | None) -> SwitchDriver:
    return _DRIVERS.get(harness or "", _DEFAULT_DRIVER)


# ── Box lock (F10) ──────────────────────────────────────────────────────


async def box_lock_for(runtime: Any) -> str | None:
    """Why a switch onto ``runtime`` must wait, or ``None``.

    A runtime whose engine does not serve its model needs a recipe switch
    on its box; that switch is refused while the box's engine still answers
    requests (``box_guard.switch_lock``, PRINCIPLES §8 rule 6) or while a
    switch is already running there. Fail-open like the lock itself: an
    unreadable state does not block."""
    if runtime is None or getattr(runtime, "host_id", None) is None:
        return None
    try:
        from app.redis_client import get_redis
        from app.services.box_status import _engine_state, _live_snapshot
        from app.services.heads import box_guard

        snapshot = await _live_snapshot(await get_redis(), runtime.slug)
        state = _engine_state(snapshot, runtime)
        if state == "switching":
            return "a model switch is already running on this box"
        if state not in ("not_loaded", "not_serving"):
            return None
        refusal, _unknown = await box_guard.switch_lock([runtime])
        return refusal.get("message") if refusal else None
    except Exception:  # noqa: BLE001 — fail-open, same as box_guard
        logger.warning("chat switch: box lock of %s not readable", getattr(runtime, "slug", None))
        return None


# ── Record helpers ──────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _current_session(session: AsyncSession, chat: Chat) -> ChatSession | None:
    """The session the chat runs on: the pinned one, else the newest open one."""
    if chat.native_session_id:
        row = (
            await session.exec(
                select(ChatSession).where(
                    ChatSession.chat_id == chat.id,
                    ChatSession.native_session_id == chat.native_session_id,
                )
            )
        ).first()
        if row is not None:
            return row
    rows = (
        await session.exec(
            select(ChatSession).where(ChatSession.chat_id == chat.id).order_by(ChatSession.created_at.desc())
        )
    ).all()
    for row in rows:
        if row.ended_at is None:
            return row
    return rows[0] if rows else None


async def _switch_rows(session: AsyncSession, chat_id: uuid.UUID) -> list[ChatEvent]:
    return list(
        (
            await session.exec(
                select(ChatEvent)
                .where(ChatEvent.chat_id == chat_id, ChatEvent.kind == SWITCH_KIND)
                .order_by(ChatEvent.seq)
            )
        ).all()
    )


async def _write(session: AsyncSession, chat_id: uuid.UUID, session_row_id: uuid.UUID, row_key: str, payload: dict) -> None:
    await chat_record.upsert_events(session, chat_id, session_row_id, [(row_key, payload)])


async def _set_payload(session: AsyncSession, row: ChatEvent, **changes: Any) -> dict[str, Any]:
    payload = {**(row.payload or {}), **changes}
    await _write(session, row.chat_id, row.session_id, row.source_key, payload)
    row.payload = payload
    return payload


def _pair_of(chat: Chat, row: ChatSession | None) -> chat_handoff.Pair:
    return chat_handoff.Pair(
        harness=chat.harness,
        runtime_id=str(chat.runtime_id) if chat.runtime_id else None,
        model=chat.model or (row.model if row else None),
        session_id=row.native_session_id if row else chat.native_session_id,
    )


def switch_out(row: ChatEvent) -> dict[str, Any]:
    return {"eventId": str(row.id), "seq": row.seq, **(row.payload or {})}


async def _record_events(session: AsyncSession, chat_id: uuid.UUID, *, after_seq: int = 0,
                         exclude_session: uuid.UUID | None = None) -> list[dict[str, Any]]:
    q = (
        select(ChatEvent.payload)
        .where(ChatEvent.chat_id == chat_id, ChatEvent.kind.in_(_PACKAGE_KINDS), ChatEvent.seq > after_seq)
        .order_by(ChatEvent.seq)
    )
    if exclude_session is not None:
        q = q.where(ChatEvent.session_id != exclude_session)
    return [p for p in (await session.exec(q)).all() if isinstance(p, dict)]


async def _runtime(session: AsyncSession, runtime_id: uuid.UUID | None):
    if runtime_id is None:
        return None
    from app.models.runtime import Runtime

    return await session.get(Runtime, runtime_id)


async def _agent(session: AsyncSession, chat: Chat):
    if chat.legacy_agent_id is None:
        return None
    from app.models.agent import Agent

    return await session.get(Agent, chat.legacy_agent_id)


# ── Lazy upkeep: model-only reconcile (F12) and orphans (F14) ───────────


_ALIAS_SEP = r"[-_/.:\s]"


def model_matches(requested: str, seen: str | None) -> bool:
    """Does the model a ``usage`` event reports match the one asked for?
    Claude aliases (``sonnet``) match the full id (``claude-sonnet-4-6``);
    omp ``provider/model`` matches either side's tail."""
    if not seen:
        return False
    r, s = requested.strip().lower(), seen.strip().lower()
    if not r:
        return False
    if r == s or s.rsplit("/", 1)[-1] == r.rsplit("/", 1)[-1]:
        return True
    return re.search(rf"(^|{_ALIAS_SEP}){re.escape(r)}({_ALIAS_SEP}|$)", s) is not None


async def _reconcile_model_switch(session: AsyncSession, chat: Chat, row: ChatEvent) -> bool:
    """A pending model-only switch: the first ``usage`` after it decides."""
    payload = row.payload or {}
    switched_at = _aware(chat_record.parse_ts(payload.get("ts")))
    usages = (
        await session.exec(
            select(ChatEvent)
            .where(ChatEvent.chat_id == chat.id, ChatEvent.kind == "usage", ChatEvent.seq > row.seq)
            .order_by(ChatEvent.seq)
        )
    ).all()
    for usage in usages:
        ts = _aware(usage.ts)
        if switched_at is not None and ts is not None and ts < switched_at:
            continue  # a late import of an older turn, not the answer to the switch
        requested = (payload.get("to") or {}).get("model") or ""
        seen = (usage.payload or {}).get("model")
        if model_matches(requested, seen):
            chat.model = requested
            session.add(chat)
            await _set_payload(session, row, status="done", seenModel=seen, finishedAt=_iso(utcnow()))
        else:
            await _set_payload(
                session, row, status="failed", seenModel=seen, finishedAt=_iso(utcnow()),
                error={"code": "model_not_applied", "detail": f"the next answer came from {seen or 'an unknown model'}"},
            )
        return True
    return False


async def _upkeep(session: AsyncSession, chat: Chat, *, now: datetime | None = None) -> bool:
    """Settles the chat's pending switch rows that can be settled now.
    Returns whether anything changed (the caller commits)."""
    now = now or utcnow()
    changed = False
    for row in await _switch_rows(session, chat.id):
        payload = row.payload or {}
        if payload.get("status") != "pending":
            continue
        if payload.get("path") == "model_only":
            # Waits for the next answer as long as it takes — no orphan
            # timeout: the operator may simply not have typed yet.
            changed |= await _reconcile_model_switch(session, chat, row)
            continue
        started = _aware(chat_record.parse_ts(payload.get("ts")))
        if started is not None and now - started >= ORPHAN_AFTER:
            await _set_payload(
                session, row, status="failed", finishedAt=_iso(now),
                error={"code": "orphaned", "detail": "the harness never reported back"},
            )
            changed = True
    return changed


async def sweep_orphans(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Every chat's pending switches, once (for the background sweep).
    Returns the chats touched. Commits."""
    chat_ids = {
        cid
        for cid, payload in (
            await session.exec(select(ChatEvent.chat_id, ChatEvent.payload).where(ChatEvent.kind == SWITCH_KIND))
        ).all()
        if isinstance(payload, dict) and payload.get("status") == "pending"
    }
    touched = 0
    for cid in chat_ids:
        chat = await session.get(Chat, cid)
        if chat is not None and await _upkeep(session, chat, now=now):
            touched += 1
    if touched:
        await session.commit()
    return touched


# ── Requests ────────────────────────────────────────────────────────────


def _start_request(row: ChatEvent, chat: Chat, *, action: str, native: str | None,
                   try_native_load: bool, package: str | None, to: dict[str, Any]) -> dict[str, Any]:
    return {
        "switchEventId": str(row.id),
        "chatId": str(chat.id),
        "action": action,
        "nativeSessionId": native,
        "tryNativeLoad": try_native_load,
        "package": package,
        "to": to,
    }


async def _check_pair(session: AsyncSession, harness: str, runtime_id: uuid.UUID | None):
    from app.services import harness_compat

    if harness not in chat_record.RESUME_STYLE:
        raise SwitchError(400, "harness_cannot_hold_chats")
    runtime = await _runtime(session, runtime_id)
    if runtime_id is not None and runtime is None:
        raise SwitchError(404, "runtime_not_found")
    if runtime is None:
        if harness_compat.requires_runtime_binding(harness):
            raise SwitchError(400, "runtime_required")
        return None
    reason = harness_compat.incompat_reason(harness, runtime)
    if reason:
        raise SwitchError(400, "pair_incompatible", message=reason)
    return runtime


async def _guard_turn(driver: SwitchDriver, agent: Any, chat: Chat, mode: str | None) -> None:
    """F1 — never switch under a running turn. ``stop_and_switch`` stops it
    first; there is no force path."""
    busy = await driver.is_busy(agent, chat) if agent is not None else False
    if busy is False:
        return
    if mode == "stop_and_switch" and busy is True:
        try:
            await driver.stop_turn(agent, chat)
        except DriverUnavailable as exc:
            raise SwitchError(409, "cannot_stop_turn") from exc
        return
    raise SwitchError(
        409, "turn_running", options=list(SUPPORTED_BUSY_MODES), state="running" if busy else "unknown"
    )


async def _open_row(session: AsyncSession, chat: Chat, current: ChatSession, payload: dict[str, Any]) -> ChatEvent:
    key = f"switch:{uuid.uuid4()}"
    await _write(session, chat.id, current.id, key, payload)
    row = (
        await session.exec(select(ChatEvent).where(ChatEvent.chat_id == chat.id, ChatEvent.source_key == key))
    ).one()
    # F14: on disk before any process is touched.
    await session.commit()
    return row


async def _fail(session: AsyncSession, row: ChatEvent, code: str, detail: str | None = None) -> dict[str, Any]:
    payload = await _set_payload(
        session, row, status="failed", finishedAt=_iso(utcnow()), error={"code": code, "detail": detail}
    )
    await session.commit()
    return {"eventId": str(row.id), "seq": row.seq, **payload}


async def request_switch(
    session: AsyncSession,
    user: Any,
    chat: Chat,
    *,
    harness: str,
    runtime_id: uuid.UUID | None,
    model: str | None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Moves ``chat`` to the pair ``harness × runtime_id × model``.
    Commits. Raises :class:`SwitchError` for a refusal before anything was
    written; a failure after the row exists is reported in its status."""
    if mode is not None and mode not in SUPPORTED_BUSY_MODES:
        raise SwitchError(422, "mode_unsupported", options=list(SUPPORTED_BUSY_MODES))
    model = (model or "").strip() or None
    runtime = await _check_pair(session, harness, runtime_id)
    same_harness = harness == chat.harness
    same_runtime = runtime_id == chat.runtime_id
    if same_harness and same_runtime and (model is None or model == chat.model):
        raise SwitchError(400, "no_change")
    model_only = same_harness and same_runtime
    if chat.agent_default and not model_only:
        # The default chat IS the agent's process; moving it is the agent
        # runtime switch (agent_runtime_switch), not a chat switch.
        raise SwitchError(409, "default_chat_follows_agent")

    if await _upkeep(session, chat):
        await session.commit()
    if any((r.payload or {}).get("status") == "pending" for r in await _switch_rows(session, chat.id)):
        raise SwitchError(409, "switch_pending")

    agent = await _agent(session, chat)
    driver = driver_for(chat.harness if model_only else harness)
    await _guard_turn(driver_for(chat.harness), agent, chat, mode)
    if not same_runtime:
        lock = await box_lock_for(runtime)
        if lock:
            raise SwitchError(423, "box_locked", message=lock)

    current = await _current_session(session, chat)
    to = {"harness": harness, "runtimeId": str(runtime_id) if runtime_id else None, "model": model or (chat.model if model_only else None)}
    if current is None:
        # Never started: nothing to hand off and nothing to stop.
        if not same_harness:
            # The native id follows the new harness' rule (create_chat).
            style = chat_record.RESUME_STYLE.get(harness)
            chat.native_session_id = str(uuid.uuid4()) if style == "cli" else None
        chat.harness, chat.runtime_id, chat.model = harness, runtime_id, to["model"]
        session.add(chat)
        await session.commit()
        return {"eventId": None, "status": "done", "path": "not_started", "to": to}

    old = _pair_of(chat, current)
    now = utcnow()
    base: dict[str, Any] = {
        "kind": SWITCH_KIND,
        "uuid": str(uuid.uuid4()),
        "ts": _iso(now),
        "status": "pending",
        "mode": mode,
        "from": old.as_dict(),
        "to": to,
        "package": None,
        "estTokens": 0,
        "truncated": False,
        "messagesIncluded": 0,
        "messagesTotal": 0,
        "realTokens": None,
        "error": None,
    }
    if model_only:
        row = await _open_row(session, chat, current, {**base, "path": "model_only"})
        try:
            await driver.set_model(agent, chat, to["model"])
        except DriverUnavailable as exc:
            return await _fail(session, row, "process_start_failed", str(exc))
        except ValueError as exc:
            return await _fail(session, row, "model_not_offered", str(exc))
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed silently
            logger.warning("chat switch: model switch of %s failed: %s", chat.id, type(exc).__name__)
            return await _fail(session, row, "process_start_failed", type(exc).__name__)
        return switch_out(row)

    budget = chat_handoff.budget_for_window(chat_handoff.window_for(runtime, to["model"]))
    package = chat_handoff.build_handoff(
        await _record_events(session, chat.id),
        chat=chat,
        old_pair=old,
        new_pair=chat_handoff.Pair(harness=harness, runtime_id=to["runtimeId"], model=to["model"],
                                   runtime_label=getattr(runtime, "display_name", None)),
        budget_tokens=budget,
        cwd=current.cwd,
        today=now.date().isoformat(),
    )
    try_load = same_harness and chat_record.RESUME_STYLE.get(harness) == "acp" and bool(chat.native_session_id)
    if try_load:
        action, native = "resume", chat.native_session_id
    else:
        # Claude Code takes the id MC chooses (``--session-id``); ACP gets
        # one from ``session/new``.
        action = "start"
        native = str(uuid.uuid4()) if chat_record.RESUME_STYLE.get(harness) == "cli" else None
    row = await _open_row(session, chat, current, {
        **base, **package.as_dict(), "path": "restart", "action": action,
        "nativeSessionId": native, "tryNativeLoad": try_load,
    })
    return await _hand_over(session, driver, agent, chat, row, action=action, native=native,
                            try_load=try_load, package=package.text, to=to)


async def _hand_over(session, driver, agent, chat, row, *, action, native, try_load, package, to) -> dict[str, Any]:
    try:
        await driver.start(agent, chat, _start_request(
            row, chat, action=action, native=native, try_native_load=try_load, package=package, to=to,
        ))
    except DriverUnavailable as exc:
        return await _fail(session, row, "process_start_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat switch: start for %s failed: %s", chat.id, type(exc).__name__)
        return await _fail(session, row, "process_start_failed", type(exc).__name__)
    return switch_out(row)


async def switch_back(session: AsyncSession, user: Any, chat: Chat, *, mode: str | None = None) -> dict[str, Any]:
    """Back to the pair before the chat's last restart, resuming that
    session natively (F7). A package goes along only when the chat moved on
    in between (messages in other sessions after the old one's last event);
    after a failed switch there is none. Commits."""
    if mode is not None and mode not in SUPPORTED_BUSY_MODES:
        raise SwitchError(422, "mode_unsupported", options=list(SUPPORTED_BUSY_MODES))
    if await _upkeep(session, chat):
        await session.commit()
    rows = await _switch_rows(session, chat.id)
    if any((r.payload or {}).get("status") == "pending" for r in rows):
        raise SwitchError(409, "switch_pending")
    last = next(
        (r for r in reversed(rows)
         if (r.payload or {}).get("path") in ("restart", "switch_back") and (r.payload or {}).get("status") in ("done", "failed")),
        None,
    )
    if last is None:
        raise SwitchError(409, "nothing_to_switch_back_to")
    back = (last.payload or {}).get("from") or {}
    target_native = back.get("sessionId")
    harness = back.get("harness")
    if not target_native or harness not in chat_record.RESUME_STYLE:
        raise SwitchError(409, "nothing_to_switch_back_to")
    target = (
        await session.exec(
            select(ChatSession).where(ChatSession.chat_id == chat.id, ChatSession.native_session_id == target_native)
        )
    ).first()
    if target is None:
        raise SwitchError(409, "previous_session_gone")
    runtime_id = uuid.UUID(back["runtimeId"]) if back.get("runtimeId") else None
    await _check_pair(session, harness, runtime_id)

    agent = await _agent(session, chat)
    await _guard_turn(driver_for(chat.harness), agent, chat, mode)
    if runtime_id != chat.runtime_id:
        lock = await box_lock_for(await _runtime(session, runtime_id))
        if lock:
            raise SwitchError(423, "box_locked", message=lock)

    current = await _current_session(session, chat) or target
    last_seq = (
        await session.exec(select(ChatEvent.seq).where(ChatEvent.session_id == target.id).order_by(ChatEvent.seq.desc()))
    ).first() or 0
    between = await _record_events(session, chat.id, after_seq=last_seq, exclude_session=target.id)
    package = None
    if any(ev.get("kind") == "message" for ev in between):
        runtime = await _runtime(session, runtime_id)
        package = chat_handoff.build_handoff(
            between,
            chat=chat,
            old_pair=_pair_of(chat, current),
            new_pair=chat_handoff.Pair(harness=harness, runtime_id=back.get("runtimeId"), model=back.get("model")),
            budget_tokens=chat_handoff.budget_for_window(chat_handoff.window_for(runtime, back.get("model"))),
            cwd=target.cwd,
            today=utcnow().date().isoformat(),
        )
    to = {"harness": harness, "runtimeId": back.get("runtimeId"), "model": back.get("model")}
    now = utcnow()
    row = await _open_row(session, chat, current, {
        "kind": SWITCH_KIND,
        "uuid": str(uuid.uuid4()),
        "ts": _iso(now),
        "status": "pending",
        "path": "switch_back",
        "mode": mode,
        "action": "resume",
        "nativeSessionId": target_native,
        "tryNativeLoad": False,
        "from": _pair_of(chat, current).as_dict(),
        "to": to,
        **(package.as_dict() if package else
           {"package": None, "estTokens": 0, "truncated": False, "messagesIncluded": 0, "messagesTotal": 0}),
        "realTokens": None,
        "error": None,
    })
    return await _hand_over(session, driver_for(harness), agent, chat, row, action="resume", native=target_native,
                            try_load=False, package=package.text if package else None, to=to)


# ── The harness reports back ────────────────────────────────────────────


async def get_switch(session: AsyncSession, chat: Chat, event_id: uuid.UUID) -> dict[str, Any]:
    if await _upkeep(session, chat):
        await session.commit()
    row = await session.get(ChatEvent, event_id)
    if row is None or row.chat_id != chat.id or row.kind != SWITCH_KIND:
        raise SwitchError(404, "switch_not_found")
    return switch_out(row)


async def report_result(
    session: AsyncSession,
    chat: Chat,
    event_id: uuid.UUID,
    *,
    status: str,
    native_session_id: str | None = None,
    native_resume: bool = False,
    error: dict[str, Any] | None = None,
    real_tokens: int | None = None,
) -> dict[str, Any]:
    """The outcome of a restart, from the harness side: after the new
    session answered its first prompt (``done``), or the failure (F2, F6).
    Only ``done`` writes the pair, ends the old session and pins the new
    native id. Commits."""
    row = await session.get(ChatEvent, event_id)
    if row is None or row.chat_id != chat.id or row.kind != SWITCH_KIND:
        raise SwitchError(404, "switch_not_found")
    payload = row.payload or {}
    if payload.get("path") not in ("restart", "switch_back"):
        raise SwitchError(409, "switch_not_reportable")
    if payload.get("status") != "pending":
        if payload.get("status") == status:
            return switch_out(row)  # a retried report
        raise SwitchError(409, "switch_not_pending", current=payload.get("status"))
    if status not in ("done", "failed"):
        raise SwitchError(422, "bad_status")

    if status == "failed":
        code = (error or {}).get("code")
        if code not in REPORTABLE_CODES:
            raise SwitchError(422, "bad_error_code", allowed=sorted(REPORTABLE_CODES))
        detail = (error or {}).get("detail")
        return await _fail(session, row, code, str(detail)[:500] if detail else None)

    native = native_session_id or payload.get("nativeSessionId")
    if not native or not re.match(chat_record.NATIVE_ID_PATTERN, native):
        raise SwitchError(422, "native_session_required")
    if payload.get("path") == "switch_back" and native != payload.get("nativeSessionId"):
        raise SwitchError(409, "native_session_mismatch")
    other = (
        await session.exec(
            select(Chat.id).where(
                Chat.legacy_agent_id == chat.legacy_agent_id,
                Chat.native_session_id == native,
                Chat.id != chat.id,
            )
        )
    ).first()
    if other is not None:
        raise SwitchError(409, "pinned_by_other_chat")

    to = payload.get("to") or {}
    old = (await session.get(ChatSession, row.session_id))
    now = utcnow()
    runtime_id = uuid.UUID(to["runtimeId"]) if to.get("runtimeId") else None
    new = await chat_record.ensure_session(
        session, chat, native_session_id=native, harness=to.get("harness"), runtime_id=runtime_id,
        model=to.get("model"), place=old.place if old else None, transcript_path=None,
        cwd=old.cwd if old else None, started_at=now,
    )
    table = ChatSession.__table__
    if old is not None and old.id != new.id:
        await session.execute(update(table).where(table.c.id == old.id).values(ended_at=now, end_reason="switch"))
    # A session the chat comes back to is current again.
    await session.execute(update(table).where(table.c.id == new.id).values(ended_at=None, end_reason=None))
    # The one legitimate re-pin of a pinned chat: MC asked for it (ADR-105).
    chat.native_session_id = native
    chat.harness = to.get("harness")
    chat.runtime_id = runtime_id
    chat.model = to.get("model")
    session.add(chat)
    await session.flush()
    await chat_record.merge_strays(session, chat)
    done = await _set_payload(
        session, row, status="done", finishedAt=_iso(now), nativeSessionId=native,
        nativeResume=bool(native_resume), used=bool(payload.get("package")) and not native_resume,
        toSessionRowId=str(new.id), realTokens=real_tokens,
    )
    await session.commit()
    return {"eventId": str(row.id), "seq": row.seq, **done}
