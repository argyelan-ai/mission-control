"""
Nodes API — self-registration + push telemetry for mc-node-agent
(Fleet & Rezepte v2, Phase 1; see docs/plans/2026-08-30-node-agent-telemetry-phase1.md).

Beschluss (Mark, 30.08.2026): SSH-Pull-Telemetrie funktioniert nur mit
vorverdrahteten Keys — ein neuer Nutzer ohne SSH-Zugriff sieht nie Metriken.
Dieser Router dreht das Modell um: ein Gerät meldet sich selbst per
Pairing-Code an und pusht danach alle 15s seine Telemetrie per HTTPS.

Endpunkte, nach Auth-Stufe:
- POST /pairing-codes  — admin-only (wie hosts.py-Schreibzugriffe): mint
  einen kurzlebigen Code, den der Operator auf das Zielgerät überträgt.
- GET  /agent-script    — UNAUTHENTIFIZIERT: liefert das mc-node-agent.py
  dieser laufenden Instanz aus (Install-Einzeiler curlt von hier).
- POST /pair            — UNAUTHENTIFIZIERT (der Code IST die Auth, einmalig
  und 15 Minuten gültig): tauscht Code gegen node_token.
- POST /heartbeat        — Bearer node_token, konstantzeitverglichen gegen den
  gespeicherten Hash (nie den Klartext-Token in der DB).
- GET  /{host_id}/inventory — wie hosts.py-Lesezugriffe: letzter gemeldeter
  Gewichte-Bestand (Nachtrag, für Phase 2).

Scope Phase 1 ist reines Monitoring: `commands` in der Heartbeat-Antwort ist
bewusst immer eine leere Liste (Platzhalter für Phase 3 — Befehlsausführung).
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.config import node_agent_base_url
from app.database import get_session
from app.models.host import Host
from app.models.host_pairing_code import HostPairingCode
from app.utils import ensure_aware, slugify, utcnow

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])

_bearer_scheme = HTTPBearer(auto_error=False)

PAIRING_CODE_TTL_MINUTES = 15
HEARTBEAT_INTERVAL_S = 15
_HEARTBEAT_MIN_INTERVAL_S = 5  # rate guard — see heartbeat()

# No 0/O/1/I — a code that gets read aloud off a terminal must not be
# ambiguous between digit and letter.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

# Where the docker-compose bind mount (./scripts/mc-node-agent.py ->
# /app/scripts/mc-node-agent.py:ro) lands inside the backend container —
# same "soft, feature-gated" convention as jarvis_core's /app/jarvis_core
# mount (docker-compose.yml). See get_agent_script() below.
_AGENT_SCRIPT_PATH = Path("/app/scripts/mc-node-agent.py")

_AGENT_INSTALL_PATH = "/usr/local/bin/mc-node-agent.py"


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _build_install_command(code: str) -> str:
    """Downloads to a real path first (not `curl | python3 -`): --install's
    systemd unit needs a stable ExecStart path, which a stdin-piped script
    can never have (no __file__ to point at).

    Review finding #10 (30.08.2026): this used to curl a GitHub raw URL
    pinned to `main`, which drifts from whatever version THIS backend
    actually understands (heartbeat schema, endpoint paths) — a fork, a
    self-host on a different branch, or just this repo's own next release
    would hand out an agent that doesn't match the API it's talking to.
    Serving the running instance's own copy (GET /agent-script) makes the
    two inseparable by construction.
    """
    base_url = node_agent_base_url()
    return (
        f"sudo curl -fsSL {base_url}/api/v1/nodes/agent-script -o {_AGENT_INSTALL_PATH} && "
        f"sudo python3 {_AGENT_INSTALL_PATH} --mc-url {base_url} --pair {code} --install"
    )


async def _unique_slug(session: AsyncSession, hostname: str) -> str:
    """Same dedup shape as group_service's doc_slug — base name, then -2, -3, …"""
    base = slugify(hostname, max_length=56) or "agent"
    slug = base
    i = 2
    while (await session.exec(select(Host).where(Host.slug == slug))).first() is not None:
        slug = f"{base}-{i}"
        i += 1
    return slug


async def _resolve_host(session: AsyncSession, host_id: str) -> Host | None:
    """Slug-or-UUID lookup — mirrors hosts.py's _get_host so /nodes/{host_id}/…
    accepts the same identifiers as /hosts/{host_id}/…."""
    host = (await session.exec(select(Host).where(Host.slug == host_id))).first()
    if not host:
        try:
            host_uuid = uuid.UUID(host_id)
        except ValueError:
            host_uuid = None
        if host_uuid is not None:
            host = await session.get(Host, host_uuid)
    return host


async def _authenticate_node(
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials | None,
) -> Host:
    """Bearer node_token → Host, hash-compared in constant time.

    Iterates the (small — one fleet's worth of GPU boxes) set of hosts that
    have ever paired, rather than a DB WHERE agent_token_hash = :hash — an
    indexed equality lookup only avoids table-scan cost, not the hmac
    requirement itself, and the fleet size here is never large enough for
    the scan to matter.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node-Token fehlt")
    presented_hash = _hash_token(credentials.credentials)
    candidates = (
        await session.exec(select(Host).where(Host.agent_token_hash.is_not(None)))
    ).all()
    for host in candidates:
        if hmac.compare_digest(host.agent_token_hash, presented_hash):
            return host
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ungültiger Node-Token")


# ── Pydantic bodies ──────────────────────────────────────────────────────────


class PairingCodeCreate(BaseModel):
    host_id: str | None = None
    display_name_hint: str | None = Field(default=None, max_length=128)


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: datetime
    host_id: str | None
    install_command: str


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=12)
    hostname: str = Field(min_length=1, max_length=128)
    os: str | None = Field(default=None, max_length=64)
    arch: str | None = Field(default=None, max_length=32)
    agent_version: str | None = Field(default=None, max_length=32)


class PairResponse(BaseModel):
    node_token: str
    host_id: str
    heartbeat_interval_s: int = HEARTBEAT_INTERVAL_S


class NodeTelemetry(BaseModel):
    """Everything optional except ``ts`` — a box mid-boot or without a GPU
    still sends a valid heartbeat with most fields null (GB10/unified memory
    boxes null out the vram_* fields the same way TelemetryColumn already
    tolerates for SSH hosts)."""

    ts: datetime
    cpu_pct: float | None = None
    load1: float | None = None
    mem_used_mb: int | None = None
    mem_total_mb: int | None = None
    mem_available_mb: int | None = None
    swap_used_mb: int | None = None
    disk_used_gb: float | None = None
    disk_total_gb: float | None = None
    gpu_util_pct: int | None = None
    gpu_temp_c: int | None = None
    vram_used_mb: int | None = None
    vram_total_mb: int | None = None


class InventoryEntry(BaseModel):
    """One scanned model directory (scripts/mc-node-agent.py's
    scan_model_inventory). ``hf_repo_id`` is only set for HF-cache-style
    entries (models--Org--Name); local models-local dirs leave it null and
    rely on name/size matching in Phase 2 instead."""

    name: str
    total_bytes: int
    file_count: int
    mtime_max: float | None = None
    hf_repo_id: str | None = None
    model_type: str | None = None


class HeartbeatRequest(BaseModel):
    telemetry: NodeTelemetry
    agent_version: str | None = Field(default=None, max_length=32)
    # Nachtrag 30.08.2026: the agent only attaches this every ~40th
    # heartbeat AND only when its own hash of the scan changed — omitted
    # (None) on every other heartbeat, which must leave the last stored
    # inventory untouched rather than wiping it (see heartbeat()).
    inventory: list[InventoryEntry] | None = None


class HeartbeatResponse(BaseModel):
    ok: bool = True
    heartbeat_interval_s: int = HEARTBEAT_INTERVAL_S
    # Phase 3 placeholder (Befehlsausführung) — Phase 1 never populates this.
    commands: list = Field(default_factory=list)


class InventoryResponse(BaseModel):
    host_id: str
    agent_inventory: list[dict] | None
    agent_inventory_updated_at: datetime | None


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/pairing-codes", response_model=PairingCodeResponse)
async def create_pairing_code(
    body: PairingCodeCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Mints a one-shot, 15-minute pairing code + the ready-to-paste install
    command. Admin-only — same rationale as host writes (hosts.py): this
    provisions a new device that will later push data into the registry."""
    host_uuid: uuid.UUID | None = None
    if body.host_id:
        try:
            host_uuid = uuid.UUID(body.host_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="host_id ist keine gültige UUID")
        host = await session.get(Host, host_uuid)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{body.host_id}' nicht gefunden")

    code = _generate_code()
    for _ in range(5):  # collision safety net — practically never hit at 32^8 codes
        if not (await session.exec(select(HostPairingCode).where(HostPairingCode.code == code))).first():
            break
        code = _generate_code()

    expires_at = utcnow() + timedelta(minutes=PAIRING_CODE_TTL_MINUTES)
    pairing = HostPairingCode(
        code=code,
        host_id=host_uuid,
        display_name_hint=body.display_name_hint,
        expires_at=expires_at,
    )
    session.add(pairing)
    await session.commit()

    return PairingCodeResponse(
        code=code,
        expires_at=expires_at,
        host_id=str(host_uuid) if host_uuid else None,
        install_command=_build_install_command(code),
    )


@router.get("/agent-script", response_class=PlainTextResponse)
async def get_agent_script():
    """Serves THIS instance's own scripts/mc-node-agent.py (review finding
    #10, 30.08.2026) — see _build_install_command's docstring for why that
    matters. UNAUTHENTICATED on purpose: mission-control is a public repo
    (PUBLIC-UPSTREAM seit 03.07.2026), this file carries no secrets, and an
    unpaired device by definition has no credential to authenticate with
    yet — that's the whole point of the pairing flow this file kicks off.

    Requires the docker-compose bind mount (jarvis_core's convention, see
    _AGENT_SCRIPT_PATH) — a plain image run without it is a clean 404, not
    a crash, same as jarvis_core's "feature stays off" fallback.
    """
    try:
        return PlainTextResponse(_AGENT_SCRIPT_PATH.read_text(encoding="utf-8"))
    except OSError:
        raise HTTPException(
            status_code=404,
            detail=(
                "mc-node-agent.py ist auf dieser Instanz nicht verfügbar — "
                "fehlt der docker-compose-Mount ./scripts/mc-node-agent.py:/app/scripts/mc-node-agent.py:ro?"
            ),
        )


@router.post("/pair", response_model=PairResponse)
async def pair(body: PairRequest, session: AsyncSession = Depends(get_session)):
    """Trades a pairing code for a node_token. UNAUTHENTICATED — the code
    itself is the credential (single-use, short-lived, see mint above).

    Review finding #3 (30.08.2026): this endpoint is unauthenticated, so a
    code racing against ITSELF (replayed/leaked, or just a flaky client
    retrying) is the actual threat model — without a row lock, two
    concurrent requests can both read used_at=None and both redeem it.
    ``with_for_update()`` closes that (Postgres only — SQLite, used only in
    tests, has no concurrent writers in-process anyway; see messaging.py's
    _next_seq for the same pattern). A second, independent race lives in
    _unique_slug: two different codes for the same hostname can both pass
    the "not taken" check before either inserts — the DB's unique index on
    hosts.slug is the actual referee, so IntegrityError there also becomes
    a 409 (with a retry hint) instead of a raw 500.
    """
    stmt = select(HostPairingCode).where(HostPairingCode.code == body.code)
    if session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    pairing = (await session.exec(stmt)).first()
    if not pairing:
        raise HTTPException(status_code=404, detail="Pairing-Code unbekannt")
    if pairing.used_at is not None:
        raise HTTPException(status_code=409, detail="Pairing-Code wurde bereits eingelöst")
    if ensure_aware(pairing.expires_at) < utcnow():
        raise HTTPException(status_code=410, detail="Pairing-Code ist abgelaufen (15-Minuten-Frist)")

    if pairing.host_id:
        host = await session.get(Host, pairing.host_id)
        if not host:
            raise HTTPException(
                status_code=404,
                detail="Der Host zu diesem Pairing-Code wurde inzwischen gelöscht",
            )
    else:
        slug = await _unique_slug(session, body.hostname)
        host = Host(
            slug=slug,
            display_name=pairing.display_name_hint or body.hostname,
            kind="agent",
        )
        session.add(host)
        try:
            await session.flush()  # host.id needed below, same transaction as used_at
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="Ein Host mit diesem Namen wurde gerade gleichzeitig angelegt — bitte erneut versuchen.",
            )

    token = secrets.token_urlsafe(32)
    host.agent_token_hash = _hash_token(token)
    if body.agent_version is not None:
        host.agent_version = body.agent_version
    session.add(host)

    pairing.used_at = utcnow()
    session.add(pairing)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Pairing-Code wurde gerade gleichzeitig eingelöst — bitte erneut versuchen.",
        )
    await session.refresh(host)

    return PairResponse(node_token=token, host_id=str(host.id))


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    body: HeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
):
    """Push telemetry — only the LAST snapshot is kept (hosts.agent_telemetry).

    Rate guard: a host that heartbeats faster than every 5s gets 429 instead
    of hammering the row with writes — 15s is the agent's own interval, 5s
    just tolerates a client that misbehaves or double-fires."""
    host = await _authenticate_node(session, credentials)

    now = utcnow()
    if host.agent_last_seen_at is not None:
        age_s = (now - ensure_aware(host.agent_last_seen_at)).total_seconds()
        if age_s < _HEARTBEAT_MIN_INTERVAL_S:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Heartbeat zu häufig — mindestens {_HEARTBEAT_MIN_INTERVAL_S}s Abstand",
            )

    host.agent_telemetry = body.telemetry.model_dump(mode="json")
    host.agent_last_seen_at = now
    if body.agent_version is not None:
        host.agent_version = body.agent_version
    if body.inventory is not None:
        host.agent_inventory = [entry.model_dump(mode="json") for entry in body.inventory]
        host.agent_inventory_updated_at = now
    session.add(host)
    await session.commit()

    return HeartbeatResponse()


@router.get("/{host_id}/inventory", response_model=InventoryResponse)
async def get_inventory(
    host_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Last reported model-weights inventory (Nachtrag 30.08.2026) — Phase 2
    reads this to skip re-downloading a recipe's model when it's already on
    the box. Slug-or-UUID lookup, same as GET /hosts/{host_id}/metrics."""
    host = await _resolve_host(session, host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")
    return InventoryResponse(
        host_id=str(host.id),
        agent_inventory=host.agent_inventory,
        agent_inventory_updated_at=host.agent_inventory_updated_at,
    )
