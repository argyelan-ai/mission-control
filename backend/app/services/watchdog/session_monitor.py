"""Session Monitor Mixin — Heartbeat-basierte Offline-Erkennung (Phase 29 post-Gateway).

Vorher: Gateway-Session-Checks, Recovery, Token-Sync, Context-Compaction, Health-Probing.
Diese Methoden waren alle gateway-spezifisch und sind mit dem Gateway-Sunset entfallen:
- `_check_agent_sessions` — sessions_list-basiert
- `_check_session_recovery` — chat-send Recovery, ersetzt durch task_runner._check_dispatch_ack (Phase 26)
- `_sync_agent_tokens` — Token-Counts aus Gateway-Sessions
- `_reset_overflowed_sessions` + `_compact_overflowed_sessions` — Gateway-Context-Limit-Mgmt
- `_check_session_health` + `_escalate_to_lead` — RPC-Health-Probing

Nachher: heartbeat-basierte Offline-Erkennung (DB-only) + recovery-recap-Helper.
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
    """Parse Interval-String wie '5m', '30s', '1m' zu Sekunden."""
    try:
        if interval_str.endswith("m"):
            return int(interval_str[:-1]) * 60
        elif interval_str.endswith("s"):
            return int(interval_str[:-1])
        return int(interval_str)
    except (ValueError, IndexError):
        return 300  # Default 5min


class SessionMonitorMixin:
    """Heartbeat-basierte Offline-Erkennung (post Gateway sunset).

    Aller Gateway-spezifische Code (sessions_list, sessions_reset, chat_send) ist mit
    Phase 29 entfernt. ACK-Timeout-Owner ist task_runner._check_dispatch_ack (Phase 26).
    """

    async def _check_heartbeat_health(self, session: AsyncSession) -> None:
        """Alert wenn ein openclaw-runtime Agent laenger als 2x sein Heartbeat-Interval
        nicht gesehen wurde.

        Filter `agent_runtime == "openclaw"` bleibt stehen weil cli-bridge + host
        ihren eigenen Heartbeat-Lebenszyklus via /agent/me/heartbeat haben und
        diesen Check umgehen sollen. Post Phase 30 (DB-Cleanup) wird der CHECK-
        constraint auf agent_runtime den 'openclaw' Wert verbieten — der Filter
        matched dann garantiert 0 Rows und der Check ist effektiv no-op.
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
        """Structured Recovery Recap — kurz und fokussiert.

        Helper bleibt erhalten obwohl die aufrufenden Methoden mit Phase 29
        weg sind. Tests in test_resilient_recovery.py decken den Recap-Builder
        weiterhin ab; Phase-31-Refaktor kann ihn fuer cli-bridge-Recovery
        wiederverwenden.
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
