"""Report Auto-Draft fuer fehlgeschlagene Tasks.

Wenn ein Root-Task mit `report_back_required=true` auf `failed` geht ohne
dass der Agent selbst einen `mc telegram`-Report gesendet hat, rendern wir
aus dem Task-Kontext einen Kurz-Report und senden ihn an den Reports-Chat.

Warum Auto-Draft nur bei `failed` (nicht bei `done`):
- `done` = Agent hat ein Ergebnis → der Operator erwartet expliziten Report, nicht
  Auto-Generiert. Dort greift das Hard-Gate in `agent_scoped.py`.
- `failed` = Agent endet oft abrupt (Crash, Exception, timeouts). Wenn wir
  auch dort blockieren, riskieren wir Task-Zombies. Besser: System macht
  Best-Effort-Draft, Agent kann nachliefern wenn er wieder up ist.

Der Draft ist bewusst knapp (keine Emojis, klare Struktur) — der Agent
kann dann einen vollwertigen Report nachreichen wenn noetig.
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
    """HTML-escape Text fuer Telegram parse_mode=HTML."""
    if not text:
        return ""
    return html.escape(text, quote=False)


def _truncate(text: str, max_chars: int) -> str:
    """Text auf max_chars kuerzen, mit … am Ende wenn abgeschnitten."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


async def _load_reflection_and_last_comments(
    session: AsyncSession,
    task: Task,
) -> tuple[str | None, list[str]]:
    """Holt die letzte Reflection (falls vorhanden) + die letzten bis zu 3 Agent-Kommentare."""
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
    # Aelteste zuerst fuer Lesereihenfolge
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
    """Rendert einen knappen Failure-Report als HTML (Telegram parse_mode)."""
    parts: list[str] = []

    # Header
    title_clean = _truncate(_escape(task_title), 120)
    parts.append(f"{agent_emoji} <b>{_escape(agent_name)}</b> · {title_clean} ❌")
    parts.append("")
    parts.append("<i>Auto-Report: Task failed. Manueller Follow-up empfohlen.</i>")
    parts.append("")
    parts.append("─────────────────")

    # Reflection (erste ~300 Zeichen)
    if reflection:
        parts.append("")
        parts.append("📝 <b>Reflexion des Agenten</b>")
        parts.append(_escape(_truncate(reflection.strip(), 500)))

    # Letzte Kommentare
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
    """Rendert + sendet einen Auto-Draft-Report fuer fehlgeschlagene Tasks.

    Returns True bei erfolgreichem Send, False wenn Reports-Bot nicht konfiguriert
    oder Send fehlgeschlagen. Wirft keine Exceptions — Caller muss das
    status=failed-Transition NICHT blockieren.
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

    # Telegram limit 4096 chars — wir halten 4000 als Safety-Margin
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
