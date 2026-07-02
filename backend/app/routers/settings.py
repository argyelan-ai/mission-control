import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import require_user
from app.database import get_session
from app.models.user import UserSettings
from app.utils import utcnow

router = APIRouter(prefix="/api/v1", tags=["settings"])


class SettingUpsert(BaseModel):
    value: Any


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
