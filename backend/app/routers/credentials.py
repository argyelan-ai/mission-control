"""Credentials Vault — verschluesselte Zugangsdaten fuer Agent-Tasks.

Gleiche Fernet-Verschluesselung wie System-Secrets.
Credentials werden bei Task-Dispatch entschluesselt und an Agents uebergeben.
"""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.credential import Credential
from app.services.encryption import encrypt, safe_decrypt, mask

router = APIRouter(prefix="/api/v1", tags=["credentials"])


_LOGIN_NEEDS_URL_MSG = (
    "credential_type='login' braucht eine url (z.B. 'http://caddy/login' oder "
    "'https://app.example.com/login'). Ohne url schlaegt der Vault-Resolve "
    "(mc verify --login-as) mit HTTP 422 fehl."
)


class CredentialCreate(BaseModel):
    name: str
    credential_type: str = "login"  # login | token | custom
    data: dict  # {"username": "...", "password": "..."} etc.
    url: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _login_requires_url(self):
        if self.credential_type == "login" and not (self.url and self.url.strip()):
            raise ValueError(_LOGIN_NEEDS_URL_MSG)
        return self


class CredentialUpdate(BaseModel):
    name: str | None = None
    credential_type: str | None = None
    data: dict | None = None
    url: str | None = None
    notes: str | None = None


def _mask_data(data: dict, credential_type: str) -> dict:
    """Mask sensitive fields, keep non-sensitive visible."""
    masked = {}
    for k, v in data.items():
        if k in ("username",):
            masked[k] = v  # username not sensitive
        else:
            masked[k] = mask(str(v)) if v else ""
    return masked


def _serialize(credential: Credential, decrypted_data: dict | None) -> dict:
    return {
        "id": str(credential.id),
        "name": credential.name,
        "credential_type": credential.credential_type,
        "data_masked": _mask_data(decrypted_data, credential.credential_type) if decrypted_data else {},
        "url": credential.url,
        "notes": credential.notes,
        "created_at": credential.created_at.isoformat() if credential.created_at else None,
        "updated_at": credential.updated_at.isoformat() if credential.updated_at else None,
    }


@router.get("/credentials")
async def list_credentials(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await session.exec(select(Credential).order_by(Credential.name))
    credentials = result.all()
    items = []
    for c in credentials:
        decrypted = safe_decrypt(c.encrypted_data)
        data = json.loads(decrypted) if decrypted else None
        items.append(_serialize(c, data))
    return items


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
async def create_credential(
    payload: CredentialCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    encrypted = encrypt(json.dumps(payload.data))
    credential = Credential(
        name=payload.name,
        credential_type=payload.credential_type,
        encrypted_data=encrypted,
        url=payload.url,
        notes=payload.notes,
    )
    session.add(credential)
    await session.commit()
    await session.refresh(credential)
    return _serialize(credential, payload.data)


@router.get("/credentials/{credential_id}")
async def get_credential(
    credential_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    credential = await session.get(Credential, credential_id)
    if not credential:
        raise HTTPException(404, "Credential not found")
    decrypted = safe_decrypt(credential.encrypted_data)
    data = json.loads(decrypted) if decrypted else None
    return _serialize(credential, data)


@router.patch("/credentials/{credential_id}")
async def update_credential(
    credential_id: uuid.UUID,
    payload: CredentialUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    credential = await session.get(Credential, credential_id)
    if not credential:
        raise HTTPException(404, "Credential not found")

    if payload.name is not None:
        credential.name = payload.name
    if payload.credential_type is not None:
        credential.credential_type = payload.credential_type
    if payload.data is not None:
        credential.encrypted_data = encrypt(json.dumps(payload.data))
    if payload.url is not None:
        credential.url = payload.url
    if payload.notes is not None:
        credential.notes = payload.notes

    # State-aware Validation: nach Merge muss login-Credential eine url haben.
    # Greift z.B. wenn jemand credential_type von "token" auf "login" updated
    # ohne url mitzuschicken.
    if credential.credential_type == "login" and not (credential.url and credential.url.strip()):
        raise HTTPException(status_code=422, detail=_LOGIN_NEEDS_URL_MSG)

    credential.updated_at = datetime.now(timezone.utc)
    session.add(credential)
    await session.commit()
    await session.refresh(credential)

    decrypted = safe_decrypt(credential.encrypted_data)
    data = json.loads(decrypted) if decrypted else None
    return _serialize(credential, data)


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    credential = await session.get(Credential, credential_id)
    if not credential:
        raise HTTPException(404, "Credential not found")
    # Explizit NULL setzen (ON DELETE SET NULL ist DB-Ebene, klappt nicht in SQLite-Tests)
    from sqlmodel import select, update
    from app.models.task import Task
    await session.exec(
        update(Task).where(Task.credential_id == credential_id).values(credential_id=None)
    )
    await session.delete(credential)
    await session.commit()
