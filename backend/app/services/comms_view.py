"""The operator's read-only view of session conversations (ADR-100 §4, 2d).

The phone's inbox tab "Between agents" lists every session-to-session
conversation and opens one. Everything here only reads the rows the send
path wrote (`comms_conversations` + `threads`/`messages`, §6 rule 3). It
never computes "alive" or "listening" — the directory sheet asks
`comms_directory.directory` for that (§6 rule 2).

The brake state of a conversation, from the operator's side:
- ``running``  — messages may flow;
- ``paused``   — six messages without the operator, the "let it continue?"
                 card waits (or went missing);
- ``stopped``  — the operator said no on that card.
An approved card counts as running: the send path lifts the pause on the
next message.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.comms import CommsConversation, CommsSession
from app.models.thread import Message, Thread
from app.services import comms_directory
from app.services.comms_send import address_for_message, unframe
from app.utils import ensure_aware


async def participant(session: AsyncSession, ref: str) -> dict[str, Any]:
    """A principal ref ("agent:<uuid>" / "session:<uuid>") as the phone shows
    it. Ended sessions and archived agents keep their name in old threads."""
    kind, _, raw_id = ref.partition(":")
    try:
        pid = uuid.UUID(raw_id)
    except ValueError:
        return {"ref": ref, "kind": kind, "address": ref, "label": ref, "harness": None}
    if kind == "agent":
        agent = await session.get(Agent, pid)
        if agent is not None:
            return {
                "ref": ref, "kind": "agent", "address": comms_directory.agent_address(agent),
                "label": agent.name, "harness": agent.harness or agent.agent_runtime,
            }
    elif kind == "session":
        row = await session.get(CommsSession, pid)
        if row is not None:
            return {
                "ref": ref, "kind": "session", "address": row.address,
                "label": row.folder, "harness": row.harness,
            }
    return {"ref": ref, "kind": kind, "address": raw_id[:8], "label": raw_id[:8], "harness": None}


async def brake_state(session: AsyncSession, conv: CommsConversation) -> str:
    if not conv.paused:
        return "running"
    card = await session.get(Approval, conv.brake_approval_id) if conv.brake_approval_id else None
    if card is not None and card.status == "approved":
        return "running"
    if card is not None and card.status not in ("pending",):
        return "stopped"
    return "paused"


def _iso(dt) -> str | None:
    return ensure_aware(dt).isoformat() if dt else None


async def _message_out(session: AsyncSession, m: Message) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "seq": m.seq,
        "from": await address_for_message(session, m),
        "text": unframe(m.body or ""),
        "created_at": _iso(m.created_at),
    }


async def _conversation_out(session: AsyncSession, conv: CommsConversation) -> dict[str, Any]:
    count = (
        await session.exec(select(func.count()).select_from(Message).where(Message.thread_id == conv.thread_id))
    ).one()
    last = (
        await session.exec(
            select(Message)
            .where(Message.thread_id == conv.thread_id)
            .order_by(Message.seq.desc())  # type: ignore[union-attr]
            .limit(1)
        )
    ).first()
    return {
        "thread_id": str(conv.thread_id),
        "participants": [await participant(session, conv.a_ref), await participant(session, conv.b_ref)],
        "message_count": int(count or 0),
        "last_message": await _message_out(session, last) if last else None,
        "last_message_at": _iso(conv.last_message_at or (last.created_at if last else None) or conv.created_at),
        "state": await brake_state(session, conv),
        "brake_approval_id": str(conv.brake_approval_id) if conv.brake_approval_id else None,
    }


async def list_conversations(session: AsyncSession) -> list[dict[str, Any]]:
    convs = (await session.exec(select(CommsConversation))).all()
    out = [await _conversation_out(session, c) for c in convs]
    out.sort(key=lambda c: c["last_message_at"] or "", reverse=True)
    return out


async def conversation_messages(session: AsyncSession, thread_id: uuid.UUID) -> dict[str, Any] | None:
    """None when the thread is not a session conversation — the operator's
    own direct threads have their own view and are not served here."""
    conv = await session.get(CommsConversation, thread_id)
    thread = await session.get(Thread, thread_id)
    if conv is None or thread is None or thread.kind != "session":
        return None
    rows = (
        await session.exec(
            select(Message).where(Message.thread_id == thread_id).order_by(Message.seq.asc())  # type: ignore[union-attr]
        )
    ).all()
    return {
        "conversation": await _conversation_out(session, conv),
        "messages": [await _message_out(session, m) for m in rows],
    }
