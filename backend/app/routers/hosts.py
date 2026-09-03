"""
Hosts API — CRUD + live metrics for the host registry (ADR-048).

A host describes a physical box on which LLM runtimes run
(kind ssh | flask_wol | local). Runtimes bind via runtimes.host_id;
resolution goes through services/host_resolver.

Writes are admin-only — same rationale as runtime writes
(test_runtime_readiness_gate): ssh_host/control_url determine WHERE
remote commands land. Responses include ssh_key_path (just a
path, not a secret) — key CONTENTS are never read or served.

Beyond CRUD this router carries the Box-Wizard's two remote operations:
``POST /probe`` (read-only inventory, services/host_probe) and
``POST /{id}/bootstrap`` + ``GET /{id}/bootstrap/log`` (idempotent
docker/nvidia-toolkit setup, services/host_bootstrap). Both are admin-only
and both go through runtime_manager._ssh_run — there is exactly one SSH
implementation in this codebase.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user, require_role, Role
from app.database import get_session
from app.models.host import Host, normalise_role
from app.models.runtime import Runtime
from app.services import host_bootstrap, host_onboarding, host_probe, launch_template, runtime_manager
from app.services.host_resolver import ResolvedHost, resolved_host_from_row, ssh_capable

router = APIRouter(prefix="/api/v1/hosts", tags=["hosts"])

_ALLOWED_KINDS = ("ssh", "flask_wol", "local", "agent")

# Rezept-Umschalter P2: Geräterolle — nur head/worker oder leer. Die Rolle ist
# eine Vorbelegung für den Duo-Dialog, keine Regel: sie sperrt nirgends etwas,
# darum wird sie nur auf Tippfehler geprüft (models/host.normalise_role, dieselbe
# Regel im Pairing-Code und im Passwort-Onboarding).
_validate_role = normalise_role


def _validate_fabric_ip(v: str | None) -> str | None:
    """Trimmen, leer → None. P3 schreibt das Feld unverändert als
    HEAD_IP/WORKER_IP in die .env des Rezepts — ein Leerzeichen dort wäre
    eine tote Adresse (Review 03.09.2026)."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def _require_ssh_capable(host: Host, what: str) -> None:
    """400 mit Satz, wenn MC die Box nicht per SSH erreichen kann.

    Seit P2 zählt nicht mehr ``kind == "ssh"``, sondern ob eine SSH-Adresse
    da ist (host_resolver.ssh_capable): eine per Pairing angelegte
    ``kind=agent``-Box mit ``ssh_host`` ist genauso erreichbar.
    """
    if ssh_capable(host):
        return
    if host.kind == "agent":
        detail = (
            f"Box '{host.slug}' hat keinen SSH-Zugang — {what} braucht eine SSH-Adresse. "
            "Unter Geräte-Einstellungen 'SSH-Adresse' eintragen."
        )
    else:
        detail = f"Host '{host.slug}' hat kind='{host.kind}' — {what} gibt es nur für SSH-Hosts."
    raise HTTPException(status_code=400, detail=detail)


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
    # Tailscale address for this box (100.64.0.0/10 or a *.ts.net name) —
    # optional. When set, runtime endpoints prefer it over ssh_host; see
    # services/address_classify and models/host.Host.tailscale_host.
    tailscale_host: str | None = Field(default=None, max_length=128)
    control_url: str | None = Field(default=None, max_length=512)
    wol_mac_address: str | None = Field(default=None, max_length=32)
    power_managed: bool = False
    notes: str | None = None
    enabled: bool = True
    ui_order: int = 0
    # Rezept-Umschalter P2: Geräterolle (head | worker | null) und die
    # Verbund-Adresse, unter der sich die Boxen gegenseitig erreichen.
    role: str | None = Field(default=None, max_length=16)
    fabric_ip: str | None = Field(default=None, max_length=128)

    @field_validator("kind")
    @classmethod
    def _kind_create(cls, v: str) -> str:
        return _validate_kind(v)

    @field_validator("role")
    @classmethod
    def _role_create(cls, v: str | None) -> str | None:
        return _validate_role(v)

    @field_validator("fabric_ip")
    @classmethod
    def _fabric_ip_create(cls, v: str | None) -> str | None:
        return _validate_fabric_ip(v)

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
    tailscale_host: str | None = Field(default=None, max_length=128)
    control_url: str | None = Field(default=None, max_length=512)
    wol_mac_address: str | None = Field(default=None, max_length=32)
    power_managed: bool | None = None
    notes: str | None = None
    enabled: bool | None = None
    ui_order: int | None = None
    role: str | None = Field(default=None, max_length=16)
    fabric_ip: str | None = Field(default=None, max_length=128)

    @field_validator("kind")
    @classmethod
    def _kind_update(cls, v: str | None) -> str | None:
        return _validate_kind(v) if v is not None else None

    @field_validator("role")
    @classmethod
    def _role_update(cls, v: str | None) -> str | None:
        return _validate_role(v)

    @field_validator("fabric_ip")
    @classmethod
    def _fabric_ip_update(cls, v: str | None) -> str | None:
        return _validate_fabric_ip(v)

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


# Fields hidden from GET /hosts (list) for viewers — the operational detail
# useful to admin/operator, not to a read-only viewer. GET /{host_id}/metrics
# stays the viewer-safe way to see live numbers (it never touches the raw
# ORM object, so it was never affected by this).
_VIEWER_HIDDEN_FIELDS = ("agent_telemetry", "agent_inventory", "agent_inventory_updated_at")


def _serialize_host(host: Host, current_user) -> dict:
    """Host row -> API dict, agent-channel fields filtered by role
    (review finding #4, 30.08.2026).

    agent_token_hash is NEVER returned to ANY role — it's an implementation
    detail of heartbeat auth (routers/nodes.py._authenticate_node), no
    client needs it, and serving a hash widens the attack surface for
    nothing. Unlike ssh_key_path (a path, not a secret — see module
    docstring), this field really is sensitive-adjacent and has no reason
    to ever leave the backend.
    """
    data = host.model_dump(mode="json")
    data.pop("agent_token_hash", None)
    if current_user.role == Role.VIEWER:
        for field in _VIEWER_HIDDEN_FIELDS:
            data.pop(field, None)
    return data


@router.get("")
async def list_hosts(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """All hosts, sorted by ui_order (then slug for stable ordering)."""
    hosts = (await session.exec(select(Host))).all()
    ordered = sorted(hosts, key=lambda h: (h.ui_order, h.slug))
    return [_serialize_host(h, current_user) for h in ordered]


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
    return _serialize_host(host, current_user)


# ── Box-Wizard: probe + bootstrap ────────────────────────────────────────────
#
# Router ordering: the static ``/probe`` path is declared before ``/{host_id}``
# so FastAPI can never parse "probe" as a host slug. Same rule as
# local_registry's ``/refresh`` — cheap now, correct the day someone adds a
# ``POST /{host_id}``.


class HostProbeBody(BaseModel):
    """Either an existing host (``host_id``) or ad-hoc credentials.

    Ad-hoc is the wizard's step 1: the operator types connection details for a
    box that has no row yet, and only once the probe succeeds does the row get
    created. Probing before persisting is the whole point — otherwise every
    typo leaves a dead host behind.
    """

    host_id: str | None = None
    ssh_host: str | None = Field(default=None, max_length=128)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_key_path: str | None = Field(default=None, max_length=512)


@router.post("/probe")
async def probe_host(
    body: HostProbeBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Read-only hardware/software inventory of a box over SSH.

    Admin-only for the same reason writes are: ssh_host decides WHERE a remote
    command lands, and this endpoint accepts one from the request body.

    An unreachable box is a 200 with ``reachable: false`` and a reason — see
    services/host_probe. Only a malformed request (no host at all) is a 4xx.
    """
    if body.host_id:
        host = await _get_host(session, body.host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{body.host_id}' nicht gefunden")
        _require_ssh_capable(host, "Probe")
        resolved = resolved_host_from_row(host)
    else:
        if not body.ssh_host:
            raise HTTPException(
                status_code=422,
                detail="ssh_host oder host_id muss gesetzt sein.",
            )
        resolved = ResolvedHost(
            ssh_host=body.ssh_host,
            ssh_user=body.ssh_user,
            ssh_key_path=body.ssh_key_path,
            kind="ssh",
            source="settings",
        )

    return await host_probe.probe_host(resolved)


class HostOnboardAuth(BaseModel):
    """Exactly one of the three."""

    password: str | None = None
    private_key: str | None = None
    use_existing_credential_id: str | None = None

    @field_validator("use_existing_credential_id")
    @classmethod
    def _credential_id_is_uuid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("use_existing_credential_id ist keine gültige UUID")
        return v

    @model_validator(mode="after")
    def _exactly_one(self):
        provided = [v for v in (self.password, self.private_key, self.use_existing_credential_id) if v]
        if len(provided) != 1:
            raise ValueError(
                "auth braucht genau eines von: password, private_key, use_existing_credential_id"
            )
        return self


class HostOnboardBody(BaseModel):
    """POST /hosts/onboard — see services/host_onboarding.py's module
    docstring for the full flow this kicks off."""

    address: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=64)
    auth: HostOnboardAuth
    display_name: str | None = Field(default=None, max_length=128)
    bootstrap: bool = True
    install_agent: bool = True
    # P2: Geräterolle darf auf jedem Weg mitkommen, der eine Box anlegt.
    role: str | None = Field(default=None, max_length=16)

    @field_validator("role")
    @classmethod
    def _role_onboard(cls, v: str | None) -> str | None:
        return normalise_role(v)


@router.post("/onboard", status_code=202)
async def onboard_host(
    body: HostOnboardBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Starts the auto-onboarding job (Fleet & Rezepte v2, Phase 2) and
    returns immediately — poll GET /onboard/{job_id}/log for progress.
    Admin-only + rate-limited: this endpoint points MC's own network
    position at an address the operator supplies, which is exactly the
    shape of an SSH brute-force tool if left unguarded (see
    services/host_onboarding.check_rate_limit — max 3 failed auths per
    address per 10 minutes, checked before a job is even created).
    """
    existing_credential_id: uuid.UUID | None = None
    if body.auth.use_existing_credential_id:
        existing_credential_id = uuid.UUID(body.auth.use_existing_credential_id)
        from app.models.credential import Credential

        credential = await session.get(Credential, existing_credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credential nicht gefunden.")
        if credential.credential_type != "ssh_key":
            raise HTTPException(
                status_code=422,
                detail=f"Credential '{credential.name}' hat credential_type='{credential.credential_type}', "
                "kein ssh_key.",
            )

    params = host_onboarding.OnboardParams(
        address=body.address,
        username=body.username,
        password=body.auth.password,
        private_key_pem=body.auth.private_key,
        existing_credential_id=existing_credential_id,
        display_name=body.display_name,
        bootstrap=body.bootstrap,
        install_agent=body.install_agent,
        role=body.role,
    )
    try:
        job_id = await host_onboarding.start_onboarding(params)
    except host_onboarding.RateLimitExceeded:
        raise HTTPException(
            status_code=429,
            detail=f"Zu viele fehlgeschlagene SSH-Logins für '{body.address}' — bitte 10 Minuten warten.",
        )
    return {"job_id": job_id}


@router.get("/onboard/{job_id}/log")
async def onboard_log(
    job_id: str,
    cursor: int = Query(default=0, ge=0),
    current_user=Depends(require_user),
):
    """Progress of an onboarding run (status + new lines since ``cursor``).
    Read access for any authenticated role — the job log itself never
    contains the password (see services/host_onboarding.py's security
    tests), so there is nothing here a viewer shouldn't see.
    """
    return await host_onboarding.read_log(job_id, cursor)


class LaunchCommandBody(BaseModel):
    """A registry entry plus the two things the operator chose (slug, port)."""

    model_config = {"protected_namespaces": ()}

    engine: str
    model_identifier: str
    slug: str = Field(min_length=1, max_length=64)
    port: int = Field(ge=1, le=65535)
    launch_template: str | None = None
    container_name: str | None = None
    image: str | None = None
    # ssh_process (PR 6): a host engine has a checkout and a weight directory
    # instead of an image, its own stop script instead of `docker stop`, and a
    # context budget that has to be passed at launch.
    stop_template: str | None = None
    src_dir: str | None = Field(default=None, max_length=512)
    gguf_dir: str | None = Field(default=None, max_length=512)
    ctx: int | None = Field(default=None, ge=0)
    # Recipe tuning (PR 8). Only the compose templates consume it (via
    # ``{env_yaml}``); for every other template it is inert.
    env: dict[str, str] | None = None


@router.post("/launch-command")
async def preview_launch_command(
    body: LaunchCommandBody,
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Render a registry entry into the ``launch_command`` for a new runtime.

    Pure function behind an endpoint — nothing is created, started or written.
    It exists so the renderer lives in exactly one place: the wizard shows the
    command it is about to store, then posts that same string to the existing
    ``POST /runtimes``. Reimplementing the templating in TypeScript would mean
    two renderers drifting apart, with the shell command as the casualty.

    A bad template is a 400 with the reason (unknown placeholder, missing
    ``mc.runtime.slug`` label, unsupported engine) — all operator-fixable.

    ``stop_command`` comes back rendered too when the entry ships a
    ``stop_template`` (ssh_process). It is stored on the runtime row, so it
    must go through the same renderer as the launch command rather than being
    assembled in the browser.
    """
    try:
        command = launch_template.build_launch_command(
            engine=body.engine,
            model_identifier=body.model_identifier,
            slug=body.slug,
            port=body.port,
            launch_template=body.launch_template,
            container_name=body.container_name,
            image=body.image,
            src_dir=body.src_dir,
            gguf_dir=body.gguf_dir,
            ctx=body.ctx,
            env=body.env,
        )
        stop_command = (
            launch_template.render_launch_template(
                body.stop_template,
                {
                    "port": body.port,
                    "model": body.model_identifier,
                    "slug": body.slug,
                    "container_name": body.container_name or f"mc-{body.slug}",
                    "image": body.image or "-",
                    "src_dir": body.src_dir or launch_template.DEFAULT_SRC_DIR,
                    "gguf_dir": body.gguf_dir or launch_template.DEFAULT_GGUF_DIR,
                    "ctx": body.ctx or 0,
                    "env_yaml": launch_template.render_compose_env(body.env),
                },
            )
            if body.stop_template
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"launch_command": command, "stop_command": stop_command}


@router.post("/{host_id}/bootstrap", status_code=202)
async def bootstrap_host(
    host_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Start the idempotent bootstrap run (docker + nvidia toolkit + group).

    Returns immediately; progress is polled from
    ``GET /{host_id}/bootstrap/log``. 409 while a run for this host is still
    going — two concurrent apt runs on one box is how a dpkg lock deadlock
    starts.
    """
    host = await _get_host(session, host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")
    _require_ssh_capable(host, "Bootstrap")

    current = await host_bootstrap.get_status(str(host.id))
    if current and current.get("status") == host_bootstrap.STATUS_RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"Für Host '{host.slug}' läuft bereits ein Bootstrap.",
        )

    await host_bootstrap.start_bootstrap(str(host.id), resolved_host_from_row(host))
    return {"status": "started", "host_id": str(host.id)}


@router.get("/{host_id}/bootstrap/log")
async def bootstrap_log(
    host_id: str,
    cursor: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Bootstrap progress since ``cursor`` (status + new lines in one read).

    ``status`` is ``idle`` when no run was ever started for this host, or the
    1h TTL has expired.
    """
    host = await _get_host(session, host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")
    return await host_bootstrap.read_log(str(host.id), cursor)


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
    return _serialize_host(host, current_user)


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
    - agent     → node-agent push telemetry if <60s fresh (routers/nodes.py),
      otherwise the same SSH probe as above (falls back byte-identically)
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
    return {"kind": host.kind, "slug": host.slug, **metrics}
