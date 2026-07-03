"""
Dispatch Delivery — Runtime-specific delivery extracted from dispatch.auto_dispatch_task (REF-01 Step 3).

Owns the three delivery branches (claude-code / cli-bridge / host) that
hand the dispatch message off to the agent runtime. Each branch is
behavior-preserving — same DB writes, same event emission. Post Phase 29
(Gateway-Sunset), the legacy openclaw RPC branch is gone; an "unsupported
runtime" path replaces it as the final else (queues + logs + emits).

Module-access pattern (Pitfall A safety):
    Tests patch `app.services.dispatch.settings` + `app.services.dispatch.engine`.
    To make patches flow through this sibling module without a parallel
    patch, all settings accesses go through `app.services.dispatch`
    namespace via a lazy import — the patched attribute on the dispatch
    module is read by attribute access each call.

Source: backend/app/services/dispatch.py (Phase 4 REF-01 Step 3 Bottom-Up Extraction).
"""
from __future__ import annotations

import logging
import uuid

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger(__name__)


async def _check_runtime_readiness(
    task: Task,
    agent: Agent,
    session: AsyncSession,
    board_id: uuid.UUID,
    agent_id_str: str,
) -> bool:
    """Structured check whether the runtime is ready.

    Post Phase 29 / Gateway-Sunset: all remaining runtimes (cli-bridge, host,
    claude-code, free-code-bridge, manual) self-poll via their own runners.
    No readiness gate needed — return True unconditionally. The cli-bridge
    poll.sh / launchd-host loops act as the de-facto liveness check.

    Signature preserved for caller compatibility (auto_dispatch_task in
    dispatch.py + any future shim). The async signature is kept so callers
    don't need to drop `await`.
    """
    return True


async def _deliver_dispatch_message(
    task: Task,
    agent: Agent,
    message: str,
    session: AsyncSession,
    board_id: uuid.UUID,
    agent_id_str: str,
) -> str:
    """Hand off the dispatch message to the agent runtime (runner-only).

    Returns the resulting `dispatch_mode` string ("claude_code", "cli_bridge",
    "host_poll", "push_pending"). Post Phase 29 / Gateway-Sunset: the
    openclaw RPC `else:` branch is gone — only cli-bridge / host /
    claude-code runners remain (each agent has its own poll-loop).

    For an unrecognized runtime: log an error and return "push_pending"
    (caller treats as unsent; watchdog / next dispatch tick retries).
    """
    # Lazy import: dispatch.settings is the patched attribute in subagent
    # dispatch tests. Reading it via the module ensures patches flow through.
    from app.services import dispatch as _disp

    settings = _disp.settings

    dispatch_mode = "push"

    if getattr(agent, "agent_runtime", "openclaw") == "claude-code":
        from app.services.claude_code_runner import dispatch_to_claude_code
        try:
            started = await dispatch_to_claude_code(agent, task, message, session)
            if started:
                dispatch_mode = "claude_code"
                task.dispatched_at = utcnow()
                task.updated_at = utcnow()
                agent.run_state = "running"
                agent.last_dispatch_error = None
                session.add(task)
                session.add(agent)
                await session.commit()
            else:
                agent.last_dispatch_error = "Claude Code CLI start failed"
                session.add(agent)
                logger.warning("Claude Code start failed for %s", agent.name)
        except Exception as e:
            agent.last_dispatch_error = str(e)[:500]
            session.add(agent)
            logger.warning("Claude Code dispatch failed for %s: %s", agent.name, e)
    elif getattr(agent, "agent_runtime", "openclaw") == "cli-bridge":
        # ── CLI Bridge Dispatch ──
        # Workspace setup only. Task stays inbox.
        # poll.sh in the container picks it up via /me/next-task.
        from app.services.cli_bridge_runner import dispatch_to_cli_bridge
        try:
            prepared = await dispatch_to_cli_bridge(agent, task, message, session)
            if prepared:
                dispatch_mode = "cli_bridge"
                # Set dispatched_at so _check_undispatched_tasks doesn't touch
                # the task again via workspace setup every 30s. Without this
                # flag the watchdog finds dispatched_at=NULL and re-dispatches —
                # combined with poll.sh restarts (LAST_DISPATCHED_ATTEMPT_ID reset)
                # that produces the multi-fire bug (task arrives at the agent multiple times).
                task.dispatched_at = utcnow()
                task.updated_at = utcnow()
                agent.last_dispatch_error = None
                session.add(task)
                session.add(agent)
                await session.commit()
            else:
                agent.last_dispatch_error = "CLI bridge workspace setup failed"
                session.add(agent)
                logger.warning("CLI bridge workspace failed for %s", agent.name)
        except Exception as e:
            agent.last_dispatch_error = str(e)[:500]
            session.add(agent)
            logger.warning("CLI bridge dispatch failed for %s: %s", agent.name, e)
    elif getattr(agent, "agent_runtime", "openclaw") == "host":
        # ── Host Runtime (e.g. Boss via launchd, ADR-014) ──
        # No workspace setup needed (orchestrator doesn't work locally).
        # Task stays inbox — poll.sh on the host claims it via /agent/me/poll
        # and sends the prompt to the tmux session.
        dispatch_mode = "host_poll"
        task.dispatched_at = utcnow()
        task.updated_at = utcnow()
        if settings.enable_dispatch_gating and task.dispatch_phase is not None:
            task.dispatch_phase = None
        agent.run_state = "running"
        agent.last_dispatch_error = None
        session.add(task)
        session.add(agent)
        await session.commit()
    else:
        # ── Unknown / unsupported runtime ──
        # Post Phase 29 / Gateway-Sunset: only cli-bridge / host / claude-code
        # are reachable. Anything else lands here — log + queue for retry.
        runtime = getattr(agent, "agent_runtime", "openclaw")
        from app.services.task_queue import enqueue_pending_dispatch
        dispatch_mode = "push_pending"
        await enqueue_pending_dispatch(agent_id_str, str(task.id))
        agent.last_dispatch_error = f"unsupported_runtime:{runtime}"
        session.add(agent)
        logger.error(
            "No dispatcher available for runtime '%s' on agent %s — dispatch aborted",
            runtime, agent.id,
        )
        await emit_event(
            session, "task.dispatch_runtime_unsupported",
            f"Dispatch nicht moeglich: {agent.name} hat Runtime '{runtime}' (kein Dispatcher).",
            board_id=board_id, task_id=task.id, agent_id=agent.id,
            severity="warning",
            detail={"runtime": runtime, "agent_name": agent.name},
        )

    return dispatch_mode
