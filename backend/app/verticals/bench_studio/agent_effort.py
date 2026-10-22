"""Agent-path effort, recorded — not set (bench W0.7).

A bench task on an omp agent runs in a NEW omp session of its own (the ACP
bridge's task session, beside the chat). That session starts on omp's
default thinking level ("high" for the fleet model) and the chat's effort
switch does not reach it — so the bench cannot set effort there (operator
decision D5: shown, not settable) but it can READ which level the session
really ran on: omp writes a ``thinking_level_change`` line at session start
(``{"thinkingLevel": "high", "configured": null}`` — ``configured: null`` =
nobody chose it, the session default).

The task's session files are found the way the chat's task steps find them
(``omp_chat.find_task_sessions``: the bridge copy carries the ``taskId``);
the level line lives in omp's OWN file of the same session
(``omp_chat.session_files``) — the bridge copy never has it.

Result shape, stored as ``entry.metrics["effort_observed"]``::

    {"source": "omp_session", "level": "high", "configured": None,
     "changed": False, "attempts": 1}
    {"source": "unknown", "reason": "..."}        # nothing readable
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("mc.bench_studio")

_LEVEL_MARKER = b'"thinking_level_change"'
#: Same slack the chat's task steps use before task creation (clock skew
#: between container and host).
_SINCE_SLACK = timedelta(minutes=5)


def _levels_in(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if _LEVEL_MARKER not in raw:
                    continue
                try:
                    d = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "thinking_level_change":
                    continue
                level = d.get("thinkingLevel")
                if isinstance(level, str) and level:
                    out.append({"level": level, "configured": d.get("configured")})
    except OSError:
        logger.debug("bench agent effort: %s not readable", path, exc_info=True)
    return out


def omp_task_effort(agent: Any, task_id: str, since: float | None = None) -> dict[str, Any]:
    """The thinking level the omp session of ``task_id`` ran on. Sync (file
    reads) — call in a thread. Never raises."""
    from app.services import omp_chat

    try:
        tdir = omp_chat.resolve_transcript_dir(agent)
        if tdir is None:
            return {"source": "unknown", "reason": "no omp transcript folder for this agent"}
        files = omp_chat.find_task_sessions(tdir, task_id, since)
        if not files:
            return {"source": "unknown", "reason": "no omp task session found"}
        newest = files[-1].path
        candidates = [newest] + [p for p in omp_chat.session_files(newest) if p != newest]
        levels: list[dict[str, Any]] = []
        for path in candidates:
            if not omp_chat.transcript_allowed(agent, path):
                continue
            levels = _levels_in(path)
            if levels:
                break
        if not levels:
            return {"source": "unknown", "reason": "omp session has no thinking level line"}
        last = levels[-1]
        return {
            "source": "omp_session",
            "level": last["level"],
            "configured": levels[0]["configured"],
            "changed": len({lv["level"] for lv in levels}) > 1,
            "attempts": len(files),
        }
    except Exception:  # noqa: BLE001 — recording must never fail the bench run
        logger.warning("bench agent effort: read failed for task %s", task_id, exc_info=True)
        return {"source": "unknown", "reason": "transcript read failed"}


async def observed_agent_effort(session, entry, task) -> dict[str, Any]:
    """``effort_observed`` for a finished agent entry (see module doc)."""
    from app.models.agent import Agent

    agent = await session.get(Agent, entry.agent_id) if entry.agent_id else None
    if agent is None:
        return {"source": "unknown", "reason": "agent gone"}
    harness = getattr(agent, "harness", None)
    if harness != "omp":
        return {"source": "unknown", "reason": f"effort of {harness or 'this'} sessions is not read yet"}
    created = getattr(task, "created_at", None)
    since = None
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        since = (created - _SINCE_SLACK).timestamp()
    return await asyncio.to_thread(omp_task_effort, agent, str(task.id), since)
