"""MeetingService — Structured agent discussions.

Flow:
1. start_meeting() → background task starts the meeting
2. Per agenda topic: agents are questioned sequentially
   (agent B sees what agent A said → real discussion)
3. Board Lead summarizes at the end
4. Results → BoardMemory + Telegram to the operator

Phase 29 (Gateway Sunset, D-10): The synchronous "ask agent, wait for reply"
path used to run over the Gateway chat RPC with wait-for-reply. Without the
Gateway this synchronous loop is no longer possible. Until Phase 31 delivers
a cli-bridge-based replacement, agents respond in the meeting with a
placeholder and the meeting still runs to completion (BoardMemory +
auto-summary). Telegram notification goes via telegram_bot.send_message
directly to the Bot API.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine as async_engine
from app.models.agent import Agent
from app.models.meeting import AgentMeeting, AgentMeetingMessage
from app.models.memory import BoardMemory
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.discord import send_to_discord_channel
from app.services.sse import broadcast
from app.services.telegram_bot import telegram_bot
from app.utils import create_tracked_task, utcnow

logger = logging.getLogger("mc.meetings")

# ── Configuration ──────────────────────────────────────────────────────

AGENT_RESPONSE_TIMEOUT = 180.0  # 3 minutes per agent
SUMMARY_TIMEOUT = 300.0  # 5 minutes for summary
MEETING_MAX_DURATION = timedelta(hours=2)
MEETING_LOCK_TTL = 7200  # 2h in seconds


# ── Public API ────────────────────────────────────────────────────────


async def start_meeting(
    session: AsyncSession,
    board_id: uuid.UUID,
    title: str,
    agenda: list[str],
    meeting_type: str = "ad_hoc",
    participant_ids: list[uuid.UUID] | None = None,
    scheduled_at: datetime | None = None,
) -> AgentMeeting:
    """Create meeting and start it as a background task.

    Returns the meeting object immediately — execution runs async.
    """
    # Check lock — only one meeting per board at a time
    locked = await _acquire_meeting_lock(str(board_id))
    if not locked:
        raise MeetingAlreadyRunningError(
            f"Es laeuft bereits ein Meeting auf Board {board_id}"
        )

    # Load participants (or take all board agents)
    if participant_ids:
        pid_strs = [str(pid) for pid in participant_ids]
    else:
        pid_strs = None  # resolved in the runner

    meeting = AgentMeeting(
        board_id=board_id,
        title=title,
        meeting_type=meeting_type,
        status="scheduled",
        agenda=agenda,
        participant_ids=pid_strs,
        scheduled_at=scheduled_at or utcnow(),
    )
    session.add(meeting)
    await session.commit()
    await session.refresh(meeting)

    await emit_event(
        session,
        event_type="meeting.scheduled",
        title=f"Meeting geplant: {title}",
        board_id=board_id,
        detail={"meeting_id": str(meeting.id), "agenda": agenda},
    )

    # Start meeting as a background task
    create_tracked_task(
        _run_meeting(meeting.id, board_id),
        name=f"meeting:{meeting.id}",
    )

    return meeting


async def cancel_meeting(
    session: AsyncSession,
    meeting_id: uuid.UUID,
) -> AgentMeeting:
    """Cancel a running or scheduled meeting."""
    meeting = await session.get(AgentMeeting, meeting_id)
    if not meeting:
        raise MeetingNotFoundError(f"Meeting {meeting_id} nicht gefunden")

    if meeting.status in ("completed", "cancelled"):
        raise MeetingStateError(f"Meeting ist bereits {meeting.status}")

    meeting.status = "cancelled"
    meeting.completed_at = utcnow()
    session.add(meeting)
    await session.commit()

    await _release_meeting_lock(str(meeting.board_id))

    await emit_event(
        session,
        event_type="meeting.cancelled",
        title=f"Meeting abgebrochen: {meeting.title}",
        board_id=meeting.board_id,
        detail={"meeting_id": str(meeting.id)},
    )

    return meeting


# ── Meeting Runner (Background) ───────────────────────────────────────


async def _run_meeting(meeting_id: uuid.UUID, board_id: uuid.UUID) -> None:
    """Main loop: runs the meeting, topic by topic."""
    from sqlmodel.ext.asyncio.session import AsyncSession as AS

    async with AS(async_engine) as session:
        meeting = await session.get(AgentMeeting, meeting_id)
        if not meeting or meeting.status == "cancelled":
            await _release_meeting_lock(str(board_id))
            return

        meeting.status = "running"
        meeting.started_at = utcnow()
        session.add(meeting)
        await session.commit()

        deadline = utcnow() + MEETING_MAX_DURATION

        await _broadcast_meeting_event(meeting, "meeting.started", {
            "meeting_id": str(meeting.id),
            "title": meeting.title,
        })

        try:
            # Load participants
            participants = await _load_participants(session, meeting)
            if not participants:
                logger.warning("Meeting %s: keine Teilnehmer gefunden", meeting_id)
                meeting.status = "failed"
                meeting.completed_at = utcnow()
                session.add(meeting)
                await session.commit()
                await _release_meeting_lock(str(board_id))
                return

            lead = await _find_board_lead(session, board_id)
            all_responses: list[dict[str, Any]] = []

            # Per agenda topic
            for topic_idx, topic in enumerate(meeting.agenda or []):
                if utcnow() > deadline:
                    logger.warning("Meeting %s: Zeitlimit erreicht", meeting_id)
                    break

                # Save facilitator question as message
                await _save_message(
                    session, meeting.id,
                    role="facilitator_question",
                    content=topic,
                    round=1,
                    topic_index=topic_idx,
                )

                await _broadcast_meeting_event(meeting, "meeting.topic_started", {
                    "meeting_id": str(meeting.id),
                    "topic_index": topic_idx,
                    "topic": topic,
                })

                # Question agents sequentially
                topic_responses: list[dict[str, str]] = []
                for agent in participants:
                    if utcnow() > deadline:
                        break

                    response = await _ask_agent(
                        session, meeting, agent,
                        topic, topic_idx,
                        previous_responses=topic_responses,
                    )
                    topic_responses.append({
                        "agent_name": agent.name,
                        "agent_id": str(agent.id),
                        "response": response,
                    })

                all_responses.append({
                    "topic": topic,
                    "topic_index": topic_idx,
                    "responses": topic_responses,
                })

            # Summary by Board Lead
            summary = await _generate_summary(
                session, meeting, lead, all_responses
            )

            # Save results
            meeting.summary = summary.get("summary", "")
            meeting.decisions = summary.get("decisions", [])
            meeting.action_items = summary.get("action_items", [])
            meeting.status = "completed"
            meeting.completed_at = utcnow()

            # Save as BoardMemory
            memory = BoardMemory(
                board_id=board_id,
                title=f"Meeting: {meeting.title}",
                content=meeting.summary or "",
                memory_type="meeting_summary",
                auto_generated=True,
            )
            session.add(memory)
            await session.commit()
            await session.refresh(memory)

            meeting.memory_id = memory.id
            session.add(meeting)
            await session.commit()

            await _broadcast_meeting_event(meeting, "meeting.completed", {
                "meeting_id": str(meeting.id),
                "summary": meeting.summary[:500] if meeting.summary else "",
                "decisions_count": len(meeting.decisions or []),
            })

            # Telegram to the operator — direct Bot API (no Gateway dependency)
            decisions_text = ""
            if meeting.decisions:
                decisions_text = "\n".join(
                    f"- {d.get('text', d)}" for d in meeting.decisions[:5]
                )
            try:
                await telegram_bot.send_message(
                    f"<b>Meeting abgeschlossen</b>\n\n"
                    f"{meeting.title}\n"
                    f"{len(meeting.decisions or [])} Entscheidungen\n"
                    f"{decisions_text}"
                )
            except Exception as e:
                logger.warning("Meeting Telegram notify failed: %s", e)

            # Optional: per-board lead Discord channel notify
            if lead and getattr(lead, "discord_channel_id", None):
                try:
                    await send_to_discord_channel(
                        lead.discord_channel_id,
                        embed={
                            "title": f"Meeting abgeschlossen: {meeting.title}",
                            "description": (meeting.summary or "")[:1800],
                            "color": 0x7C3AED,
                        },
                    )
                except Exception as e:
                    logger.warning(
                        "Discord meeting notify failed for %s: %s",
                        getattr(lead, "name", "?"),
                        e,
                    )

        except Exception as e:
            logger.exception("Meeting %s fehlgeschlagen: %s", meeting_id, e)
            meeting.status = "failed"
            meeting.completed_at = utcnow()
            session.add(meeting)
            await session.commit()

            await _broadcast_meeting_event(meeting, "meeting.failed", {
                "meeting_id": str(meeting.id),
                "error": str(e)[:200],
            })

        finally:
            await _release_meeting_lock(str(board_id))


# ── Agent Questioning ────────────────────────────────────────────────


async def _ask_agent(
    session: AsyncSession,
    meeting: AgentMeeting,
    agent: Agent,
    topic: str,
    topic_index: int,
    previous_responses: list[dict[str, str]],
) -> str:
    """Question a single agent on the current topic.

    Builds context from previous answers → real discussion.
    """
    # Formulate question with context
    question = _build_agent_question(
        meeting.title, topic, agent.name, previous_responses
    )

    await _broadcast_meeting_event(meeting, "meeting.agent_thinking", {
        "meeting_id": str(meeting.id),
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "topic_index": topic_index,
    })

    # Send RPC to agent
    response_text = await _send_and_wait(agent, question)

    # Save response
    await _save_message(
        session, meeting.id,
        agent_id=agent.id,
        agent_name=agent.name,
        role="agent_response",
        content=response_text,
        round=1,
        topic_index=topic_index,
    )

    await _broadcast_meeting_event(meeting, "meeting.message_received", {
        "meeting_id": str(meeting.id),
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "topic_index": topic_index,
        "content": response_text[:500],
    })

    return response_text


def _build_agent_question(
    meeting_title: str,
    topic: str,
    agent_name: str,
    previous_responses: list[dict[str, str]],
) -> str:
    """Build the question for an agent with context of prior answers."""
    parts = [
        f"# Meeting: {meeting_title}",
        f"\n## Aktuelles Thema\n{topic}",
    ]

    if previous_responses:
        parts.append("\n## Bisherige Antworten der anderen Agents")
        for resp in previous_responses:
            parts.append(f"\n**{resp['agent_name']}:** {resp['response']}")

    parts.append(
        f"\n---\n{agent_name}, was ist deine Einschaetzung zu diesem Thema? "
        "Beziehe dich auf die vorherigen Antworten wenn relevant. "
        "Antworte kurz und praegnant (max 3-4 Saetze)."
    )

    return "\n".join(parts)


async def _send_and_wait(agent: Agent, message: str) -> str:
    """Placeholder after Gateway sunset (Phase 29, D-10).

    Before Phase 29 a synchronous chat RPC with wait-for-reply ran here.
    Without the Gateway there is no synchronous reply path anymore;
    cli-bridge agents work async (task → TaskComment → poll). Until Phase 31
    delivers an async meeting runner, agents respond with a marked
    placeholder and the meeting still runs to completion with auto-summary.
    """
    logger.info(
        "Meeting question for %s skipped — Gateway sunset, awaiting Phase 31 cli-bridge meeting runner",
        agent.name,
    )
    return (
        f"[{agent.name}: Meeting-Frage wurde im Gateway-Sunset nicht synchron beantwortet — "
        "Antwort wird im naechsten Meeting-Iteration ueber den cli-bridge Pfad zugestellt.]"
    )


# ── Summary ───────────────────────────────────────────────────────────


async def _generate_summary(
    session: AsyncSession,
    meeting: AgentMeeting,
    lead: Agent | None,
    all_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Meeting summary (post-Gateway-sunset).

    Before Phase 29 the Board Lead generated a summary synchronously via
    Gateway chat. Without the Gateway there is no synchronous LLM path
    anymore — we fall back immediately to the deterministic auto-summary.
    Phase 31 will deliver a cli-bridge-based async summary runner.
    """
    if lead is not None:
        logger.info(
            "Meeting summary: skipping lead-LLM path (Gateway sunset) for %s",
            lead.name,
        )
    return _auto_summary(meeting, all_responses)


def _auto_summary(
    meeting: AgentMeeting,
    all_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fallback summary when no lead is available."""
    parts = [f"## Meeting: {meeting.title}\n"]
    for topic_data in all_responses:
        parts.append(f"### {topic_data['topic']}")
        for resp in topic_data.get("responses", []):
            parts.append(f"- **{resp['agent_name']}:** {resp['response'][:200]}")
        parts.append("")

    return {
        "summary": "\n".join(parts),
        "decisions": [],
        "action_items": [],
    }


# ── Helper Functions ─────────────────────────────────────────────────


async def _load_participants(
    session: AsyncSession,
    meeting: AgentMeeting,
) -> list[Agent]:
    """Load meeting participants — from participant_ids or all board agents.

    Phase 29 (D-10): no more gateway_agent_id filter — cli-bridge / host
    agents don't have a gateway_agent_id.
    """
    if meeting.participant_ids:
        pids = [uuid.UUID(pid) for pid in meeting.participant_ids]
        stmt = select(Agent).where(Agent.id.in_(pids))
    else:
        stmt = select(Agent).where(Agent.board_id == meeting.board_id)
    result = await session.exec(stmt)
    return list(result.all())


async def _find_board_lead(
    session: AsyncSession,
    board_id: uuid.UUID,
) -> Agent | None:
    """Find Board Lead (role='lead' or is_board_lead=True)."""
    stmt = (
        select(Agent)
        .where(Agent.board_id == board_id)
        .where(Agent.role == "lead")
    )
    result = await session.exec(stmt)
    lead = result.first()
    if lead:
        return lead

    # Fallback: is_board_lead
    stmt2 = (
        select(Agent)
        .where(Agent.board_id == board_id)
        .where(Agent.is_board_lead.is_(True))
    )
    result2 = await session.exec(stmt2)
    return result2.first()


async def _save_message(
    session: AsyncSession,
    meeting_id: uuid.UUID,
    role: str,
    content: str,
    agent_id: uuid.UUID | None = None,
    agent_name: str | None = None,
    round: int = 1,
    topic_index: int = 0,
) -> AgentMeetingMessage:
    """Save meeting message in DB."""
    msg = AgentMeetingMessage(
        meeting_id=meeting_id,
        agent_id=agent_id,
        agent_name=agent_name,
        role=role,
        content=content,
        round=round,
        topic_index=topic_index,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


async def _broadcast_meeting_event(
    meeting: AgentMeeting,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Broadcast SSE event for meeting updates."""
    try:
        payload = {
            **data,
            "board_id": str(meeting.board_id),
            "meeting_type": meeting.meeting_type,
        }
        await broadcast(RedisKeys.meeting_events(), event_type, payload)
        if meeting.board_id:
            await broadcast(
                RedisKeys.board_events(str(meeting.board_id)),
                event_type,
                payload,
            )
    except Exception as e:
        logger.debug("Meeting broadcast failed: %s", e)


# ── Redis Lock ────────────────────────────────────────────────────────


async def _acquire_meeting_lock(board_id: str) -> bool:
    """Only one meeting per board at a time. Fail-open."""
    try:
        redis = await get_redis()
        result = await redis.set(
            RedisKeys.meeting_lock(board_id),
            "1",
            nx=True,
            ex=MEETING_LOCK_TTL,
        )
        return result is not None and result is not False
    except Exception:
        return True  # Fail-open


async def _release_meeting_lock(board_id: str) -> None:
    """Release meeting lock."""
    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.meeting_lock(board_id))
    except Exception:
        pass


# ── Exceptions ────────────────────────────────────────────────────────


class MeetingError(Exception):
    pass


class MeetingAlreadyRunningError(MeetingError):
    pass


class MeetingNotFoundError(MeetingError):
    pass


class MeetingStateError(MeetingError):
    pass
