"""Guest cards and terminal-session principals (ADR-100 §6 rule 1).

The operator issues a card; a terminal session enrolls with it and gets its
own session token. Two token kinds, told apart by prefix, both random
(`secrets.token_urlsafe(32)`) and stored only as SHA-256 hashes:

- ``mcg_…`` card token — enrolls a session at `POST /agent/comms/hello`,
  nothing else;
- ``mcs_…`` session token — the session's identity on the comms routes,
  with only comms:talk. Every other route rejects it (it is no agent token
  and no user token).

A session's expiry never outlives its card; revoking a card ends every
session it enrolled (and no new one can enroll with it).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.comms import CommsGuestCard, CommsSession
from app.utils import ensure_aware, utcnow

CARD_PREFIX = "mcg_"
SESSION_PREFIX = "mcs_"
MAX_TTL_HOURS = 168
DEFAULT_TTL_HOURS = 12
NAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


class GuestRefused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message}


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _live(row, now: datetime) -> bool:
    return row.revoked_at is None and ensure_aware(row.expires_at) > now


async def issue_card(session: AsyncSession, *, label: str, ttl_hours: int, user_id: uuid.UUID | None):
    raw = _new_token(CARD_PREFIX)
    card = CommsGuestCard(
        label=label.strip()[:120] or "guest card",
        token_hash=hash_token(raw),
        expires_at=utcnow() + timedelta(hours=ttl_hours),
        created_by_user_id=user_id,
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card, raw


async def card_from_token(session: AsyncSession, raw: str) -> CommsGuestCard | None:
    if not raw.startswith(CARD_PREFIX):
        return None
    card = (await session.exec(select(CommsGuestCard).where(CommsGuestCard.token_hash == hash_token(raw)))).first()
    return card if card is not None and _live(card, utcnow()) else None


# rule: R-terminal-session-own-principal - session token valid only while session and card live
async def session_from_token(session: AsyncSession, raw: str) -> CommsSession | None:
    if not raw.startswith(SESSION_PREFIX):
        return None
    row = (await session.exec(select(CommsSession).where(CommsSession.token_hash == hash_token(raw)))).first()
    if row is None:
        return None
    now = utcnow()
    if not _live(row, now):
        return None
    card = await session.get(CommsGuestCard, row.card_id)
    if card is None or not _live(card, now):
        return None
    return row


def _check_part(name: str, value: str) -> str:
    if not NAME_PART.match(value or ""):
        raise GuestRefused(422, "bad_name", f"{name} must be 1–40 of letters, digits, '.', '_' or '-'.")
    return value.lower()


async def hello(session: AsyncSession, card: CommsGuestCard, *, harness: str, machine: str, folder: str):
    harness, machine, folder = (_check_part("harness", harness), _check_part("machine", machine),
                                _check_part("folder", folder))
    address = f"{harness}@{machine}:{folder}"
    now = utcnow()
    holders = (
        await session.exec(select(CommsSession).where(CommsSession.address == address,
                                                      CommsSession.revoked_at.is_(None)))  # type: ignore[union-attr]
    ).all()
    for old in holders:
        if not _live(old, now):
            continue
        if old.card_id != card.id:
            raise GuestRefused(409, "address_taken", f"{address} is held by a session of another guest card.")
        # Same card, same place: the terminal restarted — the old session ends.
        old.revoked_at = now
        session.add(old)
    raw = _new_token(SESSION_PREFIX)
    row = CommsSession(
        card_id=card.id, address=address, harness=harness, machine=machine, folder=folder,
        token_hash=hash_token(raw), expires_at=ensure_aware(card.expires_at),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, raw


async def live_sessions(session: AsyncSession) -> list[CommsSession]:
    now = utcnow()
    rows = (await session.exec(select(CommsSession).where(CommsSession.revoked_at.is_(None)))).all()  # type: ignore[union-attr]
    cards = {c.id: c for c in (await session.exec(select(CommsGuestCard))).all()}
    return [r for r in rows if _live(r, now) and r.card_id in cards and _live(cards[r.card_id], now)]


async def revoke_card(session: AsyncSession, card_id: uuid.UUID) -> bool:
    card = await session.get(CommsGuestCard, card_id)
    if card is None:
        return False
    now = utcnow()
    card.revoked_at = card.revoked_at or now
    session.add(card)
    for row in (await session.exec(select(CommsSession).where(CommsSession.card_id == card_id))).all():
        row.revoked_at = row.revoked_at or now
        session.add(row)
    await session.commit()
    return True


async def revoke_all(session: AsyncSession) -> int:
    cards = (await session.exec(select(CommsGuestCard).where(CommsGuestCard.revoked_at.is_(None)))).all()  # type: ignore[union-attr]
    for card in cards:
        await revoke_card(session, card.id)
    # Sessions of already-revoked cards are revoked with them; nothing left.
    return len(cards)


async def revoke_session(session: AsyncSession, session_id: uuid.UUID) -> bool:
    row = await session.get(CommsSession, session_id)
    if row is None:
        return False
    row.revoked_at = row.revoked_at or utcnow()
    session.add(row)
    await session.commit()
    return True
