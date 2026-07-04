"""
Runtimes API — start/stop/restart/status for local model runtimes.
"""

import json as _json
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
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services import runtime_manager, runtime_readiness
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
from app.services.runtime_propagation import sync_pending_agents

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


# ── DB-backed runtime CRUD ───────────────────────────────────────────────────


class RuntimeCreate(BaseModel):
    """Generic runtime creation — supersedes LMS-specific AddLMStudioRuntimeBody."""
    slug: str
    display_name: str
    runtime_type: str  # lmstudio | vllm_docker | unsloth | openai_compatible | cloud
    endpoint: str
    healthcheck_path: str | None = "/v1/models"
    model_identifier: str | None = None
    container_name: str | None = None
    lms_identifier: str | None = None
    lms_cli_path: str | None = None
    launch_command: str | None = None
    host: str | None = None  # DEPRECATED legacy string — registry binding via host_id
    host_id: uuid.UUID | None = None  # Host registry binding (ADR-048)
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

    @field_validator("control_url")
    @classmethod
    def _validate_control_url_create(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("control_url muss mit http:// oder https:// beginnen")
        return v


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
    host: str | None = None  # DEPRECATED legacy string — registry binding via host_id
    # Host registry binding (ADR-048). PATCH uses exclude_none, so host_id
    # is handled separately in the endpoint via model_fields_set: only this
    # way is explicit host_id=null (unbind — prerequisite for the host
    # delete guard in routers/hosts.py) distinguishable from omission.
    host_id: uuid.UUID | None = None
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

    @field_validator("control_url")
    @classmethod
    def _validate_control_url_update(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("control_url muss mit http:// oder https:// beginnen")
        return v


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
        result.append({**rt_dict, **state_info, "host": _host_ref(host)})
    result.sort(key=lambda x: x.get("ui_order", 99))
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
        live[rt.slug] = data
    return {
        "live": live,
        "watcher_enabled": settings.runtime_watcher_enabled,
        "interval": settings.runtime_watcher_interval,
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
    """Stops a runtime (vllm_docker only)."""
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
    """Restarts a runtime (vllm_docker only)."""
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


# ── Sparkrun recipe management ──────────────────────────────────────────────


class SwitchRecipeBody(BaseModel):
    """Body for ``POST /runtimes/{id}/switch-recipe``."""

    recipe: str = Field(min_length=1, max_length=128)

    @field_validator("recipe")
    @classmethod
    def validate_recipe(cls, v: str) -> str:
        # Allow ``@registry/recipe-name`` or bare ``recipe-name``. Reject
        # anything else to keep shell-safe.
        import re as _re

        if not _re.fullmatch(r"[@\w./-]+", v):
            raise ValueError("recipe contains invalid characters")
        return v.strip()


@router.get("/sparkrun/recipes")
async def list_sparkrun_recipes(current_user=Depends(require_user)):
    """Enumerate available sparkrun recipes on the Spark host.

    Calls ``uvx sparkrun list`` via SSH and returns parsed entries. Used by
    the ``/runtimes`` UI to populate the recipe-switcher dropdown for
    vllm_docker runtimes that are sparkrun-managed.
    """
    from app.services.sparkrun_manager import list_recipes

    recipes = await list_recipes()
    return {"recipes": recipes}


@router.get("/{runtime_id}/current-recipe")
async def get_current_recipe(
    runtime_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Return the sparkrun recipe currently encoded in this runtime's
    ``launch_command``, or ``None`` if not sparkrun-managed.
    """
    from app.services.sparkrun_manager import extract_current_recipe

    rt = await _resolve_runtime_dict(session, runtime_id)
    if rt is None:
        raise HTTPException(status_code=404, detail=f"Runtime '{runtime_id}' nicht gefunden")
    recipe = extract_current_recipe(rt.get("launch_command"))
    return {
        "slug": rt["slug"],
        "current_recipe": recipe,
        "sparkrun_managed": recipe is not None,
    }


@router.post("/{runtime_id}/switch-recipe")
async def switch_sparkrun_recipe(
    runtime_id: str,
    body: SwitchRecipeBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Switch the sparkrun recipe driving this runtime.

    Flow (atomic):
      1. Stop the current container (best-effort)
      2. Persist the new ``launch_command`` derived from ``body.recipe``
      3. Start the new container via SSH
      4. Trigger model-identifier re-probe so the resolver picks up the new model

    Returns 200 with ``ok=true`` once the new container is launching;
    container warmup (2-5 min) happens asynchronously in the background.
    Frontend can poll the runtime health endpoint until model_identifier
    shows the new value.
    """
    from app.services.sparkrun_manager import switch_recipe

    # Slug-or-UUID lookup (mirrors probe_model_endpoint)
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

    if rt.runtime_type != "vllm_docker":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Recipe-switch only supported for vllm_docker runtimes "
                f"(this is {rt.runtime_type!r})."
            ),
        )

    try:
        result = await switch_recipe(session, rt, body.recipe)
    except ValueError as exc:
        # ``build_launch_command`` raises on invalid slug/recipe — surface as 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("message", "switch failed"))
    return result


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


async def _runtime_row_response(session: AsyncSession, rt: Runtime) -> dict:
    """CRUD response with the same host shape as GET /runtimes (HostRef|null).

    Without this, POST/PATCH would return the DEPRECATED legacy string field
    `host`, while GET returns an object — one field name, two shapes."""
    host = await resolve_host_for_runtime(session, rt)
    return {**rt.model_dump(), "host": _host_ref(host)}


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

    host_id (ADR-048) goes through model_fields_set instead of exclude_none:
    an explicit null unbinds the runtime from the host (prerequisite for
    DELETE /api/v1/hosts/{id}, whose 409 guard only clears after unbind).
    """
    rt = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first()
    if not rt:
        raise HTTPException(status_code=404, detail=f"Runtime '{slug}' not found")
    changes = body.model_dump(exclude_none=True)
    changes.pop("host_id", None)
    if "host_id" in body.model_fields_set:
        if body.host_id is not None:
            await _validate_host_id(session, body.host_id)
        rt.host_id = body.host_id
    for k, v in changes.items():
        setattr(rt, k, v)
    rt.updated_at = datetime.utcnow()
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
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
