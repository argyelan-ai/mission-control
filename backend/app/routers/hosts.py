"""
Hosts API — CRUD + live metrics for the host registry (ADR-048).

A host describes a physical box on which LLM runtimes run
(kind ssh | flask_wol | local). Runtimes bind via runtimes.host_id;
resolution goes through services/host_resolver.

Writes are admin-only — same rationale as runtime writes
(test_runtime_readiness_gate): ssh_host/control_url determine WHERE
remote commands land. Responses include ssh_key_path (just a
path, not a secret) — key CONTENTS are never read or served.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user, require_role, Role
from app.database import get_session
from app.models.host import Host
from app.models.runtime import Runtime
from app.services import runtime_manager
from app.services.host_resolver import resolved_host_from_row

router = APIRouter(prefix="/api/v1/hosts", tags=["hosts"])

_ALLOWED_KINDS = ("ssh", "flask_wol", "local")


def _validate_kind(v: str) -> str:
    if v not in _ALLOWED_KINDS:
        raise ValueError(f"kind muss eines von {list(_ALLOWED_KINDS)} sein")
    return v


def _validate_control_url(v: str | None) -> str | None:
    # Same rule as RuntimeCreate.control_url — prevents a typo'd scheme
    # (ftp://…) from later being addressed as a control server.
    if v is not None and not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("control_url muss mit http:// oder https:// beginnen")
    return v


class HostCreate(BaseModel):
    # max_length mirrors the String(N) columns in models/host.py — without it
    # an overlong value would only blow up in Postgres as StringDataRightTruncation
    # (500) instead of a clean 422 (SQLite tests don't enforce the length).
    slug: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    kind: str  # ssh | flask_wol | local
    ssh_host: str | None = Field(default=None, max_length=128)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_key_path: str | None = Field(default=None, max_length=512)
    control_url: str | None = Field(default=None, max_length=512)
    wol_mac_address: str | None = Field(default=None, max_length=32)
    power_managed: bool = False
    notes: str | None = None
    enabled: bool = True
    ui_order: int = 0

    @field_validator("kind")
    @classmethod
    def _kind_create(cls, v: str) -> str:
        return _validate_kind(v)

    @field_validator("control_url")
    @classmethod
    def _control_url_create(cls, v: str | None) -> str | None:
        return _validate_control_url(v)


class HostUpdate(BaseModel):
    slug: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    kind: str | None = None
    ssh_host: str | None = Field(default=None, max_length=128)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_key_path: str | None = Field(default=None, max_length=512)
    control_url: str | None = Field(default=None, max_length=512)
    wol_mac_address: str | None = Field(default=None, max_length=32)
    power_managed: bool | None = None
    notes: str | None = None
    enabled: bool | None = None
    ui_order: int | None = None

    @field_validator("kind")
    @classmethod
    def _kind_update(cls, v: str | None) -> str | None:
        return _validate_kind(v) if v is not None else None

    @field_validator("control_url")
    @classmethod
    def _control_url_update(cls, v: str | None) -> str | None:
        return _validate_control_url(v)


async def _get_host(session: AsyncSession, host_id: str) -> Host | None:
    """Slug-or-UUID lookup — same pattern as GET /runtimes/{runtime_id}."""
    host = (await session.exec(select(Host).where(Host.slug == host_id))).first()
    if not host:
        try:
            host_uuid = uuid.UUID(host_id)
        except ValueError:
            host_uuid = None
        if host_uuid is not None:
            host = await session.get(Host, host_uuid)
    return host


@router.get("")
async def list_hosts(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """All hosts, sorted by ui_order (then slug for stable ordering)."""
    hosts = (await session.exec(select(Host))).all()
    return sorted(hosts, key=lambda h: (h.ui_order, h.slug))


@router.post("")
async def create_host(
    body: HostCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Create a new host in the registry. Returns the saved row."""
    existing = (await session.exec(select(Host).where(Host.slug == body.slug))).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Host slug '{body.slug}' already exists")
    host = Host(**body.model_dump())
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


@router.patch("/{host_id}")
async def update_host(
    host_id: str,
    body: HostUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Update fields on a host (slug or UUID in the path).

    exclude_unset (not exclude_none like the runtime PATCH): nullable
    fields like notes/ssh_user must be explicitly resettable to null.
    """
    host = await _get_host(session, host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")
    changes = body.model_dump(exclude_unset=True)
    new_slug = changes.get("slug")
    if new_slug and new_slug != host.slug:
        existing = (await session.exec(select(Host).where(Host.slug == new_slug))).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Host slug '{new_slug}' already exists")
    for k, v in changes.items():
        setattr(host, k, v)
    host.updated_at = datetime.utcnow()
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


@router.delete("/{host_id}", status_code=204)
async def delete_host(
    host_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Delete a host. 409 while runtimes are still bound — rebind first,
    so no runtime silently falls back to the settings fallback box."""
    host = await _get_host(session, host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")
    bound = (await session.exec(select(Runtime).where(Runtime.host_id == host.id))).all()
    if bound:
        slugs = ", ".join(sorted(rt.slug for rt in bound))
        raise HTTPException(
            status_code=409,
            detail=(
                f"Host '{host.slug}' hat noch {len(bound)} gebundene Runtime(s): "
                f"{slugs}. Erst umbinden (PATCH /api/v1/runtimes/db/{{slug}} "
                f"mit host_id=null oder anderer Host-UUID), dann löschen."
            ),
        )
    await session.delete(host)
    await session.commit()
    return None


@router.get("/{host_id}/metrics")
async def host_metrics(
    host_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Live metrics for a host (ADR-048).

    - ssh       → nvidia-smi + free -m via SSH (get_host_metrics)
    - flask_wol → awake/health of the control server (mirrors unsloth_porsche state)
    - local     → empty object with kind field (the MC host doesn't measure itself)
    """
    host = await _get_host(session, host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")

    if host.kind == "local":
        return {"kind": "local", "slug": host.slug, "reachable": True}

    resolved = resolved_host_from_row(host)
    if host.kind == "flask_wol":
        # get_host_metrics' flask_wol branch probes the :5555 control server —
        # reachable == box awake + logged in (work-ready), otherwise it's asleep.
        m = await runtime_manager.get_host_metrics(resolved)
        awake = bool(m.get("reachable"))
        return {
            "kind": "flask_wol",
            "slug": host.slug,
            "reachable": awake,
            "awake": awake,
            "status": "awake" if awake else "asleep",
        }

    metrics = await runtime_manager.get_host_metrics(resolved)
    return {"kind": "ssh", "slug": host.slug, **metrics}
