"""Claude account API — the settings card behind "Claude" (Welle 2, PR 2-4).

``GET /claude-account`` shows the token's check (from the config-check
registry), when it last changed and which running agents still hold the old
one. ``/login`` drives ``claude setup-token`` in a throwaway container so the
user can reconnect from the browser (``services/claude_account``).

Saving a pasted token, testing it and disconnecting use the existing
endpoints (``/secrets``, ``/config-checks/secret/claude_code_oauth_token/run``),
and restarting an agent uses ``POST /agents/{id}/restart`` — this router adds
no second way to do any of that. Everything here is admin-only: it manages
the credential every Claude agent signs in with.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.services import claude_account

router = APIRouter(prefix="/api/v1/claude-account", tags=["claude-account"])


class LoginCode(BaseModel):
    code: str = Field(min_length=1, max_length=512, pattern=claude_account.CODE_RE.pattern)


@router.get("")
async def get_claude_account(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    return await claude_account.account_status(session)


@router.post("/login")
async def start_claude_login(current_user=Depends(require_role(Role.ADMIN))):
    return await claude_account.start_login()


@router.get("/login")
async def get_claude_login(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    return await claude_account.login_status(session)


@router.post("/login/code")
async def submit_claude_login_code(
    payload: LoginCode,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    try:
        return await claude_account.submit_code(session, payload.code.strip())
    except claude_account.NoLogin:
        raise HTTPException(status_code=409, detail="No sign-in is waiting for a code. Start again.")


@router.delete("/login")
async def cancel_claude_login(current_user=Depends(require_role(Role.ADMIN))):
    return await claude_account.cancel_login()
