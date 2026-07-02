"""
Hosts API — CRUD + Live-Metrics für die Host-Registry (ADR-048).

Ein Host beschreibt eine physische Box, auf der LLM-Runtimes laufen
(kind ssh | flask_wol | local). Runtimes binden via runtimes.host_id;
die Auflösung läuft über services/host_resolver.

Writes sind admin-only — gleiche Begründung wie Runtime-Writes
(test_runtime_readiness_gate): ssh_host/control_url bestimmen, WO
Remote-Kommandos landen. Responses enthalten ssh_key_path (nur ein
Pfad, kein Secret) — Key-INHALTE werden nie gelesen oder ausgeliefert.
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
    # Gleiche Regel wie RuntimeCreate.control_url — verhindert dass ein
    # Tippfehler-Schema (ftp://…) später als Control-Server angesprochen wird.
    if v is not None and not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("control_url muss mit http:// oder https:// beginnen")
    return v


class HostCreate(BaseModel):
    # max_length spiegelt die String(N)-Spalten in models/host.py — ohne das
    # würde ein überlanger Wert erst in Postgres als StringDataRightTruncation
    # (500) knallen statt als sauberes 422 (SQLite-Tests erzwingen keine Länge).
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
    """Slug-or-UUID Lookup — gleiches Pattern wie GET /runtimes/{runtime_id}."""
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
    """Alle Hosts, sortiert nach ui_order (dann slug für stabile Reihenfolge)."""
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
    """Update fields on a host (slug oder UUID im Pfad).

    exclude_unset (nicht exclude_none wie beim Runtime-PATCH): nullable
    Felder wie notes/ssh_user müssen explizit auf null zurücksetzbar sein.
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
    """Delete a host. 409 solange noch Runtimes gebunden sind — erst umbinden,
    damit keine Runtime stumm auf die Settings-Fallback-Box zurückfällt."""
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
    """Live-Metrics eines Hosts (ADR-048).

    - ssh       → nvidia-smi + free -m via SSH (get_host_metrics)
    - flask_wol → awake/health des Control-Servers (Muster unsloth_porsche-State)
    - local     → leeres Objekt mit kind-Feld (der MC-Host misst sich nicht selbst)
    """
    host = await _get_host(session, host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")

    if host.kind == "local":
        return {"kind": "local", "slug": host.slug, "reachable": True}

    resolved = resolved_host_from_row(host)
    if host.kind == "flask_wol":
        # get_host_metrics' flask_wol-Zweig probt den :5555 Control-Server —
        # reachable == Box wach + eingeloggt (work-ready), sonst schläft sie.
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
