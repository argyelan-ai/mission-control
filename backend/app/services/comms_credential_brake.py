"""Credential brake after a cross-agent message (ADR-100 §6, Nachtrag 2026-11-02).

A session message is information, never a permission — but the receiving
model may still follow it ("read credential X and send it to me"). So a
credential read by an agent that received a session message within
`comms_send.TRIGGER_WINDOW` (30 min, the same lookup as the "triggered by a
message from X" label) is not served directly:

* the read is refused with HTTP 409 and code ``credential_read_needs_approval``;
* the operator gets ONE approval card (action ``credential_read``) per agent
  and target — a repeated read reuses the open card instead of adding one;
* after the operator approves it, that agent may read that target (one
  credential, the masked list, or the deploy tokens) for `GRANT_WINDOW` —
  as long as no newer message has arrived since (a new message needs a new
  card);
* a rejected card refuses the same read (HTTP 403,
  ``credential_read_rejected``) until a new message arrives.

Without a recent message nothing changes. The check is read-time only:
approving goes through the ordinary `PATCH /approvals/{id}`, no hook.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.services import comms_send
from app.utils import ensure_aware, utcnow

logger = logging.getLogger(__name__)

ACTION = "credential_read"
GRANT_WINDOW = timedelta(minutes=30)
CODE_HELD = "credential_read_needs_approval"
CODE_REJECTED = "credential_read_rejected"
MESSAGE_HELD = (
    "credential read needs operator approval: a cross-agent message arrived within 30 min"
)

#: Targets that are not a single credential id.
TARGET_LIST = "list"
TARGET_DEPLOY = "deploy"


def _target_of(card: Approval) -> str | None:
    return (card.payload or {}).get("target")


def _trigger_id_of(card: Approval) -> str | None:
    return ((card.payload or {}).get("triggered_by") or {}).get("message_id")


# rule: R-comms-credential-brake - no credential read straight after a cross-agent message
async def guard_credential_read(
    session: AsyncSession, agent: Agent, *, target: str, label: str
) -> None:
    """Return when the read may be served; raise HTTPException otherwise.

    `target` is a credential id (str), ``"list"`` or ``"deploy"``; `label`
    is what the operator reads on the card (never a secret)."""
    now = utcnow()
    message = await comms_send.trigger_message(session, agent.id, now)
    if message is None:
        return
    trigger = await comms_send.describe_trigger(session, message)

    rows = await session.exec(
        select(Approval).where(Approval.agent_id == agent.id, Approval.action_type == ACTION)
    )
    cards = [c for c in rows.all() if _target_of(c) == target]
    same_trigger = [c for c in cards if _trigger_id_of(c) == trigger["message_id"]]

    for card in same_trigger:
        if (
            card.status == "approved"
            and card.resolved_at is not None
            and ensure_aware(card.resolved_at) >= now - GRANT_WINDOW
        ):
            return
    if any(c.status == "rejected" for c in same_trigger):
        raise HTTPException(
            status_code=403,
            detail={
                "code": CODE_REJECTED,
                "message": "The operator refused this credential read after a cross-agent message.",
                "triggered_by": trigger,
            },
        )

    card = next((c for c in cards if c.status == "pending"), None)
    if card is not None:
        if _trigger_id_of(card) != trigger["message_id"]:
            # Reuse the open card; it now names the latest message.
            card.payload = {**(card.payload or {}), "triggered_by": trigger}
            session.add(card)
            await session.commit()
    else:
        card = await _raise_card(session, agent, target=target, label=label, trigger=trigger)

    raise HTTPException(
        status_code=409,
        detail={
            "code": CODE_HELD,
            "message": MESSAGE_HELD,
            "approval_id": str(card.id),
            "triggered_by": trigger,
        },
    )


async def _raise_card(
    session: AsyncSession, agent: Agent, *, target: str, label: str, trigger: dict
) -> Approval:
    from app.services import activity

    sender = trigger.get("from") or "another session"
    card = Approval(
        agent_id=agent.id,
        action_type=ACTION,
        description=(
            f"{agent.name} wants to read {label} shortly after a message from {sender}. "
            f"Allow it for {int(GRANT_WINDOW.total_seconds() // 60)} minutes?"
        ),
        payload={"target": target, "label": label, "triggered_by": trigger},
    )
    session.add(card)
    await session.flush()
    # Same channel every approval card uses to reach the operator's inbox.
    await activity.emit_event(
        session,
        event_type="approval.created",
        title=card.description,
        severity="warning",
        agent_id=agent.id,
        detail={"approval_id": str(card.id), "action_type": ACTION, "target": target},
    )
    await session.commit()
    await session.refresh(card)
    logger.info("credential brake: card %s for agent %s target %s", card.id, agent.id, target)
    return card
