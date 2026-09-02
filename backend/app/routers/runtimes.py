"""
Runtimes API — start/stop/restart/status for local model runtimes.
"""

import json as _json
import logging
import re as _re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user, require_role, Role
from app.config import settings
from app.database import get_session
from app.models.agent import Agent
from app.models.host import Host
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost, RUNTIME_HOST_ROLES
from app.redis_client import RedisKeys, get_redis
from app.services import recipe_switcher, runtime_manager, runtime_readiness, runtime_naming
from app.services.agent_runtime_switch import (
    _PROBEABLE_RUNTIME_TYPES,
    probe_runtime_model,
)
from app.services.endpoint_probe import probe_endpoint_url
from app.services.host_resolver import (
    ResolvedHost,
    resolve_host_by_slug,
    resolve_host_for_runtime,
)
from app.services.runtime_manager import add_lmstudio_runtime
from app.services.runtime_model_resolver import invalidate_cached_model
from app.services.runtime_propagation import mark_agents_for_sync, sync_pending_agents
from app.services import activity
from app.services.runtime_autostart import (
    AutostartHostUnreachable,
    get_autostart_status,
    set_autostart,
)

_AUTOSTART_FLAG_PATH_PATTERN = _re.compile(r"^/[\w./\-]{1,511}$")


def _validate_autostart_flag_path(v: str | None) -> str | None:
    """Must be an absolute path with only safe characters (ADR-057) — this
    string is shell-quoted before use, but rejecting anything exotic up front
    keeps the operator honest and avoids surprising remote-path bugs."""
    if v is not None and not _AUTOSTART_FLAG_PATH_PATTERN.match(v):
        raise ValueError(
            "autostart_flag_path muss ein absoluter Pfad sein "
            "(nur Buchstaben, Ziffern, '.', '_', '-', '/')"
        )
    return v

logger = logging.getLogger("mc.runtimes")

router = APIRouter(prefix="/api/v1/runtimes", tags=["runtimes"])


async def _resolve_runtime_dict(
    session: AsyncSession, runtime_id: str
) -> dict | None:
    """Slug-or-UUID DB lookup → model_dump() dict for runtime_manager.* calls.

    Phase 16 (ADR-028) makes the registry DB-only. start/stop/restart/health
    still used the old `runtime_manager.get_runtime()` (JSON lookup), which
    404'd on a UUID from the DB (e.g. nemotron-super had a slug in the JSON
    but a UUID in the DB). This helper mirrors the same pattern as the
    GET /{runtime_id} endpoint.
    """
    rt = (await session.exec(select(Runtime).where(Runtime.slug == runtime_id))).first()
    if not rt:
        try:
            rt_uuid = uuid.UUID(runtime_id)
        except ValueError:
            rt_uuid = None
        if rt_uuid is not None:
            rt = await session.get(Runtime, rt_uuid)
    return rt.model_dump() if rt else None


async def _resolve_runtime_and_host(
    session: AsyncSession, runtime_id: str
) -> tuple[dict | None, ResolvedHost | None]:
    """Like _resolve_runtime_dict, but includes the resolved host (ADR-048).

    Lifecycle endpoints pass the host through to runtime_manager so
    SSH/control ops run on the box of the respective runtime — no longer
    implicitly on settings.dgx_ssh_host.
    """
    rt = (await session.exec(select(Runtime).where(Runtime.slug == runtime_id))).first()
    if not rt:
        try:
            rt_uuid = uuid.UUID(runtime_id)
        except ValueError:
            rt_uuid = None
        if rt_uuid is not None:
            rt = await session.get(Runtime, rt_uuid)
    if not rt:
        return None, None
    host = await resolve_host_for_runtime(session, rt)
    return rt.model_dump(), host


def _host_ref(host: ResolvedHost | None) -> dict | None:
    """Compact host reference {id, slug, display_name} for runtime payloads
    (ADR-048). Only real registry bindings (runtime.host_id) count — legacy
    string and settings fallback return None (UI shows no host chip)."""
    if host is None or host.source != "registry":
        return None
    return {
        "id": str(host.host_id),
        "slug": host.slug,
        "display_name": host.display_name,
    }


# Runtime types that are ALWAYS a remote API call, regardless of host binding
# (Verbund-UI Phase 0, 30.08.2026). Deliberately narrow: "hermes" and "omp"
# look cloud-ish by name but are curated LOCAL runtime_types (see
# runtime_naming.CURATED_RUNTIME_TYPES — a self-hosted bridge process on a
# specific box), and "openai_compatible" is genuinely ambiguous (compose_
# renderer.py notes it can be a local container OR a cloud-hosted endpoint
# like a remote Ollama) — for that one, host resolution below is the only
# signal. "anthropic*" runtime_types (e.g. "anthropic_oauth") are matched by
# prefix, same rule harness_compat.runtime_protocol() already uses.
CLOUD_RUNTIME_TYPES: frozenset[str] = frozenset({"cloud", "grok", "kimi"})


def _runtime_locality(runtime: Runtime, host: ResolvedHost | None) -> str:
    """"local" | "cloud" — can a host-inplace agent (which can only ever run
    something physically ON its own box) even reach this runtime?

    A real registry host binding (_host_ref returns non-None) always means
    local, whatever the runtime_type. Otherwise, a handful of types are
    inherently remote (CLOUD_RUNTIME_TYPES / an "anthropic*" type). Anything
    else defaults to local: under-filtering (a stray cloud row slipping
    through) is far less harmful for a picker than over-filtering (hiding a
    legitimate local candidate) while host_id coverage across the fleet is
    still incomplete (many local runtimes still resolve via the legacy
    string/settings fallback, not a registry row).
    """
    if _host_ref(host) is not None:
        return "local"
    rt = (runtime.runtime_type or "").strip()
    if rt in CLOUD_RUNTIME_TYPES or rt.startswith("anthropic"):
        return "cloud"
    return "local"


# ── DB-backed runtime CRUD ───────────────────────────────────────────────────


class RuntimeCreate(BaseModel):
    """Generic runtime creation — supersedes LMS-specific AddLMStudioRuntimeBody."""
    slug: str
    display_name: str
    # lmstudio | vllm_docker | llamacpp_docker | unsloth | openai_compatible | cloud
    runtime_type: str
    endpoint: str
    healthcheck_path: str | None = "/v1/models"
    model_identifier: str | None = None
    container_name: str | None = None
    lms_identifier: str | None = None
    lms_cli_path: str | None = None
    launch_command: str | None = None
    # ssh_process (PR 6): the process handle and its own stop script.
    stop_command: str | None = None
    process_name: str | None = None
    # "needs the whole box" — see models/runtime.exclusive_memory. NOT the same
    # flag as single_instance (that one limits agent bindings).
    exclusive_memory: bool = False
    host: str | None = None  # DEPRECATED legacy string — registry binding via host_id
    host_id: uuid.UUID | None = None  # Host registry binding (ADR-048)
    api_key_secret_id: uuid.UUID | None = None  # ADR-056: openai-protocol runtime API key
    control_url: str | None = None  # power_managed: Flask :5555 control plane
    wol_mac_address: str | None = None  # power_managed: Wake-on-LAN target MAC
    power_managed: bool = False
    role_tags: list[str] = []
    supports_tools: bool = False
    supports_reasoning: bool = False
    supports_streaming: bool = True
    preferred_context_len: int | None = None
    max_context_len: int | None = None
    gpu_profile: str | None = None
    memory_notes: str | None = None
    startup_notes: str | None = None
    ui_order: int = 999
    enabled: bool = True
    autostart_supported: bool = False  # ADR-057: Engine Control v0
    autostart_flag_path: str | None = None
    # Rezept-Umschalter (02.09.2026): {"nodes": 1|2, …} — vom Katalog kopiert,
    # wenn die Instanz über /hosts/{id}/recipes/{slug}/start entsteht.
    topology: dict | None = None

    @field_validator("control_url")
    @classmethod
    def _validate_control_url_create(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("control_url muss mit http:// oder https:// beginnen")
        return v

    @field_validator("autostart_flag_path")
    @classmethod
    def _validate_autostart_flag_path_create(cls, v: str | None) -> str | None:
        return _validate_autostart_flag_path(v)


class RuntimeUpdate(BaseModel):
    display_name: str | None = None
    runtime_type: str | None = None
    endpoint: str | None = None
    healthcheck_path: str | None = None
    model_identifier: str | None = None
    container_name: str | None = None
    lms_identifier: str | None = None
    lms_cli_path: str | None = None
    launch_command: str | None = None
    stop_command: str | None = None
    process_name: str | None = None
    exclusive_memory: bool | None = None
    host: str | None = None  # DEPRECATED legacy string — registry binding via host_id
    # Host registry binding (ADR-048). PATCH uses exclude_none, so host_id
    # is handled separately in the endpoint via model_fields_set: only this
    # way is explicit host_id=null (unbind — prerequisite for the host
    # delete guard in routers/hosts.py) distinguishable from omission.
    host_id: uuid.UUID | None = None
    # api_key_secret_id (ADR-056) mirrors the host_id special-case: PATCH must be
    # able to both set and clear (unbind) it, so it's handled via model_fields_set
    # in update_runtime_db rather than exclude_none.
    api_key_secret_id: uuid.UUID | None = None
    control_url: str | None = None
    wol_mac_address: str | None = None
    power_managed: bool | None = None
    role_tags: list[str] | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    supports_streaming: bool | None = None
    preferred_context_len: int | None = None
    max_context_len: int | None = None
    gpu_profile: str | None = None
    memory_notes: str | None = None
    startup_notes: str | None = None
    ui_order: int | None = None
    enabled: bool | None = None
    autostart_supported: bool | None = None  # ADR-057: Engine Control v0
    autostart_flag_path: str | None = None
    topology: dict | None = None

    @field_validator("control_url")
    @classmethod
    def _validate_control_url_update(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("control_url muss mit http:// oder https:// beginnen")
        return v

    @field_validator("autostart_flag_path")
    @classmethod
    def _validate_autostart_flag_path_update(cls, v: str | None) -> str | None:
        return _validate_autostart_flag_path(v)


_MODEL_ID_PATTERN = _re.compile(r'^[\w.\-/]{1,200}$')


class LMStudioModelAction(BaseModel):
    model_id: str
    quantization: str | None = None
    context_length: int | None = None

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, v: str) -> str:
        if not _MODEL_ID_PATTERN.match(v):
            raise ValueError(
                "model_id darf nur alphanumerische Zeichen, '.', '-', '_', '/' enthalten (max. 200 Zeichen)"
            )
        return v


@router.get("/lmstudio/models")
async def list_lmstudio_models(current_user=Depends(require_user)):
    """Returns all LLM models installed in LM Studio."""
    models = await runtime_manager.list_lms_models()
    return {"models": models, "reachable": True}


@router.post("/lmstudio/load")
async def load_lmstudio_model(body: LMStudioModelAction, current_user=Depends(require_user)):
    """Loads a model in LM Studio (lms load)."""
    rt = {
        "id": body.model_id,
        "display_name": body.model_id,
        "runtime_type": "lmstudio",
        "lms_identifier": body.model_id,
        "lms_cli_path": "~/.lmstudio/bin/lms",
        "context_length": body.context_length,
    }
    result = await runtime_manager.start_runtime(rt)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/lmstudio/unload")
async def unload_lmstudio_model(body: LMStudioModelAction, current_user=Depends(require_user)):
    """Unloads a model from LM Studio (lms unload)."""
    rt = {
        "id": body.model_id,
        "display_name": body.model_id,
        "runtime_type": "lmstudio",
        "lms_identifier": body.model_id,
        "lms_cli_path": "~/.lmstudio/bin/lms",
    }
    result = await runtime_manager.stop_runtime(rt)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/lmstudio/kv-reset")
async def trigger_kv_reset(current_user=Depends(require_user)):
    """Performs a manual KV reset: remember active models → unload all → reload."""
    import asyncio
    loaded = await runtime_manager.lms_get_loaded_models()
    if not loaded:
        return {"ok": True, "message": "Keine Modelle geladen — nichts zu tun.", "reloaded": []}
    unload = await runtime_manager.lms_unload_all()
    if not unload["ok"]:
        raise HTTPException(status_code=400, detail=f"Unload fehlgeschlagen: {unload['message']}")
    await asyncio.sleep(3)
    errors = []
    for model_id in loaded:
        result = await runtime_manager.lms_load_by_id(model_id)
        if not result["ok"]:
            errors.append(model_id)
    if errors:
        raise HTTPException(status_code=500, detail=f"Reload fehlgeschlagen für: {', '.join(errors)}")
    return {"ok": True, "message": f"{len(loaded)} Modell(e) neu geladen.", "reloaded": loaded}


@router.post("/lmstudio/download")
async def download_lmstudio_model(body: LMStudioModelAction, current_user=Depends(require_user)):
    """Starts a model download via lms get (background)."""
    result = await runtime_manager.lms_download_model(body.model_id, body.quantization)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/lmstudio/delete")
async def delete_lmstudio_model(body: LMStudioModelAction, current_user=Depends(require_user)):
    """Deletes a model from LM Studio (lms rm)."""
    result = await runtime_manager.lms_delete_model(body.model_id)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


class HFDownloadAction(BaseModel):
    repo_id: str
    filename: str

    @field_validator("repo_id", "filename")
    @classmethod
    def validate_ids(cls, v: str) -> str:
        if not _MODEL_ID_PATTERN.match(v):
            raise ValueError(
                "Ungültige ID — nur alphanumerische Zeichen, '.', '-', '_', '/' erlaubt (max. 200 Zeichen)"
            )
        return v


@router.get("/lmstudio/downloads")
async def list_active_downloads(current_user=Depends(require_user)):
    """Returns active downloads (lms get + HF curl)."""
    downloads = await runtime_manager.get_active_downloads()
    return {"downloads": downloads}


class CancelDownloadBody(BaseModel):
    model_name: str


@router.post("/lmstudio/downloads/cancel")
async def cancel_download(body: CancelDownloadBody, current_user=Depends(require_user)):
    """Cancels a running download."""
    result = await runtime_manager.cancel_download(body.model_name)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/lmstudio/catalog/search")
async def search_lmstudio_catalog(q: str = "", current_user=Depends(require_user)):
    """Searches for models in the LM Studio catalog (lmstudio-community on HuggingFace)."""
    models = await runtime_manager.search_lmstudio_catalog(q)
    return {"models": models}


@router.get("/lmstudio/hf/files")
async def get_hf_repo_files(repo: str, current_user=Depends(require_user)):
    """Returns all GGUF files of a HuggingFace repo."""
    if not _MODEL_ID_PATTERN.match(repo):
        raise HTTPException(status_code=400, detail="Ungültige Repo-ID")
    return await runtime_manager.get_hf_repo_files(repo)


@router.post("/lmstudio/download-hf")
async def download_hf_file(body: HFDownloadAction, current_user=Depends(require_user)):
    """Starts a download of a GGUF file from HuggingFace onto the DGX Spark."""
    result = await runtime_manager.download_hf_file(body.repo_id, body.filename)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/spark/metrics")
async def spark_metrics(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Live hardware metrics of the DGX Spark (GPU, VRAM, RAM, temp).

    Back-compat alias (ADR-048): delegates to the registry host with slug
    `dgx-spark` (created by the host_seeder from settings.dgx_ssh_*).
    Static path — must stay defined BEFORE the /{runtime_id} routes.
    """
    host = await resolve_host_by_slug(session, "dgx-spark")
    if host is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Kein Host mit slug 'dgx-spark' registriert — Spark-Metrics "
                "laufen jetzt über die Host-Registry. Host unter /api/v1/hosts "
                "anlegen (oder DGX_SSH_HOST setzen, der Seeder legt ihn an) und "
                "GET /api/v1/hosts/{id}/metrics nutzen."
            ),
        )
    return await runtime_manager.get_host_metrics(host)


@router.get("/vllm/discover")
async def discover_vllm_containers(current_user=Depends(require_user)):
    """Lists running vLLM containers on the DGX (with is_registered flag)."""
    containers = await runtime_manager.list_vllm_containers()
    return {"containers": containers}


class AddVllmRuntimeBody(BaseModel):
    container_name: str
    display_name: str
    endpoint: str
    role_tags: list[str] = []

    @field_validator("container_name")
    @classmethod
    def validate_container_name(cls, v: str) -> str:
        if not _MODEL_ID_PATTERN.match(v):
            raise ValueError(
                "container_name darf nur alphanumerische Zeichen, '.', '-', '_', '/' enthalten (max. 200 Zeichen)"
            )
        return v


@router.post("/vllm")
async def create_vllm_runtime(body: AddVllmRuntimeBody, current_user=Depends(require_user)):
    """Adds a new vLLM Docker runtime to runtimes.json."""
    new_rt = runtime_manager.add_vllm_runtime(
        container_name=body.container_name,
        display_name=body.display_name,
        endpoint=body.endpoint,
        role_tags=body.role_tags,
    )
    state_info = await runtime_manager.get_runtime_state(new_rt)
    return {**new_rt, **state_info}


class AddLMStudioRuntimeBody(BaseModel):
    lms_identifier: str
    display_name: str
    endpoint: str = "http://192.0.2.10:1234/v1"


@router.post("")
async def create_lmstudio_runtime(body: AddLMStudioRuntimeBody, current_user=Depends(require_user)):
    """Adds a new LM Studio runtime to runtimes.json."""
    new_rt = add_lmstudio_runtime(
        lms_identifier=body.lms_identifier,
        display_name=body.display_name,
        endpoint=body.endpoint,
    )
    state_info = await runtime_manager.get_runtime_state(new_rt)
    return {**new_rt, **state_info}


def _grouped_sort_key(rt: dict) -> tuple:
    """Group runtimes by provider, then order within the group.

    Sorting by ``ui_order`` alone scattered the list: catalogue-bound rows are
    created with the default 999 and landed in a heap at the end, so the two
    Anthropic models sat apart and the Ollama ones did too (operator report,
    2026-07-31). Provider membership is derived from the endpoint — the same
    rule runtime_naming/model_catalog use — so a newly bound model files itself
    next to its siblings without anyone maintaining ui_order.

    Unknown providers (local vLLM, LM Studio, unsloth) keep their curated
    ui_order and sort after the known cloud providers, which is where they sat
    before. display_name breaks ties so bound siblings have a stable order
    instead of depending on insertion time.
    """
    provider = runtime_naming.resolve_provider(rt.get("endpoint"))
    if provider is not None:
        return (0, provider.label, rt.get("ui_order") or 999, rt.get("display_name") or "")
    return (1, "", rt.get("ui_order") or 999, rt.get("display_name") or "")


@router.get("")
async def list_runtimes(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Returns all enabled runtimes with their current state.

    Phase 16 (D-01/D-03): Reads exclusively from the DB table `runtimes`.
    The JSON file `backend/config/runtimes.json` is now only a bootstrap seed.
    """
    runtimes = await runtime_manager.list_db_runtimes(session)

    # Verbund-UI Phase 1b (30.08.2026): member hosts of a multi-node runtime
    # (runtime_hosts), batched in one query rather than per-row — the
    # overwhelming majority of runtimes have zero rows here (solo), so this
    # is empty/cheap in the common case and avoids an N+1 for the rest.
    member_hosts_by_runtime: dict[uuid.UUID, list[dict]] = {}
    runtime_ids = [rt.id for rt in runtimes]
    if runtime_ids:
        membership_rows = (
            await session.execute(
                select(RuntimeHost, Host)
                .join(Host, RuntimeHost.host_id == Host.id)
                .where(RuntimeHost.runtime_id.in_(runtime_ids))
                .order_by(RuntimeHost.node_rank)
            )
        ).all()
        runtime_head_ids = {rt.id: rt.host_id for rt in runtimes}
        for rh, member_host in membership_rows:
            # Defensiv: der Head steht in runtimes.host_id und darf NIE zusätzlich
            # als member_host auftauchen (Zusage in Doku + Model). Unbekannte Rollen
            # ebenfalls überspringen — die Frontend-Types deklarieren head|worker hart.
            if runtime_head_ids.get(rh.runtime_id) == rh.host_id:
                continue
            if rh.role not in RUNTIME_HOST_ROLES:
                continue
            member_hosts_by_runtime.setdefault(rh.runtime_id, []).append({
                "host_id": str(member_host.id),
                "slug": member_host.slug,
                "display_name": member_host.display_name,
                "role": rh.role,
                "node_rank": rh.node_rank,
            })

    result = []
    for rt in runtimes:
        if not rt.enabled:
            continue
        # Pitfall 1 (RESEARCH.md): get_runtime_state expects a dict.
        rt_dict = rt.model_dump()
        host = await resolve_host_for_runtime(session, rt)
        state_info = await runtime_manager.get_runtime_state(rt_dict, host=host)
        # ADR-048: `host` in the payload = {id, slug, display_name} | null.
        # Deliberately overwrites the DEPRECATED legacy string field of the
        # same name from model_dump() — frontend type is `host?: HostRef | null`.
        provider = runtime_naming.resolve_provider(rt.endpoint)
        result.append({
            **rt_dict,
            **state_info,
            "host": _host_ref(host),
            # Group label for the UI, derived from the SAME rule the sort and
            # the catalog use. Sent from here so the picker does not re-derive
            # provider membership client-side — that split is what let the
            # frontend and backend disagree about switchability before.
            # None = no recognised vendor (local vLLM, LM Studio, unsloth).
            "provider_label": provider.label if provider else None,
            # Version numbers in the display name that the served model does
            # NOT back — empty list means the name is honest. See
            # _display_name_drift below for why this ships on every row.
            "display_name_drift": runtime_naming.display_name_drift(
                rt.display_name, rt.model_identifier
            ),
            # Verbund-UI Phase 0 (30.08.2026): "local" | "cloud" — lets the
            # agent detail page's runtime picker filter cloud candidates out
            # for host-inplace agents, which can only ever run something
            # physically on their own box.
            "locality": _runtime_locality(rt, host),
            # Verbund-UI Phase 1b (30.08.2026): additional hosts this runtime
            # spans (a multi-node verbund's workers) — [] for every solo
            # runtime, which today is all of them. The runtime's OWN host_id
            # (the head) is NOT duplicated in here, it stays in "host" above.
            "member_hosts": member_hosts_by_runtime.get(rt.id, []),
        })
    result.sort(key=_grouped_sort_key)
    return {"runtimes": result}


@router.get("/live-status")
async def runtimes_live_status(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Watcher-fed live view: what each probeable runtime ACTUALLY serves."""
    result = await session.exec(
        select(Runtime).where(Runtime.enabled.is_(True))
    )
    redis = await get_redis()
    live: dict = {}
    for rt in result.all():
        raw = await redis.get(RedisKeys.runtime_live(rt.slug))
        if raw is None:
            continue
        data = _json.loads(raw)
        served = data.get("served_model")
        data["drift"] = bool(served) and served != (rt.model_identifier or "")
        # Same statement for the context window: what the engine serves vs what
        # the row (and therefore the rendered agent env) still says. Both flags
        # describe the window BEFORE the watcher's two-probe confirmation, so
        # the cockpit can show a pending change the DB does not know about yet.
        served_ctx = data.get("served_context_len")
        data["context_drift"] = bool(served_ctx) and served_ctx != rt.max_context_len
        live[rt.slug] = data
    return {
        "live": live,
        "watcher_enabled": settings.runtime_watcher_enabled,
        "interval": settings.runtime_watcher_interval,
    }


@router.get("/compat-matrix")
async def get_compat_matrix(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Harness x Provider matrix for the switch UI (ADR-056).

    `harnesses` is the cli-bridge-scoped list (HARNESSES). `host_harnesses` is
    the HostHarnessAdapter registry, added so the agent wizard stops keeping
    its own copy of it: that copy omitted `claude` entirely and assumed every
    host harness is a singleton bridge, which would have blocked creating a
    second claude host agent even though the backend allows it.
    """
    from app.services.harness_compat import (
        HARNESSES, HARNESS_LABELS, incompat_reason, is_compatible, runtime_protocol,
    )
    from app.services.host_harness_adapter import host_harness_catalog
    rows = (await session.execute(select(Runtime).where(Runtime.enabled == True))).scalars().all()  # noqa: E712
    runtimes = []
    for rt in rows:
        compatible = [h for h in HARNESSES if is_compatible(h, rt)]
        reasons = {h: incompat_reason(h, rt) for h in HARNESSES if h not in compatible}
        # Verbund-UI Phase 0 (30.08.2026) — same host resolution/locality
        # rule as GET /runtimes, so any future host-inplace consumer of this
        # matrix (RuntimeSwitchModal, the agent wizard's RuntimeStep) can
        # apply the same filter without re-deriving it.
        host = await resolve_host_for_runtime(session, rt)
        runtimes.append({
            "slug": rt.slug,
            "display_name": rt.display_name,
            "protocol": runtime_protocol(rt),
            "compatible_harnesses": compatible,
            "reasons": reasons,
            "locality": _runtime_locality(rt, host),
        })
    return {
        "harnesses": [{"key": h, "label": HARNESS_LABELS[h]} for h in HARNESSES],
        "host_harnesses": host_harness_catalog(),
        "runtimes": runtimes,
    }


class ProbeEndpointBody(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


@router.post("/probe-endpoint")
async def probe_endpoint(
    body: ProbeEndpointBody, current_user=Depends(require_user)
):
    """Probe an arbitrary base URL (no runtime row required) — the
    add-runtime wizard's engine/model auto-detection."""
    return await probe_endpoint_url(body.url)


@router.get("/{runtime_id}")
async def get_runtime(
    runtime_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Returns a single runtime from the DB (slug or UUID)."""
    rt = (await session.exec(select(Runtime).where(Runtime.slug == runtime_id))).first()
    if not rt:
        try:
            rt_uuid = uuid.UUID(runtime_id)
        except ValueError:
            rt_uuid = None
        if rt_uuid is not None:
            rt = await session.get(Runtime, rt_uuid)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")
    rt_dict = rt.model_dump()
    host = await resolve_host_for_runtime(session, rt)
    state_info = await runtime_manager.get_runtime_state(rt_dict, host=host)
    # Same host reference as GET /runtimes (list) — one frontend type.
    return {**rt_dict, **state_info, "host": _host_ref(host)}


@router.get("/{runtime_id}/health")
async def runtime_health(
    runtime_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Live health probe for a runtime."""
    rt, host = await _resolve_runtime_and_host(session, runtime_id)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")
    state_info = await runtime_manager.get_runtime_state(rt, host=host)
    return {"runtime_id": runtime_id, **state_info}


class StartRuntimeBody(BaseModel):
    context_length: int | None = None


@router.post("/{runtime_id}/start")
async def start_runtime(
    runtime_id: str,
    body: StartRuntimeBody = StartRuntimeBody(),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Starts a runtime."""
    rt, host = await _resolve_runtime_and_host(session, runtime_id)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")
    if body.context_length:
        rt = {**rt, "context_length": body.context_length}
    result = await runtime_manager.start_runtime(rt, host=host)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    await runtime_readiness.invalidate_readiness(rt.get("slug") or runtime_id)
    return result


@router.post("/{runtime_id}/stop")
async def stop_runtime(
    runtime_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Stops a runtime (docker engines: vllm_docker / llamacpp_docker)."""
    rt, host = await _resolve_runtime_and_host(session, runtime_id)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")
    result = await runtime_manager.stop_runtime(rt, host=host)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    await runtime_readiness.invalidate_readiness(rt.get("slug") or runtime_id)
    return result


@router.post("/{runtime_id}/restart")
async def restart_runtime(
    runtime_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Restarts a runtime (docker engines: vllm_docker / llamacpp_docker)."""
    rt, host = await _resolve_runtime_and_host(session, runtime_id)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")
    result = await runtime_manager.restart_runtime(rt, host=host)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    await runtime_readiness.invalidate_readiness(rt.get("slug") or runtime_id)
    return result


@router.post("/{runtime_id}/wake")
async def wake_runtime(
    runtime_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Wake a power_managed runtime's host via Wake-on-LAN (e.g. PORSCHE).

    Drops a trigger file for the host-side launchd watcher (the Docker backend
    cannot send an L2 broadcast). Only valid for runtimes with power_managed=true.
    """
    rt, host = await _resolve_runtime_and_host(session, runtime_id)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")
    result = await runtime_manager.wake_runtime(rt, host=host)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    # Box is waking → drop any stale "asleep" readiness cache so the next poll re-probes.
    await runtime_readiness.invalidate_readiness(rt.get("slug") or runtime_id)
    return result


@router.post("/{runtime_id}/probe-model")
async def probe_model_endpoint(
    runtime_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Probes the `/v1/models` endpoint of an OpenAI-compatible runtime and
    persists the result in `runtimes.model_identifier`.

    Phase 16 (D-18/D-19/D-21): Re-uses Phase-15 `probe_runtime_model` helper.
    Idempotent — a second call with an identical probe result returns
    `changed=false` and does not write.
    """
    # Slug-or-UUID lookup (pattern from GET /{runtime_id})
    rt = (await session.exec(select(Runtime).where(Runtime.slug == runtime_id))).first()
    if not rt:
        try:
            rt_uuid = uuid.UUID(runtime_id)
        except ValueError:
            rt_uuid = None
        if rt_uuid is not None:
            rt = await session.get(Runtime, rt_uuid)
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")

    if rt.runtime_type not in _PROBEABLE_RUNTIME_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Runtime-Typ '{rt.runtime_type}' unterstuetzt kein Model-Probe. "
                f"Probeable: {sorted(_PROBEABLE_RUNTIME_TYPES)}."
            ),
        )

    old_model = rt.model_identifier
    probed = await probe_runtime_model(rt)
    new_model = probed if probed else old_model
    changed = bool(probed) and probed != old_model

    if changed:
        rt.model_identifier = probed
        rt.updated_at = datetime.utcnow()
        session.add(rt)
        await session.commit()
        await session.refresh(rt)

    return {
        "slug": rt.slug,
        "old_model_identifier": old_model,
        "new_model_identifier": new_model,
        "changed": changed,
    }


# ── DB-backed CRUD endpoints (for UI management) ─────────────────────────────
# These work against the `runtimes` table (Phase 1) and will become the
# source of truth once runtime_manager is fully refactored off the JSON seed.


async def _validate_host_id(session: AsyncSession, host_id: uuid.UUID) -> None:
    """422 if the host UUID doesn't point to a registry row (ADR-048).

    Without this check, SQLite (tests, no FK enforcement) would accept a
    dead binding that the resolver would then just log away."""
    from app.models.host import Host

    if await session.get(Host, host_id) is None:
        raise HTTPException(
            status_code=422,
            detail=f"host_id {host_id} zeigt auf keinen Host (GET /api/v1/hosts)",
        )


#: Felder, deren Änderung die Startbefehl-Pflicht neu bewerten muss. Ein
#: PATCH an ui_order o.ä. lässt eine alte Zeile ohne Befehl in Ruhe — sie
#: bleibt lesbar (Vertrag), nur aktivieren/binden/umtypen geht nicht.
_LAUNCH_COMMAND_RULE_FIELDS = frozenset({"launch_command", "enabled", "host_id", "runtime_type"})

LAUNCH_COMMAND_REQUIRED = (
    "Startbefehl fehlt — eine aktivierte Runtime mit Box braucht einen "
    "Startbefehl (launch_command), sonst kann MC sie nie starten."
)


def _require_launch_command(rt: Runtime) -> None:
    """Rezept-Umschalter (Vertrag 02.09.2026): launch_command ist Pflicht für
    enabled + host-gebunden — im Router, nicht als DB-NOT-NULL, damit Cloud-
    Runtimes ohne Box unberührt bleiben. Nur Engines, die MC über einen Befehl
    startet (recipe_switcher.COMMAND_DRIVEN_RUNTIME_TYPES): LM Studio lädt
    per ``lms load``, ein Pflichtfeld wäre dort eine Lüge."""
    if (
        rt.enabled
        and rt.host_id is not None
        and rt.runtime_type in recipe_switcher.COMMAND_DRIVEN_RUNTIME_TYPES
        and not (rt.launch_command or "").strip()
    ):
        raise HTTPException(status_code=422, detail=LAUNCH_COMMAND_REQUIRED)


async def _runtime_row_response(session: AsyncSession, rt: Runtime) -> dict:
    """CRUD response with the same host shape as GET /runtimes (HostRef|null).

    Without this, POST/PATCH would return the DEPRECATED legacy string field
    `host`, while GET returns an object — one field name, two shapes.

    Also carries ``display_name_drift`` so a write that leaves a lying name
    behind says so in its own response — see the note on that field below.
    """
    host = await resolve_host_for_runtime(session, rt)
    return {
        **rt.model_dump(),
        "host": _host_ref(host),
        "display_name_drift": runtime_naming.display_name_drift(
            rt.display_name, rt.model_identifier
        ),
    }


@router.post("/db")
async def create_runtime_db(
    body: RuntimeCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Create a new runtime in the DB. Returns the saved row."""
    existing = (await session.exec(select(Runtime).where(Runtime.slug == body.slug))).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Runtime slug '{body.slug}' already exists")
    if body.host_id is not None:
        await _validate_host_id(session, body.host_id)
    rt = Runtime(**body.model_dump())
    _require_launch_command(rt)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return await _runtime_row_response(session, rt)


@router.patch("/db/{slug}")
async def update_runtime_db(
    slug: str,
    body: RuntimeUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Update fields on a DB-backed runtime.

    host_id (ADR-048) and api_key_secret_id (ADR-056) go through
    model_fields_set instead of exclude_none: an explicit null unbinds the
    runtime from the host/secret (prerequisite for DELETE /api/v1/hosts/{id},
    whose 409 guard only clears after unbind).
    """
    rt = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first()
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{slug}' not found")
    changes = body.model_dump(exclude_none=True)
    changes.pop("host_id", None)
    changes.pop("api_key_secret_id", None)
    # A manual model_identifier edit must propagate exactly like the watcher's
    # drift detection (ADR-054): flag bound cli-bridge agents for a re-sync and
    # emit runtime.model_changed. Non-probeable cloud runtimes (Anthropic) have
    # no watcher, so this PATCH is their only path to a fresh model.
    old_model = rt.model_identifier
    for k, v in changes.items():
        setattr(rt, k, v)
    if "host_id" in body.model_fields_set:
        if body.host_id is not None:
            await _validate_host_id(session, body.host_id)
        rt.host_id = body.host_id
    if "api_key_secret_id" in body.model_fields_set:
        rt.api_key_secret_id = body.api_key_secret_id
    if body.model_fields_set & _LAUNCH_COMMAND_RULE_FIELDS:
        _require_launch_command(rt)
    rt.updated_at = datetime.utcnow()
    session.add(rt)
    await session.commit()
    await session.refresh(rt)

    # Name-vs-model honesty (the display_name_drift helper from #183, until now
    # only exercised by the migration that introduced it). `qwen-general` was
    # called "Spark vLLM (Laguna/Qwen — switchable)" while it served
    # deepseek-v4-flash-0731-spark; the row was correct and the label was a
    # lie, which is worse than an obviously empty field because nobody
    # double-checks a name that looks specific.
    #
    # Deliberately a WARNING and not a 4xx: the operator may be renaming and
    # repointing in two steps, and a hard block would make the intermediate
    # state unreachable. The response carries the finding (see
    # _runtime_row_response) so the UI can say it out loud; this log line is
    # for the case where the write came from somewhere without a UI.
    if {"display_name", "model_identifier"} & set(changes):
        drift = runtime_naming.display_name_drift(rt.display_name, rt.model_identifier)
        if drift:
            logger.warning(
                "runtime %s: display_name %r claims version(s) %s that "
                "model_identifier %r does not back",
                rt.slug, rt.display_name, ", ".join(drift), rt.model_identifier,
            )

    model_changed = "model_identifier" in changes and rt.model_identifier != old_model
    if model_changed:
        await invalidate_cached_model(rt.slug)
        await activity.emit_event(
            session,
            "runtime.model_changed",
            f"{rt.slug}: {old_model or 'n/a'} → {rt.model_identifier}",
            severity="info",
            detail={
                "slug": rt.slug,
                "old_model": old_model,
                "new_model": rt.model_identifier,
                "source": "manual_edit",
            },
        )
        await mark_agents_for_sync(session, rt)

    return await _runtime_row_response(session, rt)


@router.delete("/db/{slug}", status_code=204)
async def delete_runtime_db(
    slug: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Delete a DB-backed runtime. Agents referencing it get runtime_id=NULL
    (ON DELETE SET NULL) and fall back to docker-compose env."""
    rt = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first()
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{slug}' not found")
    await session.delete(rt)
    await session.commit()
    return None


@router.get("/db/{slug}/agents")
async def runtime_db_agents(
    slug: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Return the agents currently assigned to this runtime.

    Powers the 'N Agents assigned' badge on the /runtimes page.
    """
    rt = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first()
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{slug}' not found")
    agents = (await session.exec(select(Agent).where(Agent.runtime_id == rt.id))).all()
    return {
        "runtime_slug": rt.slug,
        "count": len(agents),
        "agents": [
            {
                "id": str(a.id),
                "name": a.name,
                "agent_runtime": a.agent_runtime,
                "pending_runtime_sync": a.pending_runtime_sync,
            }
            for a in agents
        ],
    }


@router.post("/db/{slug}/sync-agents")
async def force_sync_runtime_agents(
    slug: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Force the pending-model sync for this runtime's flagged agents NOW —
    including busy ones (their in-flight task will be interrupted).

    Scoped to this runtime's agents only (runtime_id filter) — without it a
    force-sync on one runtime would restart every pending agent fleet-wide,
    including busy agents bound to unrelated runtimes.
    """
    rt = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first()
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{slug}' not found")
    await sync_pending_agents(session, force=True, runtime_id=rt.id)
    return {"synced": True}


# ── Engine Control v0: Autostart Toggle (ADR-057) ────────────────────────────


async def _load_autostart_runtime(session: AsyncSession, slug: str) -> Runtime:
    rt = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first()
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{slug}' not found")
    if not rt.autostart_supported:
        raise HTTPException(
            status_code=422,
            detail=f"Runtime '{slug}' unterstützt kein Autostart-Flag "
            f"(autostart_supported=false — PATCH /runtimes/db/{slug} zum Aktivieren)",
        )
    if not rt.autostart_flag_path:
        raise HTTPException(
            status_code=422,
            detail=f"Runtime '{slug}' hat keinen autostart_flag_path konfiguriert "
            f"(PATCH /runtimes/db/{slug})",
        )
    return rt


@router.get("/db/{slug}/autostart")
async def get_runtime_autostart(
    slug: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Live status of the autostart flag file on the runtime's bound host.

    On-demand, not part of the 90s watcher tick (ADR-054) — this touches SSH
    and should only run when someone looks at the /runtimes page or hits the
    button, not on a fleet-wide timer.
    """
    rt = await _load_autostart_runtime(session, slug)
    host = await resolve_host_for_runtime(session, rt)
    status = await get_autostart_status(rt.autostart_flag_path, host=host)
    return {
        "slug": rt.slug,
        "flag_path": rt.autostart_flag_path,
        "enabled": status.enabled,
        "reachable": status.reachable,
    }


class SetAutostartBody(BaseModel):
    enabled: bool


@router.post("/db/{slug}/autostart")
async def post_runtime_autostart(
    slug: str,
    body: SetAutostartBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Touches/removes the autostart flag file over SSH, then reads it back
    to confirm — never trusts the write blindly, never leaks a stack trace
    into the UI when the host is unreachable."""
    rt = await _load_autostart_runtime(session, slug)
    host = await resolve_host_for_runtime(session, rt)
    try:
        status = await set_autostart(rt.autostart_flag_path, body.enabled, host=host)
    except AutostartHostUnreachable:
        raise HTTPException(
            status_code=502,
            detail="Host nicht erreichbar — Autostart-Status unbekannt",
        )
    await activity.emit_event(
        session,
        "runtime.autostart_changed",
        f"{rt.slug}: Autostart {'aktiviert' if body.enabled else 'deaktiviert'}"
        + ("" if status.enabled == body.enabled else " (Verifikation fehlgeschlagen)"),
        severity="info" if status.enabled == body.enabled else "warning",
        detail={
            "slug": rt.slug,
            "requested_enabled": body.enabled,
            "confirmed_enabled": status.enabled,
            "flag_path": rt.autostart_flag_path,
        },
    )
    return {
        "slug": rt.slug,
        "flag_path": rt.autostart_flag_path,
        "enabled": status.enabled,
        "reachable": status.reachable,
    }
