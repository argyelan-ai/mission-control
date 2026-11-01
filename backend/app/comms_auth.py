"""Who may talk across harnesses (ADR-100 §6).

`require_comms_principal` turns the bearer token into a principal. The
sender of everything that follows comes from here — from the token, never
from a request body (§6 rule 4, enforced tier).

- An agent token counts only with comms:talk granted explicitly (not via
  the empty-list "all scopes" fallback) and only while not archived —
  `comms_directory.agent_may_talk`, the same rule the directory lists by.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.auth import require_agent
from app.services.comms_directory import Principal, agent_may_talk, principal_for_agent


async def require_comms_principal(agent=Depends(require_agent)) -> Principal:
    if not agent_may_talk(agent):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="comms:talk not granted (explicit grant per agent; archived agents never talk)",
        )
    return principal_for_agent(agent)
