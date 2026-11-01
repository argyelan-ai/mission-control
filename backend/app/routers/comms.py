"""Guest cards for terminal sessions — operator routes (ADR-100 §6 rule 1).

Admin only, like every credential (ADR-098). The card token is shown once at
issue; lists never contain a token or a hash. Revoking a card ends every
session it enrolled; `revoke-all` switches every guest off at once.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.models.comms import CommsGuestCard, CommsSession
from app.services import comms_guest

router = APIRouter(prefix="/api/v1/comms", tags=["comms"])


class CardBody(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    ttl_hours: int = Field(default=comms_guest.DEFAULT_TTL_HOURS, ge=1, le=comms_guest.MAX_TTL_HOURS)


def _card_out(card: CommsGuestCard, sessions: int) -> dict:
    return {
        "id": str(card.id),
        "label": card.label,
        "expires_at": card.expires_at.isoformat(),
        "revoked_at": card.revoked_at.isoformat() if card.revoked_at else None,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "live_sessions": sessions,
    }


@router.post("/guest-cards", status_code=status.HTTP_201_CREATED)
async def issue_card(
    body: CardBody,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.ADMIN)),
):
    card, raw = await comms_guest.issue_card(
        session, label=body.label, ttl_hours=body.ttl_hours, user_id=getattr(user, "id", None),
    )
    return {**_card_out(card, 0), "token": raw}


@router.get("/guest-cards")
async def list_cards(
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.ADMIN)),
):
    live = await comms_guest.live_sessions(session)
    per_card: dict[uuid.UUID, int] = {}
    for row in live:
        per_card[row.card_id] = per_card.get(row.card_id, 0) + 1
    cards = (await session.exec(select(CommsGuestCard).order_by(CommsGuestCard.created_at.desc()))).all()  # type: ignore[union-attr]
    return {"cards": [_card_out(c, per_card.get(c.id, 0)) for c in cards]}


@router.post("/guest-cards/revoke-all")
async def revoke_all_cards(
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.ADMIN)),
):
    return {"revoked_cards": await comms_guest.revoke_all(session)}


@router.post("/guest-cards/{card_id}/revoke")
async def revoke_card(
    card_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.ADMIN)),
):
    if not await comms_guest.revoke_card(session, card_id):
        raise HTTPException(status_code=404, detail="guest card not found")
    return {"ok": True}


@router.get("/sessions")
async def list_sessions(
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.ADMIN)),
):
    rows: list[CommsSession] = await comms_guest.live_sessions(session)
    return {"sessions": [
        {
            "id": str(r.id),
            "card_id": str(r.card_id),
            "address": r.address,
            "harness": r.harness,
            "machine": r.machine,
            "folder": r.folder,
            "expires_at": r.expires_at.isoformat(),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]}


@router.post("/sessions/{session_id}/revoke")
async def revoke_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.ADMIN)),
):
    if not await comms_guest.revoke_session(session, session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True}
