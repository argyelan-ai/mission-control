"""Who may talk across harnesses (ADR-100 §6).

`require_comms_principal` turns the bearer token into a principal. The
sender of everything that follows comes from here — from the token, never
from a request body (§6 rule 4, enforced tier).

- An agent token counts only with the scope `comms:talk`.
"""
from __future__ import annotations

from fastapi import Depends

from app.scopes import Scope, require_scope
from app.services.comms_directory import Principal, principal_for_agent


async def require_comms_principal(
    agent=Depends(require_scope(Scope.COMMS_TALK)),
) -> Principal:
    return principal_for_agent(agent)
