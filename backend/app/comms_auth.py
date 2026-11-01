"""Who may talk across harnesses (ADR-100 §6).

`require_comms_principal` turns the bearer token into a principal. The
sender of everything that follows comes from here — from the token, never
from a request body (§6 rule 4, enforced tier).

- A session token (``mcs_…``) is a terminal session's own principal, with
  only comms:talk (§6 rule 1). It is valid only on the comms routes.
- A guest-card token (``mcg_…``) only enrolls (`require_guest_card` on
  `POST /agent/comms/hello`); everywhere else it is refused.
- An agent token counts only with comms:talk granted explicitly (not via
  the empty-list "all scopes" fallback) and only while not archived —
  `comms_directory.agent_may_talk`, the same rule the directory lists by.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import _authenticate_agent, bearer_scheme
from app.database import get_session
from app.models.comms import CommsGuestCard
from app.services import comms_guest
from app.services.comms_directory import (
    Principal,
    agent_may_talk,
    principal_for_agent,
    principal_for_session,
)


def _raw(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    return credentials.credentials


async def require_comms_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    raw = _raw(credentials)
    if raw.startswith(comms_guest.SESSION_PREFIX):
        row = await comms_guest.session_from_token(session, raw)
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or revoked")
        return principal_for_session(row)
    if raw.startswith(comms_guest.CARD_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A guest card only enrolls a session (POST /api/v1/agent/comms/hello).",
        )
    agent = await _authenticate_agent(session, credentials)
    if not agent_may_talk(agent):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="comms:talk not granted (explicit grant per agent; archived agents never talk)",
        )
    return principal_for_agent(agent)


async def require_guest_card(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> CommsGuestCard:
    card = await comms_guest.card_from_token(session, _raw(credentials))
    if card is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Guest card unknown, expired or revoked")
    return card
