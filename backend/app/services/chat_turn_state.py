"""Turn state per chat, derived on read from the chat's session file.

Why on read and not written by the live recorder: the recorder lives inside
the SSE tailer, which runs only while somebody has that agent's chat open
and follows only the agent's newest file. The chats a chat list must show a
state for are exactly the ones nobody watches. The session file is the
harness's own truth and is there whether anyone watches or not, so the
state is read from it each time (cached per file version).

States:

* ``busy``  — the last content line belongs to a running turn;
* ``idle``  — it ends the turn (see :func:`_claude_turn_ended` /
  :func:`_omp_turn_ended`), or the file has no conversation line at all;
* ``stale`` — not ended, but nothing was written for ``TURN_STALE_SECONDS``:
  the process may be gone, or one tool call runs very long. The UI says
  "no sign of life since ``at``" — never "working" for ever;
* ``None``  — no session file found (unknown, never guessed as "ready").

``at`` is the time of the last write to the file: for ``idle`` about when
the turn ended, for ``busy`` / ``stale`` the last sign of life.

Known gap: a turn that waits for the operator (permission prompt, question
card) reads as ``busy`` — the prompt is not in the transcript.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)

TURN_BUSY = "busy"
TURN_IDLE = "idle"
TURN_STALE = "stale"

#: A turn that has not ended and whose file has been quiet this long is
#: ``stale``. Long enough for a slow build or test run, short enough that a
#: dead process does not read as "working" for the evening.
TURN_STALE_SECONDS = 15 * 60

_TAIL_BYTES = 262_144
_SCAN_LINES = 50

#: User lines that are not a request to the model: slash-command echoes.
_COMMAND_ECHO = ("<command-name>", "<command-message>", "<local-command-")
#: Claude Code's marker when the operator stops a turn (Esc).
_INTERRUPT = "[Request interrupted by user"
#: Final ``stop_reason`` values besides ``end_turn``: the API answer is
#: over and Claude Code does not go on by itself.
_FINAL_STOPS = frozenset({"max_tokens", "stop_sequence", "refusal"})

#: (path, mtime_ns, size, harness) -> ended?  Bounded; one entry per file
#: version, so an unchanged file is never re-read.
_memo: dict[tuple[str, int, int, str], bool] = {}
_MEMO_MAX = 1024

UNKNOWN: dict[str, Any] = {"state": None, "at": None}


def _tail_lines(path: Path) -> list[bytes]:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        fh.seek(max(0, size - _TAIL_BYTES))
        tail = fh.read()
    return [ln for ln in tail.split(b"\n") if ln.strip()]


def _texts(content: Any) -> list[str]:
    """Plain texts of a Claude message ``content`` (string, text blocks,
    and string / text-block contents of tool results)."""
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append(block["text"])
            elif block.get("type") == "tool_result":
                out.extend(_texts(block.get("content")))
    return out


def _claude_turn_ended(path: Path) -> bool:
    """Claude Code (and its fork openclaude): is the main turn over?

    Walks back from the end to the last line that carries turn meaning.
    Skipped: non user/assistant lines (system, hook and duration lines),
    ``isMeta`` and ``isCompactSummary`` lines (a summary is not an operator
    request — after an idle ``/compact`` the turn before decides, inside an
    auto-compact the running turn does), helper (sidechain) lines and
    slash-command echoes.

    Ended: an interrupt marker; an API error line; an answer with
    ``end_turn`` and a text block (the rule of
    ``ChatTailerManager._transcript_suggests_turn_ended``, measured there);
    an answer with another final stop reason. No meaningful line in the
    tail at all = nothing asked = ended. An unreadable line = not ended (a
    huge tool result is the usual reason; the stale rule catches the rest).
    """
    for raw in list(reversed(_tail_lines(path)))[:_SCAN_LINES]:
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return False
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        if etype not in ("user", "assistant"):
            continue
        if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if etype == "user":
            texts = _texts(content)
            if any(t.lstrip().startswith(_INTERRUPT) for t in texts):
                return True
            if texts and all(t.lstrip().startswith(_COMMAND_ECHO) for t in texts):
                continue
            return False
        if entry.get("isApiErrorMessage"):
            return True
        types = {b.get("type") for b in content if isinstance(b, dict)} if isinstance(content, list) else set()
        if "tool_use" in types:
            return False
        stop = message.get("stop_reason")
        if stop == "end_turn":
            return "text" in types
        return stop in _FINAL_STOPS
    return True


def _omp_turn_ended(path: Path) -> bool:
    """omp / Hermes: the last ``message`` line decides — an assistant line
    with a terminal ``stopReason`` (stop / error / aborted, the mapping of
    ``omp_chat.transcript_suggests_turn_ended``) ends the turn; user, tool
    results and ``toolUse`` / ``length`` go on. No message line = idle."""
    from app.services.omp_chat import _TERMINAL_STOP_REASONS

    for raw in list(reversed(_tail_lines(path)))[:_SCAN_LINES]:
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return False
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            return False
        if message.get("role") != "assistant":
            return False
        return message.get("stopReason") in _TERMINAL_STOP_REASONS
    return True


def _scanner_for(harness: str | None):
    """The turn-end scan for a harness, chosen through its transcript
    adapter (no harness names here): the omp-format adapter (omp, Hermes)
    gets :func:`_omp_turn_ended`, every Claude-Code-format one
    :func:`_claude_turn_ended`."""
    from types import SimpleNamespace

    from app.services import omp_chat
    from app.services.transcript_adapters import adapter_for

    adapter = adapter_for(SimpleNamespace(harness=harness))
    if adapter.transcript_suggests_turn_ended is omp_chat.transcript_suggests_turn_ended:
        return _omp_turn_ended
    return _claude_turn_ended


def file_turn_state(path: Path, harness: str | None, *, now: float | None = None) -> dict[str, Any]:
    """``{"state", "at"}`` of one session file (see module doc). Sync; the
    caller wraps it in a thread. Never raises."""
    try:
        st = path.stat()
    except OSError:
        return dict(UNKNOWN)
    scan = _scanner_for(harness)
    key = (str(path), st.st_mtime_ns, st.st_size, scan.__name__)
    ended = _memo.get(key)
    if ended is None:
        try:
            ended = scan(path)
        except OSError:
            return dict(UNKNOWN)
        if len(_memo) >= _MEMO_MAX:
            _memo.clear()
        _memo[key] = ended
    at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    if ended:
        state = TURN_IDLE
    elif (now if now is not None else time.time()) - st.st_mtime > TURN_STALE_SECONDS:
        state = TURN_STALE
    else:
        state = TURN_BUSY
    return {"state": state, "at": at.isoformat()}


# ── Which file is a chat's current session ─────────────────────────────────


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def _resolve(plan: list[dict[str, Any]]) -> dict[uuid.UUID, dict[str, Any]]:
    """File work for :func:`turn_states` (runs in a thread)."""
    from app.services.chat_record_import import session_files
    from app.services.transcript_adapters import adapter_for

    out: dict[uuid.UUID, dict[str, Any]] = {}
    for group in plan:
        agent = group["agent"]
        adapter = tdir = active = None
        by_native: dict[str, Path] | None = None
        if agent is not None:
            try:
                adapter = adapter_for(agent)
                tdir = adapter.resolve_transcript_dir(agent)
            except Exception:
                logger.debug("turn state: no transcript dir", exc_info=True)
                adapter = tdir = None
        for item in group["chats"]:
            # (path, harness) candidates; the newest file is the current one.
            # Rows the record knows passed the privacy gate when imported;
            # files found here pass it now.
            cands: list[tuple[Path, str | None]] = [(Path(p), h) for p, h in item["rows"]]
            if adapter is not None and tdir is not None:
                try:
                    if item["default"]:
                        if active is None:
                            found = adapter.find_active_session(tdir)
                            active = found[0] if found else False
                        pick = active or None
                        if pick is not None and adapter.session_id_for(pick) in group["pinned"]:
                            # The agent's newest session belongs to a further
                            # chat: the default chat's own newest file decides.
                            if by_native is None:
                                by_native = {adapter.session_id_for(p): p for p in session_files(adapter, tdir)}
                            mine = [p for n, p in by_native.items() if n not in group["pinned"]]
                            pick = max(mine, key=_mtime) if mine else None
                        if pick is not None and adapter.transcript_allowed(agent, pick):
                            cands.append((pick, adapter.name))
                    elif item["native"] and item["native"] not in item["row_natives"]:
                        if by_native is None:
                            by_native = {adapter.session_id_for(p): p for p in session_files(adapter, tdir)}
                        hit = by_native.get(item["native"])
                        if hit is not None and adapter.transcript_allowed(agent, hit):
                            cands.append((hit, adapter.name))
                except Exception:
                    logger.debug("turn state: session lookup failed", exc_info=True)
            cands = [c for c in cands if _mtime(c[0]) >= 0]
            if not cands:
                out[item["id"]] = dict(UNKNOWN)
                continue
            path, harness = max(cands, key=lambda c: _mtime(c[0]))
            out[item["id"]] = file_turn_state(path, harness or item["harness"])
    return out


async def turn_states(session: AsyncSession, chats: list[Any]) -> dict[uuid.UUID, dict[str, Any]]:
    """``{chat_id: {"state", "at"}}`` for ``chats``, read from each chat's
    current session file: the newest of the files the record knows for the
    chat, the agent's active file for its default chat (unless another chat
    of the agent pinned that session), and the further chat's own native
    session file before the record has a row for it."""
    if not chats:
        return {}
    from app.models.agent import Agent
    from app.models.chat_record import Chat, ChatSession

    ids = [c.id for c in chats]
    rows = (await session.exec(
        select(ChatSession.chat_id, ChatSession.transcript_path, ChatSession.harness,
               ChatSession.native_session_id)
        .where(ChatSession.chat_id.in_(ids), ChatSession.transcript_path.is_not(None))
    )).all()
    by_chat: dict[uuid.UUID, list[tuple[str, str | None, str]]] = {}
    for chat_id, tpath, harness, native in rows:
        by_chat.setdefault(chat_id, []).append((tpath, harness, native))

    agent_ids = {c.legacy_agent_id for c in chats if c.legacy_agent_id}
    agents: dict[uuid.UUID, Any] = {}
    pinned: dict[uuid.UUID, set[str]] = {}
    if agent_ids:
        for agent in (await session.exec(select(Agent).where(Agent.id.in_(agent_ids)))).all():
            agents[agent.id] = agent
        for agent_id, native in (await session.exec(
            select(Chat.legacy_agent_id, Chat.native_session_id).where(
                Chat.legacy_agent_id.in_(agent_ids), Chat.native_session_id.is_not(None)
            )
        )).all():
            pinned.setdefault(agent_id, set()).add(native)

    groups: dict[Any, dict[str, Any]] = {}
    for chat in chats:
        aid = chat.legacy_agent_id
        group = groups.setdefault(
            aid, {"agent": agents.get(aid) if aid else None, "pinned": pinned.get(aid, set()), "chats": []}
        )
        mine = by_chat.get(chat.id, [])
        group["chats"].append({
            "id": chat.id,
            "harness": chat.harness,
            "default": bool(chat.agent_default),
            "native": chat.native_session_id,
            "rows": [(p, h) for p, h, _n in mine],
            "row_natives": {n for _p, _h, n in mine},
        })
    try:
        return await asyncio.to_thread(_resolve, list(groups.values()))
    except Exception:  # noqa: BLE001 — a state lookup never breaks the list
        logger.warning("turn state lookup failed", exc_info=True)
        return {c.id: dict(UNKNOWN) for c in chats}


def with_state(out: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    st = state or UNKNOWN
    out["turnState"] = st["state"]
    out["turnStateAt"] = st["at"]
    return out


__all__ = [
    "TURN_BUSY", "TURN_IDLE", "TURN_STALE", "TURN_STALE_SECONDS",
    "file_turn_state", "turn_states", "with_state",
]
