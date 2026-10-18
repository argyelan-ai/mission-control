"""Config checks API — the one place to read and run "test this setting".

``GET /config-checks`` lists every thing the registry knows with its latest
result ("never tested" included); ``GET /config-checks/{kind}/{subject_id}``
reads one; ``POST …/run`` runs the registry procedure now and stores the
result. Responses never carry key material — only states, short scrubbed
texts and ages. The older per-page test buttons stay and write here too.

Reading is open to signed-in users (states only, no values). Running is
admin-only: it calls out with stored credentials and may spend rate limit
or plan quota (see the ``cost`` hint per item).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.services import config_checks
from app.services.config_checks import COST_HINTS, CheckNotRunnable, UnknownCheck

router = APIRouter(prefix="/api/v1/config-checks", tags=["config-checks"])


@router.get("")
async def list_config_checks(
    kind: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    try:
        items = await config_checks.status_items(session, kind)
    except UnknownCheck:
        raise HTTPException(status_code=404, detail=f"Unknown check kind '{kind}'")
    return {
        "kinds": config_checks.kinds_catalog(),
        "cost_hints": COST_HINTS,
        "items": items,
    }


@router.get("/{kind}/{subject_id}")
async def get_config_check(
    kind: str,
    subject_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    try:
        return await config_checks.status_item(session, kind, subject_id)
    except UnknownCheck:
        raise HTTPException(status_code=404, detail=f"Unknown check '{kind}/{subject_id}'")


@router.post("/{kind}/{subject_id}/run")
async def run_config_check(
    kind: str,
    subject_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    try:
        run = await config_checks.run_check(session, kind, subject_id)
    except UnknownCheck:
        raise HTTPException(status_code=404, detail=f"Unknown check '{kind}/{subject_id}'")
    except CheckNotRunnable as e:
        raise HTTPException(status_code=409, detail={"code": e.code, "item": e.item})
    return run.item
