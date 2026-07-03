"""Report auto-draft for failed tasks.

When a root task with `report_back_required=true` goes to `failed` without
the agent itself having sent an `mc telegram` report, we render a short
report from the task context and send it to the reports chat.

Why auto-draft only on `failed` (not on `done`):
- `done` = agent produced a result → the operator expects an explicit report,
  not an auto-generated one. The hard gate in `agent_scoped.py` applies there.
- `failed` = the agent often ends abruptly (crash, exception, timeout). If we
  also blocked there, we'd risk task zombies. Better: the system makes a
  best-effort draft, and the agent can follow up once it's back up.

The draft is deliberately terse (no emojis, clear structure) — the agent
can then send a full report afterwards if needed.
"""

from __future__ import annotations

import html
import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskComment

logger = logging.getLogger("mc.report_auto_draft")


def _escape(text: str | None) -> str:
    """HTML-escape text for Telegram parse_mode=HTML."""
    if not text:
        return ""
    return html.escape(text, quote=False)


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, with … at the end when truncated."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


async def _load_reflection_and_last_comments(
    session: AsyncSession,
    task: Task,
) -> tuple[str | None, list[str]]:
    """Fetches the latest reflection (if any) + the last up to 3 agent comments."""
    reflection_q = (
        select(TaskComment)
        .where(TaskComment.task_id == task.id, TaskComment.comment_type == "reflection")
        .order_by(TaskComment.created_at.desc())
        .limit(1)
    )
    reflection = (await session.exec(reflection_q)).first()
    reflection_text = reflection.content if reflection else None

    recent_q = (
        select(TaskComment)
        .where(
            TaskComment.task_id == task.id,
            TaskComment.comment_type.in_(["progress", "blocker", "feedback"]),  # type: ignore[attr-defined]
        )
        .order_by(TaskComment.created_at.desc())
        .limit(3)
    )
    recent = (await session.exec(recent_q)).all()
    # Oldest first for reading order
    recent_texts = [c.content for c in reversed(recent)]
    return reflection_text, recent_texts


def _render_draft(
    *,
    agent_name: str,
    agent_emoji: str,
    task_title: str,
    task_id: str,
    reflection: str | None,
    recent_comments: list[str],
) -> str:
    """Renders a terse failure report as HTML (Telegram parse_mode)."""
    parts: list[str] = []

    # Header
    title_clean = _truncate(_escape(task_title), 120)
    parts.append(f"{agent_emoji} <b>{_escape(agent_name)}</b> · {title_clean} ❌")
    parts.append("")
    parts.append("<i>Auto-Report: Task failed. Manueller Follow-up empfohlen.</i>")
    parts.append("")
    parts.append("─────────────────")

    # Reflection (first ~300 characters)
    if reflection:
        parts.append("")
        parts.append("📝 <b>Reflexion des Agenten</b>")
        parts.append(_escape(_truncate(reflection.strip(), 500)))

    # Recent comments
    if recent_comments:
        parts.append("")
        parts.append("💬 <b>Letzte Kommentare</b>")
        for c in recent_comments:
            snippet = _escape(_truncate(c.strip(), 250))
            parts.append(f"• {snippet}")

    # Footer
    parts.append("")
    parts.append("─────────────────")
    parts.append(f"📄 Task <code>{task_id[:8]}</code>")

    return "\n".join(parts)


async def render_and_send_failure_draft(
    session: AsyncSession,
    task: Task,
    agent: Agent,
) -> bool:
    """Renders + sends an auto-draft report for failed tasks.

    Returns True on successful send, False if the reports bot is not configured
    or the send failed. Never raises exceptions — the caller must NOT block the
    status=failed transition on this.
    """
    from app.services.telegram_reports import telegram_reports

    if not telegram_reports.configured:
        logger.info(
            "Reports-Bot nicht konfiguriert — Auto-Draft fuer Task %s uebersprungen",
            task.id,
        )
        return False

    reflection, recent_comments = await _load_reflection_and_last_comments(session, task)

    agent_emoji = getattr(agent, "emoji", None) or "🤖"
    text = _render_draft(
        agent_name=agent.name,
        agent_emoji=agent_emoji,
        task_title=task.title,
        task_id=str(task.id),
        reflection=reflection,
        recent_comments=recent_comments,
    )

    # Telegram limit 4096 chars — we keep 4000 as a safety margin
    if len(text) > 4000:
        text = _truncate(text, 4000)

    result = await telegram_reports.send(text)
    if result is None or not result.get("ok"):
        logger.warning(
            "Auto-Draft send fehlgeschlagen fuer Task %s: %s",
            task.id,
            (result or {}).get("description", "unconfigured/network"),
        )
        return False
    return True
