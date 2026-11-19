import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.user import UserSettings
from app.services import agent_language_defaults
from app.utils import utcnow

router = APIRouter(prefix="/api/v1", tags=["settings"])


class SettingUpsert(BaseModel):
    value: Any


# ── Fleet default languages for new agents (Settings → Language) ──────────
# Static path — must stay above /settings/{key} (route order). Not a per-user
# preference: one operator decision for the whole fleet, kept in app_settings.


class AgentLanguageDefaultsUpdate(BaseModel):
    operator_language: str | None = None
    work_language: str | None = None


def _language_view(defaults: agent_language_defaults.LanguageDefaults) -> dict:
    return {
        "defaults": defaults.to_dict(),
        "supported": list(agent_language_defaults.SUPPORTED_LANGUAGES),
    }


@router.get("/settings/agent-language")
async def get_agent_language_defaults(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Languages a NEW agent gets (existing agents keep their own)."""
    return _language_view(await agent_language_defaults.load_defaults(session))


@router.put("/settings/agent-language")
async def put_agent_language_defaults(
    payload: AgentLanguageDefaultsUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    updates = payload.model_dump(exclude_none=True)
    try:
        saved = await agent_language_defaults.save_defaults(session, updates)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_language", "fields": exc.args[0]},
        ) from exc
    return _language_view(saved)


@router.get("/settings")
async def list_settings(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await session.exec(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings_list = result.all()
    return {s.key: s.value for s in settings_list}


@router.get("/settings/{key}")
async def get_setting(
    key: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await session.exec(
        select(UserSettings).where(
            UserSettings.user_id == current_user.id, UserSettings.key == key
        )
    )
    setting = result.first()
    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")
    return {"key": key, "value": setting.value}


@router.put("/settings/{key}")
async def upsert_setting(
    key: str,
    payload: SettingUpsert,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await session.exec(
        select(UserSettings).where(
            UserSettings.user_id == current_user.id, UserSettings.key == key
        )
    )
    setting = result.first()

    if setting:
        setting.value = payload.value
        setting.updated_at = utcnow()
    else:
        setting = UserSettings(
            user_id=current_user.id, key=key, value=payload.value
        )
    session.add(setting)
    await session.commit()
    return {"key": key, "value": payload.value}
