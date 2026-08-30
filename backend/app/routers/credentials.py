"""Credentials Vault — encrypted credentials for agent tasks.

Same Fernet encryption as system secrets.
Credentials are decrypted at task dispatch time and handed to agents.
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
from app.services.encryption import encrypt, safe_decrypt, mask, NEVER_EXPOSE_CREDENTIAL_FIELDS

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


# Fields that are not secrets in the first place — shown verbatim, same as
# username always was. (The "never expose, period" list — private_key_pem —
# lives in services/encryption.NEVER_EXPOSE_CREDENTIAL_FIELDS, shared with
# routers/agent_scoped.py so both redaction points can't drift apart again;
# see that constant's docstring for the incident that made this a shared
# constant instead of a local one.)
_NON_SENSITIVE_FIELDS = ("username", "public_key")


def _mask_data(data: dict, credential_type: str) -> dict:
    """Mask sensitive fields, keep non-sensitive visible."""
    masked = {}
    for k, v in data.items():
        if k in NEVER_EXPOSE_CREDENTIAL_FIELDS:
            masked[k] = "[hidden]"
        elif k in _NON_SENSITIVE_FIELDS:
            masked[k] = v
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

    # Guard (Phase 2 review finding #2, 30.08.2026): an ssh_key credential is
    # auto-generated by host onboarding and a host's Vault access DEPENDS on
    # its private_key_pem — the frontend now hides the edit action for these,
    # but this guard is the actual enforcement (any other client, a future
    # UI regression, or a manual API call must hit the same wall).
    if credential.credential_type == "ssh_key":
        if payload.credential_type is not None and payload.credential_type != "ssh_key":
            raise HTTPException(
                status_code=422,
                detail=(
                    "ssh_key-Credentials werden von der Geräte-Einrichtung verwaltet — "
                    "der Typ kann hier nicht geändert werden."
                ),
            )
        if payload.data is not None and not str(payload.data.get("private_key_pem") or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "ssh_key-Credentials: private_key_pem darf nicht geleert werden — "
                    "der zugehörige Host würde seinen Vault-Zugang verlieren."
                ),
            )

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

    # State-aware validation: after merge, a login credential must have a url.
    # Applies e.g. when someone updates credential_type from "token" to "login"
    # without sending a url.
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
    # Explicitly set NULL (ON DELETE SET NULL is DB-level, doesn't work in SQLite tests)
    from sqlmodel import select, update
    from app.models.task import Task
    await session.exec(
        update(Task).where(Task.credential_id == credential_id).values(credential_id=None)
    )
    await session.delete(credential)
    await session.commit()
