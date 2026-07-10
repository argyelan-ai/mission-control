"""
Task Runner Service — monitors open tasks and makes sure agents work through them.

Runs as an asyncio background task in the FastAPI lifespan (like the watchdog).
Checks periodically:
- Dispatch ACK: was the task confirmed by the agent? Timeout → approval for the operator
- In-progress tasks with no activity → status check / escalation

Protection mechanisms:
- Board Leads (orchestrators) are NOT warned for stale progress —
  they delegate and wait, that's normal operation.
- Parent tasks with active subtasks are skipped —
  the parent waiting on its subtasks is not a stall.
- Dispatch timeouts create approvals instead of auto-reassignment —
  the operator decides manually what should happen.
- Stale-check circuit breaker: after MAX_STALE_CHECKS the agent is no
  longer warned — the operator is notified instead.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.task import Task, TaskComment
from app.utils import utcnow, ensure_aware
from app.redis_client import RedisKeys, get_redis
from app.scopes import Scope, get_agent_effective_scopes
from app.services.activity import emit_event
from app.services.dispatch import auto_dispatch_task

logger = logging.getLogger("mc.task_runner")

# Thresholds (in minutes)
DISPATCH_PENDING_WARN_MINUTES = 5     # Dispatch not successful (no dispatched_at)
DISPATCH_PENDING_TIMEOUT_MINUTES = 15 # Pending dispatch → re-assign
# ACK_TIMEOUT_MINUTES was replaced by per-runtime lookup (REL-05).
# See AGENT_RUNTIME_ACK_TIMEOUTS + _get_ack_timeout_minutes further below.
STALE_PROGRESS_MINUTES = 60          # Default when role is unknown

# Role-based idle thresholds (minutes).
# Workers are typically active on the order of minutes — longer idle = likely stuck.
# Orchestrators (Boss, Planner) delegate and wait on callbacks → need more time.
STALE_PROGRESS_MINUTES_BY_ROLE = {
    "developer": 15,
    "reviewer": 15,
    "designer": 15,
    "researcher": 20,
    "deployer": 15,
    "writer": 15,
    "automation": 20,
    "tester": 15,
    "orchestrator": 45,
    "planner": 45,
    "board_lead": 45,
}

# Circuit Breaker
MAX_STALE_CHECKS = 3       # Max status checks per task


def _idle_threshold_for(agent) -> int:
    """Idle threshold for an agent.

    Lookup priority (Phase 26, FND-06):
      1) agent.dispatch_config["idle_timeout_minutes"]    (Per-Agent Override, NEW — Migration 0097)
      2) agent.dispatch_config["stale_progress_minutes"]  (Per-Agent Override, EXISTING — backwards-compat)
      3) Role-based default from STALE_PROGRESS_MINUTES_BY_ROLE
      4) Hard fallback STALE_PROGRESS_MINUTES (60min)

    Pattern mirrors _get_ack_timeout_minutes (line ~131) — same shape, different key.
    Backwards-compat: agents without either dispatch_config key keep current behavior.
    """
    cfg = getattr(agent, "dispatch_config", None) or {}
    if isinstance(cfg, dict):
        # NEW: idle_timeout_minutes (Migration 0097, FND-06)
        if "idle_timeout_minutes" in cfg:
            return int(cfg["idle_timeout_minutes"])
        # EXISTING: stale_progress_minutes -- kept for backwards-compat
        if "stale_progress_minutes" in cfg:
            return int(cfg["stale_progress_minutes"])
    role = (agent.role or "").lower().strip()
    if role in STALE_PROGRESS_MINUTES_BY_ROLE:
        return STALE_PROGRESS_MINUTES_BY_ROLE[role]
    if getattr(agent, "is_board_lead", False):
        return STALE_PROGRESS_MINUTES_BY_ROLE["board_lead"]
    return STALE_PROGRESS_MINUTES


def _build_agent_stuck_description(
    task_title: str,
    agent_name: str,
    error_summary: str,
    timeline_events: list[tuple[str, str]] | None = None,
) -> str:
    """Human-readable description for agent_stuck approvals."""
    parts = [f"Agent braucht Hilfe: \"{task_title}\" ({agent_name})\n"]

    if timeline_events:
        parts.append("Was passiert ist:")
        for time_str, event in timeline_events:
            parts.append(f"- {time_str}: {event}")
        parts.append("")

    parts.append(f"Der Fehler:\n  {error_summary[:300]}\n")

    parts.append(
        "Was du tun kannst:\n"
        f"- Task einem anderen Agent zuweisen\n"
        f"- Dem Agent einen Hinweis als Kommentar geben\n"
        "- Task auf \"blocked\" setzen und spaeter anschauen"
    )
    return "\n".join(parts)


def _get_agent_timeout(agent, key: str, default: int) -> int:
    """Read an agent-specific timeout, falling back to the global default."""
    cfg = getattr(agent, "dispatch_config", None)
    return cfg.get(key, default) if cfg else default


# Per-Agent-Runtime ACK Timeouts (REL-05).
#
# Constants instead of DB — the existing `runtimes` table (ADR-017) is
# for LLM runtimes (LM Studio, Ollama Cloud, Anthropic API), not for
# agent_runtime types (host / cli-bridge / openclaw). If the operator later
# wants UI-editable values, that lands in Phase 6 (Context Management).
#
# Lookup order in _get_ack_timeout_minutes:
#   1) agent.dispatch_config["ack_timeout_minutes"]    (Per-Agent Override)
#   2) AGENT_RUNTIME_ACK_TIMEOUTS[agent.agent_runtime] (Runtime-Default)
#   3) _DEFAULT_ACK_TIMEOUT_MINUTES                    (Hard fallback = 5)
AGENT_RUNTIME_ACK_TIMEOUTS: dict[str, int] = {
    "host": 5,
    "cli-bridge": 15,
    "openclaw": 15,
}
_DEFAULT_ACK_TIMEOUT_MINUTES = 5


def _get_ack_timeout_minutes(agent) -> int:
    """3-step lookup for the ACK timeout (REL-05).

    Replaces the previously hardcoded 10-minute default. The old constant
    wasn't runtime-aware — host agents should have escalated much sooner,
    Docker agents need more time for cold-start.

    NB: deliberately SEPARATE from _get_agent_timeout — the generic helper is
    also used for max_stale_checks (line ~591); a specialized ACK function
    avoids a future change to one breaking the other (RESEARCH.md pitfall 6).
    """
    cfg = getattr(agent, "dispatch_config", None) or {}
    if isinstance(cfg, dict) and "ack_timeout_minutes" in cfg:
        return int(cfg["ack_timeout_minutes"])
    runtime_type = getattr(agent, "agent_runtime", None)
    if runtime_type and runtime_type in AGENT_RUNTIME_ACK_TIMEOUTS:
        return AGENT_RUNTIME_ACK_TIMEOUTS[runtime_type]
    return _DEFAULT_ACK_TIMEOUT_MINUTES


# ── Lifecycle Safety Watchdog (ADR-046) — silent-abort auto-block ─────────
#
# An agent acks a task (in_progress, ack_at set) then goes SILENT without ever
# sending a terminal PATCH (review/blocked/failed). The task hangs in_progress
# forever. This is the missing terminal rung of the stale-check ladder: after
# tiered recovery ran and the agent STILL didn't report, block the task (never
# fail it) so it surfaces to Mark via the normal blocked/Approval flow.
#
# PRIME DIRECTIVE: a genuinely-working agent (long tool call / long LLM turn)
# must NEVER be blocked. See the guards in _check_stuck_in_progress and the
# runtime-aware, floored threshold below.
MIN_STUCK_BLOCK_FLOOR = 20        # HARD floor — NO path (incl. dispatch_config override) blocks below this
STUCK_BLOCK_MINUTES = 25          # default for reliable-turn-state cli-bridge (anthropic TUI)
STUCK_BLOCK_MINUTES_SLOW = 45     # default for slow/local runtimes with documented
                                  # detect_turn_state false-negatives during long reasoning
                                  # (vllm/lmstudio/cloud/unsloth/openai_compatible — the Sparky 12-min cook)

# Runtime-types whose tmux working-marker detection false-negatives during long
# local reasoning → need a materially higher stale floor before we trust the
# frozen last_task_activity_at as a "dead turn".
_SLOW_RUNTIME_TYPES = {"vllm_docker", "lmstudio", "openai_compatible", "unsloth", "cloud"}


def _stuck_block_default_for(agent, runtime=None) -> int:
    """Runtime-aware default stuck-block threshold (minutes).

    Slow/local models false-negative in detect_turn_state during long reasoning
    (freezing last_task_activity_at while perfectly healthy), so they get a
    materially higher floor than the claude TUI whose working-markers render
    reliably. Always kept at least role-idle + 10 so tiered recovery ran first.
    """
    rt_type = (
        getattr(runtime, "runtime_type", None)
        or getattr(agent, "runtime_type", None)
        or ""
    ).lower()
    slow = rt_type in _SLOW_RUNTIME_TYPES
    base = STUCK_BLOCK_MINUTES_SLOW if slow else STUCK_BLOCK_MINUTES
    return max(_idle_threshold_for(agent) + 10, base)


def _stuck_block_threshold_for(agent, runtime=None) -> int:
    """3-step resolve: dispatch_config override (FLOORED) → runtime-aware default → hard fallback.

    PRIME DIRECTIVE: the per-agent override is CLAMPED. A mis-set
    stuck_block_minutes=5 must NOT be able to block a healthy 6-min reasoning
    cook. The effective threshold can never drop below
    max(role_idle_threshold, MIN_STUCK_BLOCK_FLOOR) — code-enforced, not a doc
    note — so _check_stale_in_progress recovery has always run and failed before
    a block is even possible.
    """
    floor = max(_idle_threshold_for(agent), MIN_STUCK_BLOCK_FLOOR)
    cfg = getattr(agent, "dispatch_config", None) or {}
    if isinstance(cfg, dict) and "stuck_block_minutes" in cfg:
        return max(int(cfg["stuck_block_minutes"]), floor)  # override is FLOORED
    return _stuck_block_default_for(agent, runtime)


def _liveness_floor_seconds(agent) -> float:
    """Wrapper-alive window = 2× heartbeat interval, min 120s.

    Derived from agent.heartbeat_config['interval'] (e.g. '5m' / '30s'). If the
    interval can't be parsed, fall back to the 120s floor. A last_seen_at fresher
    than this proves the poll.sh wrapper (and thus the container/host) is alive —
    which is exactly what separates a silent LLM abort (block) from full process
    death (orphan → inbox, handled elsewhere).
    """
    cfg = getattr(agent, "heartbeat_config", None) or {}
    raw = str(cfg.get("interval", "5m")).strip().lower() if isinstance(cfg, dict) else "5m"
    seconds = 300.0
    try:
        if raw.endswith("ms"):
            seconds = float(raw[:-2]) / 1000.0
        elif raw.endswith("m"):
            seconds = float(raw[:-1]) * 60.0
        elif raw.endswith("h"):
            seconds = float(raw[:-1]) * 3600.0
        elif raw.endswith("s"):
            seconds = float(raw[:-1])
        else:
            seconds = float(raw)
    except (ValueError, TypeError):
        seconds = 300.0
    return max(2.0 * seconds, 120.0)


class TaskRunnerService:
    def __init__(self, interval: int = 60):
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Task Runner started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Task Runner stopped")

    async def _run_loop(self) -> None:
        # Grace period: wait until everything is ready
        await asyncio.sleep(15)
        while self._running:
            try:
                if await self._acquire_lock():
                    await self._check_tasks()
                else:
                    logger.debug("Task Runner skipped — another worker holds the lock")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Task Runner check error: %s", e)
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """Redis lock so only one worker runs the checks per cycle."""
        try:
            redis = await get_redis()
            acquired = await redis.set(
                RedisKeys.task_runner_lock(), "1", nx=True, ex=self._interval
            )
            return bool(acquired)
        except Exception:
            return True

    async def _check_tasks(self) -> None:
        from app.config import settings
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await self._check_dispatch_ack(session, skip_pending=settings.use_subagent_dispatch)
            await self._check_stale_in_progress(session)
            # ADR-046: runs AFTER stale-recovery so tiered recovery (restart+resume)
            # is always attempted first; only tasks that survived recovery and
            # stayed silent get blocked.
            await self._check_stuck_in_progress(session)

    # ── Dispatch ACK Pruefung ──────────────────────────────────────

    async def _check_dispatch_ack(self, session: AsyncSession, skip_pending: bool = False) -> None:
        """Checks whether dispatched tasks were confirmed (ACK'd) by the agent.

        Two checks:
        1. ACK timeout: dispatched_at set but no ack_at → agent did not confirm
        2. Dispatch pending: assigned but dispatched_at = null → dispatch never arrived
           (skipped in subagent-dispatch mode)

        Escalation: create an approval so the operator can decide.
        """
        result = await session.exec(
            select(Task).where(
                Task.status == "inbox",
                Task.assigned_agent_id.isnot(None),  # type: ignore[arg-type]
            )
        )
        tasks = result.all()
        now = utcnow()

        for task in tasks:
            if not task.assigned_agent_id:
                continue
            # Skip tasks with active run_control (stopped/manual_hold)
            if task.run_control in ("stopped", "manual_hold"):
                continue

            agent = await session.get(Agent, task.assigned_agent_id)
            if not agent:
                continue

            # Host-based FreeCode-Bridge agents: own stale check.
            # Do NOT apply to Docker cli-bridge agents — they have their
            # own turn-state check in poll.sh (lib/turn-state.sh).
            if getattr(agent, "agent_runtime", None) == "free-code-bridge":
                if task.dispatched_at:
                    await self._handle_cli_bridge_stale_dispatch(session, task, agent)
                continue

            # Phase 30: gateway_agent_id gate dropped. Stale-Check applies to
            # all agents with a poll-based runtime — the ACK timeout itself
            # determines whether the agent picked up the task, no per-agent
            # session presence check needed.

            # Skip non-worker agents (e.g. Planner)
            if Scope.TASKS_WRITE not in get_agent_effective_scopes(agent):
                continue

            redis = await get_redis()

            if task.dispatched_at:
                # ── Check 1: ACK timeout ──
                await self._handle_ack_timeout(session, task, agent, now, redis)
            elif not skip_pending:
                # ── Check 2: dispatch pending ──
                # This check is skipped in subagent-dispatch mode
                await self._handle_dispatch_pending(session, task, agent, now, redis)

    async def _handle_ack_timeout(
        self, session: AsyncSession, task: Task, agent: Agent, now: datetime, redis
    ) -> None:
        """Task was dispatched (dispatched_at set) but the agent hasn't ACK'd.

        Two-stage handling (D-1 self-heal):
        1. After `ack_timeout/2` min without ACK → silent retry: rotate
           dispatch_attempt_id. poll.sh sees a different attempt_id → re-pastes
           automatically. Prevents Sparky-style 2.7h hangs when the original
           paste got lost.
        2. After `ack_timeout` min without ACK → escalation approval to the operator.
        """
        dispatched = ensure_aware(task.dispatched_at)
        minutes_since_dispatch = (now - dispatched).total_seconds() / 60

        ack_timeout = _get_ack_timeout_minutes(agent)

        if minutes_since_dispatch < ack_timeout:
            # D-1 silent retry window: rotate attempt_id if >= ack_timeout/2
            await self._maybe_rotate_dispatch_attempt(
                session, task, agent, minutes_since_dispatch, redis, ack_timeout
            )
            return

        # Full timeout → escalation approval
        # Dedup — 24h cooldown so multiple approvals aren't created
        ack_check_key = RedisKeys.dispatch_ack_check(str(task.id))
        if await redis.get(ack_check_key):
            return

        # Create an approval instead of auto-reassigning
        await self._create_dispatch_approval(
            session, task, agent, minutes_since_dispatch, "kein ACK nach Dispatch"
        )
        await redis.set(ack_check_key, "1", ex=86400)  # 24h Cooldown

        logger.warning(
            "ACK timeout: '%s' — %s hat nicht bestaetigt (%dmin), Approval erstellt",
            task.title, agent.name, int(minutes_since_dispatch),
        )

    async def _maybe_rotate_dispatch_attempt(
        self,
        session: AsyncSession,
        task: Task,
        agent: Agent,
        minutes_since_dispatch: float,
        redis,
        ack_timeout: float,
    ) -> bool:
        """D-1 self-heal: rotate dispatch_attempt_id at ack_timeout/2 without an ACK.

        poll.sh dedupes via the `LAST_DISPATCHED_ATTEMPT_ID` shell variable. If
        the backend rotates `dispatch_attempt_id`, poll.sh sees a different
        attempt_id than the one it last pasted on its next tick → triggers a
        fresh paste path without human intervention.

        Dedup via Redis: only 1 rotation per `(task_id, original_attempt_id)`.
        TTL = full ack_timeout so no endless rotation happens.

        Returns: True if rotated, False if skipped (still too early or already rotated).
        """
        rotation_threshold = ack_timeout / 2.0
        if minutes_since_dispatch < rotation_threshold:
            return False  # Still too early

        rotated_key = f"mc:task:{task.id}:attempt_rotated"
        if await redis.get(rotated_key):
            return False  # Already rotated within this dispatch window

        import uuid as _uuid
        old_attempt_id = task.dispatch_attempt_id
        new_attempt_id = str(_uuid.uuid4())
        from app.services.dispatch_attempt_audit import set_dispatch_attempt_id
        await set_dispatch_attempt_id(
            session, task, new_attempt_id,
            caller="d1_silent_retry",
            reason=f"no_ack_after_{int(minutes_since_dispatch)}min",
        )
        await redis.set(rotated_key, "1", ex=int(ack_timeout * 60))

        await emit_event(
            session,
            "task.dispatch_attempt_rotated",
            f"Silent retry: dispatch_attempt_id rotiert für '{task.title[:60]}' nach {int(minutes_since_dispatch)}min ohne ACK",
            severity="warning",
            agent_id=agent.id,
            board_id=task.board_id,
            task_id=task.id,
            detail={
                "old_attempt_id": old_attempt_id,
                "new_attempt_id": new_attempt_id,
                "minutes_since_dispatch": int(minutes_since_dispatch),
                "rotation_threshold_min": int(rotation_threshold),
            },
        )
        logger.warning(
            "D-1 silent retry: '%s' — %s hat nicht ACK'd nach %dmin (threshold=%dmin), neue attempt_id %s",
            task.title[:60], agent.name, int(minutes_since_dispatch),
            int(rotation_threshold), new_attempt_id[:8],
        )
        return True

    async def _handle_cli_bridge_stale_dispatch(
        self, session: AsyncSession, task: Task, agent: Agent
    ) -> None:
        """CLI-bridge task: check whether the task is in the worker queue (pending/running).

        Uses /queue/status/{agent}/{task_id} instead of a tmux session search,
        because the worker loop doesn't create its own session per task.
        If pending/running → all good, no reset.
        If unknown/failed → reset dispatched_at for a fresh dispatch.
        """
        import urllib.request
        import json as _json
        from app.config import settings

        agent_slug = agent.name.lower().replace(" ", "-")
        try:
            url = f"{settings.free_code_bridge_url}/queue/status/{agent_slug}/{task.id}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = _json.loads(resp.read().decode())
            queue_status = data.get("status", "unknown")
            if queue_status in ("pending", "running"):
                return  # Task is active in queue, all good
        except Exception:
            return  # Bridge unreachable → do nothing, next cycle will retry

        # Task not in queue → reset dispatched_at for a fresh dispatch
        task.dispatched_at = None
        session.add(task)
        await session.commit()
        logger.info(
            "CLI bridge stale dispatch reset: '%s' — Queue-Status '%s', Task wird neu dispatched",
            task.title, queue_status,
        )

    async def _handle_cli_bridge_inprogress_recovery(
        self, session: AsyncSession, task: Task, agent: Agent
    ) -> None:
        """CLI-bridge task is in_progress — check queue status and start recovery if needed.

        If queue status is pending/running → active, do nothing.
        If queue status is unknown/failed → the worker lost the task (crash/restart).
        Recovery: re-enqueue the task with a short recap prompt.
        Status stays in_progress — the agent continues, not starting over.

        Only kicks in after CLI_BRIDGE_RECOVERY_MINUTES (buffer for normal operation).
        Deduplication via a Redis key (30-minute cooldown after recovery).
        """
        import urllib.request
        import urllib.parse
        import json as _json
        from app.config import settings

        CLI_BRIDGE_RECOVERY_MINUTES = 20

        # Last comment or started_at as the time reference
        now = utcnow()
        last_activity = ensure_aware(task.started_at or task.updated_at)

        comments_result = await session.exec(
            select(TaskComment)
            .where(TaskComment.task_id == task.id)
            .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
            .limit(3)
        )
        last_comments = list(comments_result.all())
        if last_comments:
            newest_comment_time = ensure_aware(last_comments[0].created_at)
            if newest_comment_time > last_activity:
                last_activity = newest_comment_time

        minutes_since_activity = (now - last_activity).total_seconds() / 60
        if minutes_since_activity < CLI_BRIDGE_RECOVERY_MINUTES:
            return  # Too early — normal operation, no intervention needed yet

        # Query queue status
        agent_slug = agent.name.lower().replace(" ", "-")
        try:
            url = f"{settings.free_code_bridge_url}/queue/status/{agent_slug}/{task.id}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = _json.loads(resp.read().decode())
            queue_status = data.get("status", "unknown")
        except Exception:
            return  # Bridge unreachable → no intervention

        if queue_status in ("pending", "running"):
            return  # Task is actively running in the queue

        # Recovery needed: queue_status is unknown or failed
        redis = await get_redis()
        recovery_key = f"mc:task_runner:cli_bridge_recovery:{task.id}"
        if await redis.get(recovery_key):
            return  # Recovery already started, cooldown active

        # Build recap prompt from recent comments
        recap_lines = [
            f"# Recovery: {task.title}",
            "",
            f"Du warst bereits an diesem Task dran (ID: {task.id}).",
            "Der Worker wurde neu gestartet. Bitte mache dort weiter wo du aufgehoert hast.",
            "",
        ]
        if task.description:
            recap_lines += ["## Aufgabe", task.description[:500], ""]
        if last_comments:
            recap_lines.append("## Letzter bekannter Stand (neueste Kommentare)")
            for c in reversed(last_comments):
                recap_lines.append(f"- [{c.comment_type}] {c.content[:200]}")
            recap_lines.append("")
        recap_lines += [
            "## Naechste Schritte",
            "1. Pruefe was bereits erledigt ist (Dateien, Git-Status)",
            "2. Mache die verbleibende Arbeit fertig",
            "3. PATCH status: review + Resolution-Kommentar wenn fertig",
        ]
        recap_prompt = "\n".join(recap_lines)

        # Re-enqueue the task into the bridge queue
        workspace = task.workspace_path or "/tmp"
        try:
            payload = _json.dumps({
                "agent_name": agent_slug,
                "task_id": str(task.id),
                "workspace": workspace,
                "prompt": recap_prompt,
            }).encode()
            req = urllib.request.Request(
                f"{settings.free_code_bridge_url}/start",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as e:
            logger.warning("CLI-Bridge Recovery: Konnte Task nicht neu einreihen: %s", e)
            return

        # Set a cooldown (30 min) to prevent duplicate recovery
        await redis.set(recovery_key, "1", ex=1800)

        await emit_event(
            session,
            "task.recovery",
            f"CLI-Bridge Recovery: '{task.title}' bei {agent.name} — "
            f"Queue-Status war '{queue_status}', Task neu eingereiht (Agent macht weiter)",
            board_id=task.board_id,
            task_id=task.id,
            agent_id=agent.id,
            detail={
                "agent_name": agent.name,
                "queue_status": queue_status,
                "minutes_since_activity": round(minutes_since_activity, 1),
            },
        )
        logger.info(
            "CLI-Bridge Recovery: Task '%s' war in_progress, Queue '%s' → neu eingereiht (agent: %s)",
            task.title[:60], queue_status, agent.name,
        )

    async def _handle_dispatch_pending(
        self, session: AsyncSession, task: Task, agent: Agent, now: datetime, redis
    ) -> None:
        """Task is assigned but dispatched_at = null — message was never successfully sent."""
        task_updated = ensure_aware(task.updated_at)
        minutes_since_assigned = (now - task_updated).total_seconds() / 60

        if minutes_since_assigned < DISPATCH_PENDING_WARN_MINUTES:
            return  # Still fresh, watchdog pending_dispatch will deliver it

        pending_key = RedisKeys.dispatch_pending_warn(str(task.id))
        if await redis.get(pending_key):
            return

        if minutes_since_assigned >= DISPATCH_PENDING_TIMEOUT_MINUTES:
            # Pending too long → create an approval instead of auto-reassigning
            await self._create_dispatch_approval(
                session, task, agent, minutes_since_assigned, "Dispatch nie zugestellt"
            )
            await redis.set(pending_key, "1", ex=86400)  # 24h cooldown
        else:
            # Log a warning (info severity — no escalation yet)
            await emit_event(
                session,
                "task.dispatch_pending",
                f"'{task.title}' — Dispatch an {agent.name} seit {int(minutes_since_assigned)}min ausstehend",
                board_id=task.board_id,
                task_id=task.id,
                agent_id=agent.id,
            )
            await redis.set(pending_key, "1", ex=300)  # 5min cooldown

    async def _create_dispatch_approval(
        self, session: AsyncSession, task: Task, agent: Agent,
        minutes_waiting: float, reason: str,
    ) -> None:
        """Create an approval instead of auto-reassigning — the operator decides.

        D-2 fix (2026-05-14): direct Telegram push to the operator with inline
        buttons. Previously only an 'approval.created' activity event with
        severity=warning was emitted — it lands in the UI inbox badge, but if
        the operator isn't actively in the UI, they don't see the escalation.
        Sparky's frontend audit task created an escalation at 09:43, the
        operator only noticed it locally at 12:00 (= 2h 17min reaction time).
        Telegram is the operator's push channel with high action-required value.
        """
        approval = Approval(
            board_id=task.board_id,
            task_id=task.id,
            agent_id=agent.id,
            action_type="dispatch_escalation",
            description=(
                f"'{task.title}' — {agent.name} hat seit {int(minutes_waiting)} Min. "
                f"nicht reagiert ({reason}). Bitte Task manuell zuweisen oder re-dispatchen."
            ),
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)

        # D-2: direct Telegram push (action-required channel)
        try:
            from app.services.telegram_bot import telegram_bot
            await telegram_bot.send_approval_telegram(
                approval.id,
                agent.name,
                task.title,
                f"Dispatch-Eskalation nach {int(minutes_waiting)}min ohne ACK ({reason}). "
                f"Manuell entscheiden: re-dispatchen, anderem Agent zuweisen oder canceln.",
            )
        except Exception as e:
            logger.warning(
                "D-2 Telegram-Push fuer dispatch_escalation approval %s failed: %s",
                approval.id, e,
            )

        await emit_event(
            session,
            "approval.created",
            f"Dispatch-Eskalation: '{task.title}' — {agent.name} reagiert nicht",
            severity="warning",
            board_id=task.board_id,
            task_id=task.id,
            agent_id=agent.id,
        )

    # ── Tiered Recovery (Phase 6 REC-01/02/03) ───────────────────────

    async def _run_tiered_recovery(
        self,
        session: AsyncSession,
        task: Task,
        agent: Agent,
    ) -> bool:
        """REC-01 (Phase 6) — Tiered automated recovery for stale tasks.

        Returns True if any tier succeeded (recovery handled the stall);
        False only if ALL 4 tiers failed (Tier 4 emits operator notification).

        Task status STAYS in_progress throughout all tiers (REC D-15 — never
        flips to blocked or aborted). Recovery progress is observable only via
        Activity Events (REC-03 audit log).

        Tier flow:
          1. Heartbeat probe — SKIPPED post-Phase-29 (gateway sunset, D-21).
             The gateway-based heartbeat RPC is gone; cli-bridge/host agents
             have no equivalent probe. Recovery jumps straight to Tier 2.
          2. Process restart per runtime (docker/host); cli-bridge/openclaw skip
          3. Task resume with Structured Recovery Recap via runtime_context
          4. Notify operator via emit_event(severity='error') -> auto-Discord
        """
        redis = await get_redis()

        # Dedup: 600s TTL covers Tier 1 (10s) + Tier 2 (30s wait) + Tier 3 (5min)
        recovery_key = RedisKeys.recovery_inprogress(str(agent.id), str(task.id))
        if await redis.get(recovery_key):
            logger.info(
                "Recovery already in progress for %s on '%s' — skip",
                agent.name, task.title[:40],
            )
            return True  # treat as handled (in flight); avoid duplicate work
        await redis.set(recovery_key, "1", ex=600)

        # Emit Tier-1-start event (REC-03)
        await emit_event(
            session,
            "agent.recovery_started",
            f"{agent.name}: Auto-Recovery gestartet (Stale > 60min)",
            severity="warning",
            agent_id=agent.id,
            board_id=task.board_id,
            task_id=task.id,
            detail={"tier": 1, "reason": "stale_60min", "agent_name": agent.name},
        )

        # ── Tier 1: Heartbeat probe — SKIPPED post-Phase-29 ────────
        # The gateway is gone (OpenClaw sunset). There is no equivalent
        # cross-runtime "is the agent alive?" probe for cli-bridge/host/
        # claude-code agents. Recovery jumps straight to Tier 2 (restart).
        await emit_event(
            session,
            "agent.recovery_tier_complete",
            f"{agent.name}: Tier 1 uebersprungen — Heartbeat (Gateway-Sunset)",
            severity="info",
            agent_id=agent.id, board_id=task.board_id, task_id=task.id,
            detail={
                "tier": 1,
                "tier_name": "heartbeat",
                "result": "skipped",
                "reason": "gateway_removed_phase29",
            },
        )

        # ── Tier 2: Process restart per runtime ──────────────────────
        runtime = getattr(agent, "agent_runtime", "openclaw")
        tier2_ok = False
        if runtime == "docker":
            try:
                from app.services.docker_agent_sync import restart_docker_agent_container
                # Sync function — wrap in to_thread to keep watchdog loop happy
                result = await asyncio.to_thread(restart_docker_agent_container, agent)
                tier2_ok = result.get("status", "").startswith("restarted")
            except Exception as e:
                logger.warning("Tier 2 (docker restart) failed for %s: %s", agent.name, e)
        elif runtime == "host":
            try:
                from app.routers.cli_terminal import _host_agent_lifecycle
                await _host_agent_lifecycle(agent, "restart")
                tier2_ok = True
            except Exception as e:
                logger.warning("Tier 2 (host restart) failed for %s: %s", agent.name, e)
        else:
            # cli-bridge, openclaw, free-code-bridge — no restart mechanism (D-21)
            logger.debug(
                "Tier 2 skipped for %s (runtime=%s, no restart available)",
                agent.name, runtime,
            )

        await emit_event(
            session,
            "agent.recovery_tier_complete",
            f"{agent.name}: Tier 2 {'ok' if tier2_ok else ('fehlgeschlagen' if runtime in ('docker', 'host') else 'uebersprungen')} — Restart ({runtime})",
            severity="info" if tier2_ok else ("warning" if runtime in ("docker", "host") else "info"),
            agent_id=agent.id, board_id=task.board_id, task_id=task.id,
            detail={
                "tier": 2,
                "tier_name": "restart",
                "runtime": runtime,
                "result": "ok" if tier2_ok else ("failed" if runtime in ("docker", "host") else "skipped"),
            },
        )

        # 30s wait between Tier 2 (restart) and Tier 3 (resume) — let the
        # container come up before sending the recap (D-17). Skip wait if
        # Tier 2 was skipped (no restart happened).
        if tier2_ok:
            await asyncio.sleep(30)

        # ── Tier 3: Task resume via auto_dispatch_task ─────────────
        # Post Phase 29 / Gateway-Sunset: the legacy RPC chat-send path is gone.
        # We re-dispatch via the unified runtime-aware path (auto_dispatch_task).
        # The dispatcher rebuilds the message from task fields + recovery context;
        # the per-runtime delivery branch (cli-bridge / host / claude-code) hands
        # it off to the poll-loop. We also capture the structured recovery recap
        # as a TaskComment so it's durable + visible in the task timeline.
        tier3_ok = False
        try:
            from app.services.task_context_builder import build_recovery_context
            from app.redis_client import try_claim_recovery_comment_cooldown

            recap_extras = await build_recovery_context(session, task)
            if recap_extras:
                # G6: shared cooldown across all "continue"-comment mechanisms
                # (Tier-3 recap, unblock_notify, watchdog nudge, bootstrap
                # recap) — first one to fire wins, others skip silently.
                if await try_claim_recovery_comment_cooldown(redis, str(task.id)):
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=recap_extras,
                        comment_type="recovery_recap",
                    ))
                else:
                    logger.debug(
                        "Tier 3 recovery_recap skipped for task %s — "
                        "recovery-comment cooldown already claimed",
                        task.id,
                    )

            # Reset dispatch flags so the dispatcher treats this as a fresh attempt
            task.dispatched_at = None
            task.ack_at = None
            session.add(task)
            await session.commit()

            # Re-dispatch — runtime-aware (cli-bridge / host / claude-code).
            # Awaited directly (we're already in an async context) instead of
            # fire-and-forget via asyncio.create_task: auto_dispatch_task
            # catches its own exceptions internally and always returns None,
            # so its return value carries no success signal. The only
            # reliable indicator is whether the delivery branch actually set
            # dispatched_at again — re-fetch the task (auto_dispatch_task
            # commits via its own session) and check that.
            await auto_dispatch_task(task.id, task.board_id)
            await session.refresh(task)
            tier3_ok = task.dispatched_at is not None
        except Exception as e:
            logger.warning("Tier 3 (resume) failed for %s: %s", agent.name, e)

        await emit_event(
            session,
            "agent.recovery_tier_complete",
            f"{agent.name}: Tier 3 {'ok' if tier3_ok else 'fehlgeschlagen'} — Resume",
            severity="info" if tier3_ok else "warning",
            agent_id=agent.id, board_id=task.board_id, task_id=task.id,
            detail={"tier": 3, "tier_name": "resume", "result": "ok" if tier3_ok else "failed"},
        )
        if tier3_ok:
            return True

        # ── Tier 4: Notify operator (auto-Discord via severity=error) ────
        await emit_event(
            session,
            "agent.recovery_failed",
            f"{agent.name}: Auto-Recovery fehlgeschlagen — Operator benachrichtigt",
            severity="error",  # auto-triggers Discord webhook (activity.py:73-80)
            agent_id=agent.id,
            board_id=task.board_id,
            task_id=task.id,
            detail={
                "tiers_attempted": 3,
                "agent_name": agent.name,
                "task_id": str(task.id),
                "task_title": task.title,
                "runtime": runtime,
            },
        )
        return False

    # ── In-Progress Tasks ohne Fortschritt ───────────────────────────

    async def _check_stale_in_progress(self, session: AsyncSession) -> None:
        """Tasks in status 'in_progress' that haven't had a comment in a while.

        Protection mechanisms:
        1. Skip Board Leads — they orchestrate, they don't implement
        2. Skip parent tasks with active subtasks — waiting is normal
        3. Circuit breaker — after MAX_STALE_CHECKS stop nagging the agent,
           log an escalation event instead
        """
        result = await session.exec(
            select(Task).where(
                Task.status == "in_progress",
                Task.assigned_agent_id.isnot(None),  # type: ignore[arg-type]
            )
        )
        tasks = result.all()
        now = utcnow()

        for task in tasks:
            if not task.assigned_agent_id:
                continue
            # Skip tasks with active run_control (stopped/manual_hold)
            if task.run_control in ("stopped", "manual_hold"):
                continue

            agent = await session.get(Agent, task.assigned_agent_id)
            if not agent:
                continue

            # ── Protection 1: skip Board Leads ──
            if agent.is_board_lead:
                continue

            # Skip non-worker agents (e.g. Planner)
            if Scope.TASKS_WRITE not in get_agent_effective_scopes(agent):
                continue

            # ── Protection 2: skip parent tasks with subtasks ──
            subtask_result = await session.exec(
                select(Task).where(Task.parent_task_id == task.id).limit(1)
            )
            if subtask_result.first():
                continue

            # Host FreeCode-Bridge: queue status via free_code_bridge_url.
            # Docker cli-bridge agents fall through to the normal stale check
            # with the role-based idle threshold (see _idle_threshold_for).
            # Their turn-state detection additionally happens in poll.sh.
            if getattr(agent, "agent_runtime", "openclaw") == "free-code-bridge":
                await self._handle_cli_bridge_inprogress_recovery(session, task, agent)
                continue

            # Use last comment or started_at as the reference
            last_activity = ensure_aware(task.started_at or task.updated_at)

            # Check the last comment
            comments_result = await session.exec(
                select(TaskComment)
                .where(TaskComment.task_id == task.id)
                .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
                .limit(1)
            )
            last_comment = comments_result.first()
            if last_comment:
                comment_time = ensure_aware(last_comment.created_at)
                if comment_time > last_activity:
                    last_activity = comment_time

            # ── Resolution detection: last comment is "resolution" ──
            # Agent reported done but never set status to review.
            # Auto-promote as a safety net (second line of defense after fix 1).
            # Phase 8 BUG-01: agent.auto_promote_on_resolution=False suppresses
            # this Path B auto-promote (mirror of the Path A guard in
            # agent_comments.py:292). agent already loaded at line 725.
            # Default True preserves single-step worker safety-net.
            #
            # Bug 17 (2026-05-13): comment_type="resolution" is polysemous —
            # also written by approvals.py with author_type="user" for
            # clarification-resolve and blocker-resolve answers. Restrict
            # auto-promote to agent-authored resolutions only.
            if (
                last_comment
                and last_comment.comment_type == "resolution"
                and last_comment.author_type == "agent"
                and task.status == "in_progress"
                and agent.auto_promote_on_resolution
            ):
                task.status = "review"
                task.updated_at = utcnow()
                session.add(task)
                await session.commit()
                await emit_event(
                    session, "task.status_changed",
                    f"Stale-Check Auto-Promote: {agent.name} hatte resolution-Kommentar → review",
                    board_id=task.board_id, task_id=task.id, agent_id=agent.id,
                    detail={"old_status": "in_progress", "new_status": "review", "auto_promoted": True, "source": "stale_check"},
                )
                logger.info(
                    "Stale-Check Auto-Promote: Task '%s' hatte resolution-Kommentar von %s → review",
                    task.title[:60], agent.name,
                )
                continue

            minutes_since_activity = (now - last_activity).total_seconds() / 60

            stale_minutes = _idle_threshold_for(agent)
            if minutes_since_activity < stale_minutes:
                continue

            # Deduplication
            redis = await get_redis()
            stale_key = RedisKeys.task_runner_stale(str(task.id))
            already_flagged = await redis.get(stale_key)
            if already_flagged:
                continue

            # ── Protection 3: circuit breaker ──
            counter_key = RedisKeys.task_runner_stale_count(str(task.id))
            check_count = int(await redis.get(counter_key) or 0)

            max_checks = _get_agent_timeout(agent, "max_stale_checks", MAX_STALE_CHECKS)
            if check_count >= max_checks:
                escalated_key = RedisKeys.task_runner_stale_escalated(str(task.id))
                already_escalated = await redis.get(escalated_key)
                if already_escalated:
                    continue

                await emit_event(
                    session,
                    "task.stuck",
                    f"'{task.title}' bei {agent.name}: seit {int(minutes_since_activity)}min stuck "
                    f"({check_count}x Status-Check ohne Fortschritt). Manuelle Pruefung noetig.",
                    severity="error",
                    board_id=task.board_id,
                    task_id=task.id,
                    agent_id=agent.id,
                    detail={
                        "agent_name": agent.name,
                        "task_title": task.title,
                        "minutes_since_activity": round(minutes_since_activity, 1),
                        "check_count": check_count,
                    },
                )
                await redis.set(escalated_key, "1", ex=86400)
                logger.warning(
                    "Task '%s' stuck — %d checks without progress, escalated (agent: %s)",
                    task.title, check_count, agent.name,
                )
                continue

            # REC-01 (Phase 6): tiered automated recovery before falling
            # through to the legacy "remind agent" reminder. If recovery
            # succeeds at any tier, skip the reminder + stale event entirely
            # (the recovery itself is the visible action). If all 4 tiers
            # fail, the recovery_failed Discord notification has already
            # been sent — also skip the reminder to avoid noise.
            await self._run_tiered_recovery(session, task, agent)
            # Bump counter + cooldown so the same task doesn't re-trigger
            # recovery on the next watchdog tick within 30 minutes
            await redis.set(stale_key, "1", ex=1800)
            await redis.incr(counter_key)
            await redis.expire(counter_key, 86400)

    # ── Lifecycle Safety Watchdog: silent-abort auto-block (ADR-046) ──────

    async def _check_stuck_in_progress(self, session: AsyncSession) -> None:
        """Auto-block a task that was acked then went SILENT (no terminal PATCH).

        This is the missing terminal rung of the stale-check ladder. It runs
        AFTER _check_stale_in_progress (tiered recovery already tried). It fires
        ONLY on cli-bridge agents (the sole runtime that refreshes
        last_task_activity_at DURING work via poll.sh's Bug-13 working-heartbeat)
        and only when a conservative, runtime-aware, floored threshold is passed,
        corroborated by no agent TaskComment, and persisted across ≥2 ticks
        (tick 1 nudges, tick 2+ blocks).

        PRIME DIRECTIVE: a genuinely-working agent (long tool call / long LLM
        turn) must NEVER be blocked. Guard 0 (runtime gate) + guard on the
        liveness delta (wrapper alive AND turn dead) + corroboration + staged
        nudge are the layered protections. When any guard fails we skip — a
        surfaced warning always beats a wrongful block. See ADR-046.
        """
        from app.config import settings
        if not settings.lifecycle_watchdog_enabled:
            return

        from app.models.runtime import Runtime
        from app.services.task_lifecycle import apply_terminal_unassign, record_task_event

        result = await session.exec(
            select(Task).where(
                Task.status == "in_progress",
                Task.assigned_agent_id.isnot(None),  # type: ignore[arg-type]
                Task.ack_at.isnot(None),  # type: ignore[arg-type]
                Task.blocked_by_task_id.is_(None),  # type: ignore[union-attr]
            )
        )
        tasks = result.all()
        now = utcnow()
        redis = await get_redis()

        for task in tasks:
            # ── Leaf / ownership guards (mirror _check_stale_in_progress) ──
            if not task.assigned_agent_id or task.ack_at is None:
                continue
            if task.run_control in ("stopped", "manual_hold"):
                continue
            if task.review_decision == "hold":
                continue

            agent = await session.get(Agent, task.assigned_agent_id)
            if not agent:
                continue

            # ── Guard 0: RUNTIME GATE (the false-positive firewall) ──
            # Only cli-bridge refreshes last_task_activity_at during work. host /
            # manual / claude-code freeze it at ack → blocking them = prime-
            # directive violation. Hard-skip (see ADR-046 "Runtime scope").
            if getattr(agent, "agent_runtime", None) != "cli-bridge":
                continue

            # Board Leads orchestrate & wait — never their own worker turn.
            if agent.is_board_lead:
                continue
            # Non-worker agents (Planner etc.) — no TASKS_WRITE, skip.
            if Scope.TASKS_WRITE not in get_agent_effective_scopes(agent):
                continue
            # Parents with children legitimately wait on subtasks.
            subtask_result = await session.exec(
                select(Task).where(Task.parent_task_id == task.id).limit(1)
            )
            if subtask_result.first():
                continue
            # Operator holds.
            if getattr(agent, "operational_mode", "active") == "paused":
                continue
            # DELIBERATELY NO run_state skip (incident 2026-07-02, omp zombie):
            # run_state='running' is a dispatch latch, not proof of an active
            # turn — on a silent abort nobody resets it, and the skip disarmed
            # the watchdog in exactly its target scenario. Genuine work is
            # protected by the liveness threshold: heartbeating agents refresh
            # last_task_activity_at and never reach the threshold; a silent
            # 'running' latch for 25min+ is the zombie.

            # ── Idempotency (guards 15-17): skip if already handled ──
            block_key = RedisKeys.task_runner_stuck_block(str(task.id))
            if await redis.get(block_key):
                continue
            # DB fallback (restart-safe): pending blocker/stuck Approval exists.
            pending_appr = await session.exec(
                select(Approval).where(
                    Approval.task_id == task.id,
                    Approval.status == "pending",
                    Approval.action_type.in_(["blocker_decision", "agent_stuck"]),  # type: ignore[attr-defined]
                )
            )
            if pending_appr.first():
                continue

            # ── Liveness delta (the crux) ──
            # WRAPPER ALIVE: last_seen_at fresh (poll.sh loop still alive). If it
            # is ALSO stale → process/container dead → orphan path owns it, NOT us.
            last_seen = agent.last_seen_at
            if last_seen is None:
                continue
            seen_age = (now - ensure_aware(last_seen)).total_seconds()
            if seen_age >= _liveness_floor_seconds(agent):
                continue  # wrapper dead → do NOT block (orphan → inbox recovery)

            # DEAD TURN: last_task_activity_at stale beyond the runtime-aware,
            # floored threshold. COALESCE onto last_seen_at only for legacy NULL.
            activity_ref = agent.last_task_activity_at or agent.last_seen_at
            if activity_ref is None:
                continue
            mins_silent = (now - ensure_aware(activity_ref)).total_seconds() / 60.0

            runtime = None
            if getattr(agent, "runtime_id", None):
                runtime = await session.get(Runtime, agent.runtime_id)
            threshold = _stuck_block_threshold_for(agent, runtime)
            if mins_silent < threshold:
                continue

            # ── Corroboration: no agent-authored TaskComment within the window ──
            # A healthy long tool call posts no comment, but if the agent DID post
            # progress inside the stale window it is not silent → skip. Also skip
            # if the last comment is a poll.sh auto-blocker (already handled).
            comment_result = await session.exec(
                select(TaskComment)
                .where(TaskComment.task_id == task.id)
                .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
                .limit(1)
            )
            last_comment = comment_result.first()
            if last_comment:
                # Guard 16: poll.sh already flipped a blocker on this task.
                if (
                    last_comment.author_type == "agent"
                    and last_comment.comment_type == "blocker"
                ):
                    continue
                # Guard 14: fresh agent-authored progress → not silent.
                if last_comment.author_type == "agent":
                    comment_age = (now - ensure_aware(last_comment.created_at)).total_seconds() / 60.0
                    if comment_age < threshold:
                        continue

            # ── Staged escalation: tick 1 nudges, tick 2+ blocks ──
            count_key = RedisKeys.task_runner_stuck_block_count(str(task.id))
            tick_count = int(await redis.get(count_key) or 0)
            if tick_count < 1:
                # First eligible tick: nudge, do NOT block. Gives the agent (and
                # poll.sh) a chance to self-report; catches transient blips.
                # G6: shared cooldown gates the *comment* only — tick-count
                # persistence still advances even if another mechanism already
                # posted a "continue" comment on this task, so tick 2+
                # (block) still fires on schedule.
                from app.redis_client import try_claim_recovery_comment_cooldown
                if await try_claim_recovery_comment_cooldown(redis, str(task.id)):
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        comment_type="watchdog_notify",
                        content=(
                            f"LIFECYCLE-WATCHDOG: Du hast \"{task.title}\" geackt, aber seit "
                            f"{int(mins_silent)}min keinen Fortschritt gemeldet. Bitte JETZT den "
                            f"Status setzen (PATCH status: review / blocked / failed) oder einen "
                            f"Progress-Kommentar posten — sonst wird die Task zur Klärung an den Operator "
                            f"eskaliert.\nTask-ID: {task.id}"
                        ),
                    ))
                    await session.commit()
                else:
                    logger.debug(
                        "Lifecycle-Watchdog nudge comment skipped for task %s — "
                        "recovery-comment cooldown already claimed",
                        task.id,
                    )
                await redis.set(count_key, "1", ex=3600)  # ≥2-tick persistence
                logger.info(
                    "Lifecycle-Watchdog nudge: '%s' (%s) silent %dmin — tick 1, no block",
                    task.title[:60], agent.name, int(mins_silent),
                )
                continue

            # ── Tick 2+: BLOCK. Human-wait (blocked_by_task_id IS NULL). ──
            # Canonical path: apply_terminal_unassign keeps assigned_agent_id
            # (resumable) but releases agent.current_task_id + sets run_state so
            # the agent doesn't look busy forever and the poll cancel-loop can't
            # fire. Helper does NOT set status → set it explicitly.
            await apply_terminal_unassign(session, task, "blocked")
            task.status = "blocked"
            task.updated_at = utcnow()
            session.add(task)
            # Ensure human-wait agent state even if run_state was 'idle' going in
            # (the typical silent-abort case — apply_terminal_unassign only flips
            # run_state from running/None).
            if agent.current_task_id == task.id:
                agent.current_task_id = None
            agent.run_state = "blocked"
            session.add(agent)

            await record_task_event(
                session, task.id, "in_progress", "blocked",
                changed_by="watchdog", agent_id=agent.id,
                reason="stuck_no_terminal_patch",
            )

            approval = Approval(
                board_id=task.board_id,
                task_id=task.id,
                agent_id=agent.id,
                action_type="blocker_decision",
                description=(
                    f"Silent-Abort: {agent.name} hat '{task.title[:60]}' geackt, dann "
                    f"{int(mins_silent)}min ohne Rueckmeldung verstummt. "
                    f"Re-dispatchen, anderem Agent zuweisen oder abbrechen?"
                ),
                status="pending",
                expires_at=utcnow() + timedelta(hours=24),
                payload={
                    "blocker_type": "technical_problem",  # same value poll.sh uses
                    "blocker_question": (
                        f"{agent.name} verstummt seit {int(mins_silent)}min nach ACK. "
                        f"Re-dispatchen oder abbrechen?"
                    )[:150],
                    "source": "lifecycle_watchdog",
                    "reason": "stuck_no_terminal_patch",
                },
            )
            session.add(approval)
            await session.commit()
            await session.refresh(approval)

            # Telegram push (Mark's action-required channel).
            try:
                from app.services.telegram_bot import telegram_bot
                await telegram_bot.send_approval_telegram(
                    approval.id, agent.name, task.title,
                    f"Silent-Abort: {agent.name} verstummt seit {int(mins_silent)}min "
                    f"auf '{task.title[:50]}'. Re-dispatchen oder abbrechen?",
                )
            except Exception as e:
                logger.warning(
                    "Lifecycle-Watchdog Telegram-Push fuer approval %s failed: %s",
                    approval.id, e,
                )

            await emit_event(
                session,
                "task.status_changed",
                f"Lifecycle-Watchdog: {agent.name} verstummt nach ACK → blocked "
                f"({int(mins_silent)}min still)",
                severity="warning",
                board_id=task.board_id,
                task_id=task.id,
                agent_id=agent.id,
                detail={
                    "old_status": "in_progress",
                    "new_status": "blocked",
                    "reason": "stuck_no_terminal_patch",
                    "minutes_silent": round(mins_silent, 1),
                    "source": "lifecycle_watchdog",
                },
            )
            await redis.set(block_key, "1", ex=86400)  # 24h dedup
            logger.warning(
                "Lifecycle-Watchdog BLOCK: '%s' (%s) silent %dmin after ACK → blocked",
                task.title[:60], agent.name, int(mins_silent),
            )


# Singleton-Instanz
task_runner = TaskRunnerService()
