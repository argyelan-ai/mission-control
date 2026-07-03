"""Session Monitor Mixin — heartbeat-based offline detection (Phase 29 post-Gateway).

Before: Gateway session checks, recovery, token sync, context compaction, health probing.
These methods were all gateway-specific and were removed with the Gateway sunset:
- `_check_agent_sessions` — based on sessions_list
- `_check_session_recovery` — chat-send recovery, replaced by task_runner._check_dispatch_ack (Phase 26)
- `_sync_agent_tokens` — token counts from Gateway sessions
- `_reset_overflowed_sessions` + `_compact_overflowed_sessions` — Gateway context-limit mgmt
- `_check_session_health` + `_escalate_to_lead` — RPC health probing

After: heartbeat-based offline detection (DB-only) + recovery-recap helper.
"""

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskComment
from app.redis_client import get_redis
from app.services.activity import emit_event
from app.utils import ensure_aware, utcnow

logger = logging.getLogger("mc.watchdog")


def _parse_interval(interval_str: str) -> int:
    """Parse an interval string like '5m', '30s', '1m' into seconds."""
    try:
        if interval_str.endswith("m"):
            return int(interval_str[:-1]) * 60
        elif interval_str.endswith("s"):
            return int(interval_str[:-1])
        return int(interval_str)
    except (ValueError, IndexError):
        return 300  # Default 5min


class SessionMonitorMixin:
    """Heartbeat-based offline detection (post Gateway sunset).

    All gateway-specific code (sessions_list, sessions_reset, chat_send) was
    removed in Phase 29. ACK-timeout owner is task_runner._check_dispatch_ack (Phase 26).
    """

    async def _check_heartbeat_health(self, session: AsyncSession) -> None:
        """Alert when an openclaw-runtime agent hasn't been seen for longer than
        2x its heartbeat interval.

        The `agent_runtime == "openclaw"` filter stays in place because cli-bridge + host
        have their own heartbeat lifecycle via /agent/me/heartbeat and should
        bypass this check. Post Phase 30 (DB cleanup), the CHECK
        constraint on agent_runtime will forbid the 'openclaw' value — the filter
        will then match 0 rows guaranteed and the check becomes effectively a no-op.
        """
        redis = await get_redis()
        # Phase 30: gateway_agent_id filter dropped (was leftover from Phase 29
        # session-monitor migration off sessions_list()).
        result = await session.exec(
            select(Agent).where(
                Agent.agent_runtime == "openclaw",  # type: ignore[arg-type]
                Agent.is_board_lead == False,  # noqa: E712
                Agent.status.in_(["online", "idle"]),  # type: ignore[union-attr]
            )
        )
        agents = result.all()
        now = utcnow()

        for agent in agents:
            if not agent.last_seen_at:
                continue

            interval_str = (agent.heartbeat_config or {}).get("interval", "5m")
            interval_s = _parse_interval(interval_str)
            last_seen = ensure_aware(agent.last_seen_at)
            since_seen = (now - last_seen).total_seconds()

            if since_seen > interval_s * 2:
                dedup_key = f"mc:watchdog:hb_overdue:{agent.id}"
                already_alerted = await redis.get(dedup_key)
                if already_alerted:
                    continue
                await redis.set(dedup_key, "1", ex=600)

                await emit_event(
                    session, "agent.heartbeat_overdue",
                    f"{agent.emoji or '🤖'} {agent.name}: Heartbeat ueberfaellig ({int(since_seen)}s seit letztem Kontakt)",
                    severity="warning",
                    agent_id=agent.id,
                    board_id=agent.board_id,
                    detail={
                        "agent_name": agent.name,
                        "expected_interval_s": interval_s,
                        "since_seen_s": int(since_seen),
                    },
                )

    async def _build_recovery_recap(
        self, task: Task, agent: Agent, session: AsyncSession,
    ) -> str:
        """Structured Recovery Recap — short and focused.

        Helper stays in place even though the calling methods were removed
        in Phase 29. Tests in test_resilient_recovery.py still cover the
        recap builder; the Phase-31 refactor can reuse it for cli-bridge
        recovery.
        """
        parts = [
            "# Session Recovery",
            "",
            f"Dein aktiver Task: **{task.title}**",
            f"Task-ID: `{task.id}`",
            f"Board-ID: `{task.board_id}`",
        ]

        workspace = None
        if task.project_id:
            try:
                from app.models.board import Project
                project = await session.get(Project, task.project_id)
                if project:
                    if project.workspace_path:
                        workspace = project.workspace_path
                    elif project.github_repo_url and agent.workspace_path:
                        from app.services.git_service import slugify_project
                        workspace = f"{agent.workspace_path}/{slugify_project(project.name)}"
            except Exception:
                pass
        if not workspace and agent.workspace_path:
            workspace = agent.workspace_path
        if workspace:
            parts.append(f"Workspace: `{workspace}`")

        try:
            cp_result = await session.exec(
                select(TaskComment)
                .where(
                    TaskComment.task_id == task.id,
                    TaskComment.comment_type.in_(["checkpoint", "progress", "resolution"]),  # type: ignore[union-attr]
                )
                .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
                .limit(1)
            )
            cp = cp_result.first()
            if cp and cp.content:
                parts.append(f"\nLetzter Fortschritt:\n> {cp.content[:300]}")
        except Exception:
            pass

        parts.append(
            "\nDeine Session wurde neugestartet. "
            "Pruefe deinen Workspace (Dateien, Git-Log) und mach weiter wo du aufgehoert hast. "
            "Hole dir deine Task-Details ueber die API wenn du mehr Kontext brauchst."
        )

        return "\n".join(parts)
