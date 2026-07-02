"""Runtime-Context Helpers — Workspace-Path Translation + Session-Reset Recap.

Konsolidiert Logik die sonst in dispatch.py / agent_scoped.py / watchdog
verstreut waere. Phase 4 wird dieses Modul zu services/work_context.py
expandieren (siehe ROADMAP § Phase 4 / D-13). Bis dahin: kleines, fokussiertes
Modul mit zwei oeffentlichen Funktionen.

Aufrufer (Phase 1 Plan 04 + Plan 05):
  - dispatch.py: workspace_path_for_runtime an Zeilen 955, 1751, 1783 (Plan 04)
  - dispatch.py: get_session_context_for_runtime an Zeile 2368 (Plan 05)
  - watchdog/session_monitor.py: get_session_context_for_runtime an Zeilen 254, 636 (Plan 05)
  - watchdog/task_monitor.py: get_session_context_for_runtime an Zeilen 735, 830, 892, 1442 (Plan 05)

Phase-1 Scope-Boundary (RESEARCH.md): nur 3 + 7 = 10 von 11+ bekannten
Call-Sites werden in Phase 1 migriert. Die restlichen 4 (meeting_service.py,
tasks.py:1330, agents.py:980, install_executor.py:607) gehen in Phase 4
zusammen mit dem dispatch.py / agent_scoped.py Split.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.agent import Agent
    from app.models.task import Task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# REL-06: Workspace-Path Translation
# ---------------------------------------------------------------------------


def workspace_path_for_runtime(
    agent: "Agent | None",
    raw_path: str | None,
) -> str | None:
    """Translate a host path to the agent's runtime view.

    Phase 1: thin wrapper over `dispatch._container_workspace_path()`.
    Phase 4: lift the implementation here. Until then, this is the
    single public entry point — direct imports of `_container_workspace_path`
    from outside dispatch.py are removed by REL-06.

    Argument order is `(agent, raw_path)` — agent-first, idiomatic flow:
    "for THIS agent, translate THIS path". The internal helper has the older
    `(host_path, agent)` order; the wrapper swaps internally.

    Returns:
        - For host runtime: the path as-is (passthrough), per existing helper.
        - For cli-bridge / openclaw runtime: container-perspective path or None
          if the host path lies outside the agent's mount (delegates to
          dispatch._container_workspace_path which has the ADR-023
          path-traversal guard).
    """
    # Lazy local import — runtime_context is imported BY dispatch (back-edge).
    # A top-level import would create a circular import at module-load time.
    from app.services.dispatch import _container_workspace_path

    return _container_workspace_path(raw_path, agent)


# ---------------------------------------------------------------------------
# REL-07: Session-Reset Context (Plan 05 fills get_session_context_for_runtime)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionContext:
    """Result of get_session_context_for_runtime() (Plan 05).

    Encodes ABSOLUTE VERBOTE (CLAUDE.md): wenn reset_required=True, MUSS
    recovery_recap non-empty sein (sonst Warnung im Log). Niemals Reset +
    truncated Prompt.
    """
    reset_required: bool
    recovery_recap: str | None
    session_key: str | None  # isolated worker-session id; None for the main session


async def get_session_context_for_runtime(
    agent: "Agent",
    task: "Task | None",
    *,
    reset_session: bool,
    session: "AsyncSession | None",
) -> SessionContext:
    """Build the session context for dispatch / resume / redispatch (REL-07).

    reset_session=False → SessionContext(False, None, None) — fast path, no recap.
    reset_session=True  → builds Structured Recovery Recap (CLAUDE.md ABSOLUTE
                          VERBOTE) and returns it for the caller to send AS THE
                          FIRST USER MESSAGE after the runtime session reset.

    The helper does NOT issue the dispatch send call itself. It computes the
    decision and the recap content only — the caller is responsible for the
    runtime-specific delivery (cli-bridge poll-loop / host process / claude-code
    socket). The helper's role is to make the recap-presence invariant grep-able
    and log-visible.

    When reset_session=True but task is None, the helper logs a WARNING and
    returns SessionContext(True, None, None). The caller MUST decide whether
    to (a) refuse to send (preferred), or (b) treat the full dispatch message
    as the recap (acceptable for the initial-dispatch flow at dispatch.py:2368
    where ``message`` is already a full structured dispatch — ABSOLUTE VERBOTE
    forbids truncated prompts, not full dispatch messages).
    """
    if not reset_session:
        return SessionContext(
            reset_required=False, recovery_recap=None, session_key=None,
        )

    if task is None:
        logger.warning(
            "get_session_context_for_runtime: reset_session=True without task — "
            "ABSOLUTE VERBOTE risk. Caller MUST ensure a recap is being sent. "
            "agent=%s",
            getattr(agent, "name", "unknown"),
        )
        return SessionContext(
            reset_required=True, recovery_recap=None, session_key=None,
        )

    # Bind the existing recap-builder method via self=None — the method only
    # reads from task/agent/session and does not touch self for any state
    # (verified by RESEARCH.md Assumption A5 + grep-and-read of the function
    # body in session_monitor.py:454-506). Phase 4 lifts this method into a
    # free function in this module.
    # Lazy local import — runtime_context is imported BY watchdog modules
    # (back-edge). A top-level import would create a circular import.
    from app.services.watchdog.session_monitor import SessionMonitorMixin
    recap = await SessionMonitorMixin._build_recovery_recap(  # type: ignore[arg-type]
        None, task, agent, session,
    )
    return SessionContext(
        reset_required=True, recovery_recap=recap, session_key=None,
    )
