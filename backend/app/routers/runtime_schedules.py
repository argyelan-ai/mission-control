"""
Runtime Schedules API — CRUD für Runtime-Zeitpläne.
"""
import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.auth import require_user
from app.services.runtime_schedule_service import (
    create_schedule,
    delete_schedule,
    get_runs,
    get_schedules,
    update_schedule,
)
from app.services.runtime_manager import get_runtime

router = APIRouter(prefix="/api/v1/runtimes", tags=["runtime-schedules"])


_VIRTUAL_RUNTIME_IDS = {"lmstudio"}  # Globale virtuelle IDs ohne runtimes.json-Eintrag


class RuntimeScheduleCreate(BaseModel):
    name: str
    action: Literal["start", "stop", "kv_reset"]
    time_of_day: str  # "HH:MM"
    days: Literal["daily", "weekdays", "weekends"]
    unload_first: bool = False
    enabled: bool = True

    @field_validator("time_of_day")
    @classmethod
    def validate_time_of_day(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time_of_day muss im Format HH:MM sein (24h, z.B. '22:00')")
        return v


class RuntimeSchedulePatch(BaseModel):
    name: str | None = None
    action: Literal["start", "stop", "kv_reset"] | None = None
    time_of_day: str | None = None
    days: Literal["daily", "weekdays", "weekends"] | None = None
    unload_first: bool | None = None
    enabled: bool | None = None

    @field_validator("time_of_day")
    @classmethod
    def validate_time_of_day(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time_of_day muss im Format HH:MM sein (24h, z.B. '22:00')")
        return v


def _require_runtime(runtime_id: str) -> None:
    """Prüft ob die Runtime existiert. Virtuelle IDs (z.B. 'lmstudio') werden akzeptiert."""
    if runtime_id in _VIRTUAL_RUNTIME_IDS:
        return
    rt = get_runtime(runtime_id)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden.")


@router.get("/{runtime_id}/schedules")
async def list_schedules(runtime_id: str, current_user=Depends(require_user)):
    """Alle Schedules für eine Runtime."""
    _require_runtime(runtime_id)
    return await get_schedules(runtime_id)


@router.post("/{runtime_id}/schedules", status_code=201)
async def create_schedule_endpoint(
    runtime_id: str,
    body: RuntimeScheduleCreate,
    current_user=Depends(require_user),
):
    """Neuen Schedule anlegen."""
    _require_runtime(runtime_id)
    return await create_schedule(runtime_id, body.model_dump())


@router.patch("/{runtime_id}/schedules/{schedule_id}")
async def patch_schedule(
    runtime_id: str,
    schedule_id: uuid.UUID,
    body: RuntimeSchedulePatch,
    current_user=Depends(require_user),
):
    """Schedule bearbeiten."""
    _require_runtime(runtime_id)
    updated = await update_schedule(schedule_id, body.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule nicht gefunden.")
    return updated


@router.delete("/{runtime_id}/schedules/{schedule_id}", status_code=204)
async def delete_schedule_endpoint(
    runtime_id: str,
    schedule_id: uuid.UUID,
    current_user=Depends(require_user),
):
    """Schedule löschen."""
    _require_runtime(runtime_id)
    deleted = await delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule nicht gefunden.")


@router.get("/{runtime_id}/schedules/{schedule_id}/runs")
async def get_schedule_runs(
    runtime_id: str,
    schedule_id: uuid.UUID,
    current_user=Depends(require_user),
):
    """Letzte 5 Ausführungen eines Schedules."""
    _require_runtime(runtime_id)
    return await get_runs(schedule_id, limit=5)
