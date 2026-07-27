"""
Model catalogue — central overview of all available AI models.

Phase 29 (Gateway sunset): static MODEL_METADATA catalogue is the single
source of truth. The runtimes DB table (Phase 16, ADR-028) carries
per-runtime model bindings. Frontend should consume this list + the
runtimes endpoints; Phase 31 rebuild will reshape the response.

Combines:
1. Static metadata (cost, context window, capabilities)
2. Usage statistics from MC (which agents use which model)
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services import model_catalog

router = APIRouter(prefix="/api/v1", tags=["models"])


# ── Static model metadata ────────────────────────────────────────────────────
# Hand-curated — extended as new models come along.
# Cost in USD per 1M tokens (input/output).
# params: parameter size as a string for the UI.

MODEL_METADATA: dict[str, dict] = {
    # ── OpenAI ────────────────────────────────────────────────────────────────
    "gpt-4o": {
        "name": "GPT-4o",
        "provider": "openai",
        "context_window": 128_000,
        "max_output": 16_384,
        "input_cost": 2.50,
        "output_cost": 10.0,
        "capabilities": ["coding", "analysis", "reasoning", "vision", "tools"],
        "tier": "balanced",
        "params": "—",
        "description": "OpenAIs Flaggschiff. Multimodal mit schneller Antwortzeit.",
    },
    "gpt-4o-mini": {
        "name": "GPT-4o Mini",
        "provider": "openai",
        "context_window": 128_000,
        "max_output": 16_384,
        "input_cost": 0.15,
        "output_cost": 0.60,
        "capabilities": ["coding", "analysis", "tools"],
        "tier": "fast",
        "params": "—",
        "description": "Günstigste OpenAI-Option. Schnell für einfache Aufgaben.",
    },
    "o1": {
        "name": "o1",
        "provider": "openai",
        "context_window": 200_000,
        "max_output": 100_000,
        "input_cost": 15.0,
        "output_cost": 60.0,
        "capabilities": ["coding", "reasoning", "analysis"],
        "tier": "reasoning",
        "params": "—",
        "description": "Reasoning-Modell. Denkt in Schritten, ideal für Mathe/Logik.",
    },
    "o3-mini": {
        "name": "o3 Mini",
        "provider": "openai",
        "context_window": 200_000,
        "max_output": 100_000,
        "input_cost": 1.10,
        "output_cost": 4.40,
        "capabilities": ["coding", "reasoning"],
        "tier": "reasoning",
        "params": "—",
        "description": "Schnelles Reasoning-Modell. Günstiger als o1.",
    },
    "openai-codex/gpt-5.3-codex": {
        "name": "GPT-5.3 Codex",
        "provider": "openai-codex",
        "context_window": 256_000,
        "max_output": 32_768,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "tools"],
        "tier": "flatrate",
        "params": "—",
        "description": "OpenAIs Coding-Flaggschiff via Ollama Cloud. Stark bei Software Engineering und Agent-Tasks.",
    },
    # ── Google Gemini ─────────────────────────────────────────────────────────
    "gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro",
        "provider": "google",
        "context_window": 1_000_000,
        "max_output": 65_536,
        "input_cost": 1.25,
        "output_cost": 10.0,
        "capabilities": ["coding", "analysis", "reasoning", "vision", "tools"],
        "tier": "flagship",
        "params": "—",
        "description": "Googles stärkstes Modell. 1M Token Context Window.",
    },
    "gemini-2.0-flash": {
        "name": "Gemini 2.0 Flash",
        "provider": "google",
        "context_window": 1_000_000,
        "max_output": 8_192,
        "input_cost": 0.10,
        "output_cost": 0.40,
        "capabilities": ["coding", "analysis", "vision", "tools"],
        "tier": "fast",
        "params": "—",
        "description": "Sehr schnell und günstig. Grosses Context Window.",
    },
    # ── Ollama Cloud (Flatrate) ───────────────────────────────────────────────
    # Flagship / Allrounder
    "qwen3.5:397b-cloud": {
        "name": "Qwen 3.5 397B",
        "provider": "ollama",
        "context_window": 262_144,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "vision", "tools"],
        "tier": "flatrate",
        "params": "397B (17B aktiv)",
        "description": "Alibabas MoE-Flaggschiff. 262K Context, Reasoning. SWE-Bench 76.4%, MMLU 92.6%, AIME 91.3%. 7× schneller als Qwen3-235B.",
    },
    "deepseek-v3.2:cloud": {
        "name": "DeepSeek V3.2",
        "provider": "ollama",
        "context_window": 160_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "tools"],
        "tier": "flatrate",
        "params": "685B MoE",
        "description": "DeepSeeks neustes Flaggschiff. Top Reasoning + Agent Performance.",
    },
    "deepseek-v3.1:cloud": {
        "name": "DeepSeek V3.1",
        "provider": "ollama",
        "context_window": 160_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "tools"],
        "tier": "flatrate",
        "params": "671B MoE",
        "description": "Hybrid Thinking-Modell. Denk- und Nicht-Denk-Modus. Starke Tool-Nutzung.",
    },
    "glm-5:cloud": {
        "name": "GLM-5",
        "provider": "ollama",
        "context_window": 198_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "tools"],
        "tier": "flatrate",
        "params": "744B (40B aktiv)",
        "description": "Z.ai MoE-Flaggschiff. Stark bei Coding & Reasoning.",
    },
    "cogito-2.1:cloud": {
        "name": "Cogito 2.1",
        "provider": "ollama",
        "context_window": 160_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "multilingual"],
        "tier": "flatrate",
        "params": "671B",
        "description": "Bestes US Open-Weight LLM. MIT-Lizenz. Token-effizientes Reasoning.",
    },
    "mistral-large-3:cloud": {
        "name": "Mistral Large 3",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "vision", "tools"],
        "tier": "flatrate",
        "params": "675B MoE",
        "description": "Mistrals Enterprise-Flaggschiff. Multimodal, 11 Sprachen, Apache 2.0.",
    },
    "kimi-k2.5:cloud": {
        "name": "Kimi K2.5",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "vision", "tools"],
        "tier": "flatrate",
        "params": "—",
        "description": "Moonshots multimodales Agent-Modell. Vision + Coding + 256K Context.",
    },
    "minimax-m2.5:cloud": {
        "name": "MiniMax M2.5",
        "provider": "ollama",
        "context_window": 198_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning", "tools"],
        "tier": "flatrate",
        "params": "—",
        "description": "SWE-Bench 80.2%. Stark bei Software Engineering.",
    },
    "gpt-oss:cloud": {
        "name": "GPT-OSS",
        "provider": "ollama",
        "context_window": 128_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "reasoning", "tools"],
        "tier": "flatrate",
        "params": "120B",
        "description": "OpenAIs Open-Source Reasoning-Modell. Chain-of-Thought sichtbar. Apache 2.0.",
    },
    # Coding-Spezialisten
    "qwen3-coder:cloud": {
        "name": "Qwen3 Coder",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "tools"],
        "tier": "flatrate",
        "params": "480B MoE",
        "description": "Alibabas grösstes Coding-Modell. 256K Context, erweiterbar auf 1M.",
    },
    "qwen3-coder-next:cloud": {
        "name": "Qwen3 Coder Next",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "tools"],
        "tier": "flatrate",
        "params": "80B",
        "description": "Alibabas Coding-Spezialist der nächsten Generation. Agentic Coding.",
    },
    "devstral-2:cloud": {
        "name": "Devstral 2",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "tools"],
        "tier": "flatrate",
        "params": "123B",
        "description": "Mistrals Coding-Agent. 256K Context. Software Engineering.",
    },
    # Reasoning-Spezialisten
    "kimi-k2-thinking:cloud": {
        "name": "Kimi K2 Thinking",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "reasoning", "tools", "search"],
        "tier": "flatrate",
        "params": "—",
        "description": "Moonshots Reasoning-Agent. 200-300 sequenzielle Tool-Calls. SWE-Bench 71.3%.",
    },
    "nemotron-3-nano:cloud": {
        "name": "Nemotron 3 Nano",
        "provider": "ollama",
        "context_window": 1_000_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "reasoning", "tools", "multilingual"],
        "tier": "flatrate",
        "params": "30B (3.5B aktiv)",
        "description": "NVIDIAs Hybrid-Modell. 1M Context. Reasoning ein/ausschaltbar.",
    },
    # Vision
    "qwen3-vl:cloud": {
        "name": "Qwen3 VL",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["vision", "coding", "analysis", "tools"],
        "tier": "flatrate",
        "params": "235B",
        "description": "Alibabas Vision-Modell. UI-Erkennung, Video, OCR in 32 Sprachen.",
    },
    # Kompakt / Effizient
    "qwen3-next:cloud": {
        "name": "Qwen3 Next",
        "provider": "ollama",
        "context_window": 256_000,
        "max_output": 16_384,
        "input_cost": 0,
        "output_cost": 0,
        "capabilities": ["coding", "analysis", "reasoning"],
        "tier": "flatrate",
        "params": "80B",
        "description": "Hybrid-Attention (DeltaNet + MoE). Schnelle Inferenz, Multi-Token Prediction.",
    },
}

# Provider info for UI grouping
PROVIDERS = {
    "openai": {"name": "OpenAI", "color": "#10A37F"},
    "openai-codex": {"name": "OpenAI Codex (Ollama)", "color": "#10A37F"},
    "google": {"name": "Google", "color": "#4285F4"},
    "ollama": {"name": "Ollama Cloud", "color": "#FFFFFF"},
}


@router.get("/models")
async def list_models(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Model catalogue: all available models with metadata + usage info.

    Phase 29: source = static MODEL_METADATA + agents.model DB-column.
    Gateway-merge dropped (no gateway anymore). Frontend will be reshaped
    in Phase 31 to consume the runtimes table directly for live data.
    """
    # 1. Fetch agent usage from DB (which model is used by whom)
    result = await session.exec(select(Agent).where(Agent.model.isnot(None)))
    agents = result.all()
    model_usage: dict[str, list[dict]] = {}
    for a in agents:
        if a.model:
            model_usage.setdefault(a.model, []).append({
                "id": str(a.id),
                "name": a.name,
                "emoji": a.emoji,
            })

    # 2. Catalogue from the static metadata map — mark all as available
    # input_cost/output_cost are stripped from the API response (stale, a second
    # source of truth). Cost will come from the model_prices DB table going forward.
    _COST_FIELDS = {"input_cost", "output_cost"}
    catalog: list[dict] = []
    for model_id, meta in MODEL_METADATA.items():
        catalog.append({
            "id": model_id,
            "available": True,
            "used_by": model_usage.get(model_id, []),
            **{k: v for k, v in meta.items() if k not in _COST_FIELDS},
        })

    return {
        "models": catalog,
        "providers": PROVIDERS,
        "gateway_connected": False,  # Phase 29: gateway removed
        "total": len(catalog),
    }


# ── Provider model catalog ───────────────────────────────────────────────────
# Lives in this router (rather than its own file) purely for route ordering:
# ``/models/{model_id}`` below would otherwise swallow ``/models/catalog``.
# FastAPI matches in declaration order, and keeping both in one file makes that
# dependency visible instead of hiding it in main.py's include order. All actual
# logic sits in ``services/model_catalog.py``.
#
# Reminder on the contract: the catalog says which models a provider OFFERS.
# ``runtime.model_identifier`` remains the only statement about what RUNS.


def _catalog_response(providers: list[dict]) -> dict:
    """One response shape for GET and refresh, so the frontend needs one type."""
    return {
        "providers": providers,
        "total_models": sum(len(p["models"]) for p in providers),
        "new_models": sum(p["new_count"] for p in providers),
    }


@router.get("/models/catalog")
async def get_model_catalog(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Per provider: available models, probe status, cache age.

    Each model carries ``bound`` — true when some runtime row already uses this
    ``model_identifier``. "New at the provider" is exactly ``bound == false``;
    no extra DB column is involved.
    """
    return _catalog_response(await model_catalog.build_catalog(session))


@router.post("/models/catalog/refresh")
async def refresh_model_catalog(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.OPERATOR)),
):
    """Drop the cache and re-probe every provider (the "Jetzt prüfen" button).

    Same response shape as GET so the frontend can reuse one client type —
    mirrors ``POST /api/v1/cli-tools/check``.
    """
    await model_catalog.invalidate_cache(session)
    return _catalog_response(await model_catalog.build_catalog(session, force=True))


class CatalogBindBody(BaseModel):
    """Create a runtime row for a catalog model, inheriting the provider setup."""

    provider_key: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    slug: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)


_SLUG_SANITIZE = re.compile(r"[^a-z0-9]+")


def _derive_slug(prefix: str, model_id: str) -> str:
    """``claude-opus-5`` under the anthropic provider → ``anthropic-claude-opus-5``.

    The prefix is skipped when the model id already starts with it, so
    ``grok-4.5`` stays ``grok-4-5`` instead of becoming ``grok-grok-4-5``.
    """
    base = _SLUG_SANITIZE.sub("-", model_id.lower()).strip("-")
    prefix = _SLUG_SANITIZE.sub("-", prefix.lower()).strip("-")
    slug = base if (prefix and base.startswith(prefix)) else f"{prefix}-{base}"
    return slug.strip("-")[:64]


@router.post("/models/catalog/bind", status_code=201)
async def bind_catalog_model(
    body: CatalogBindBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Turn a catalog entry into a real runtime row.

    Endpoint / protocol / api_key_secret_id are inherited from an EXISTING
    runtime of the same provider rather than re-entered — a bound model that
    points at a different endpoint than its siblings is always a mistake.
    Creation itself goes through ``runtimes.create_runtime_db`` so validation,
    the 409-on-duplicate-slug rule and the response shape stay in one place.
    """
    from app.routers.runtimes import RuntimeCreate, create_runtime_db

    targets = {t.key: t for t in await model_catalog.build_provider_targets(session)}
    target = targets.get(body.provider_key)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unbekannter Provider '{body.provider_key}' (GET /api/v1/models/catalog)",
        )
    template = target.runtime
    if template is None:
        raise HTTPException(
            status_code=422,
            detail=f"Provider '{body.provider_key}' hat keine Vorlage-Runtime.",
        )

    prefix = target.protocol if target.protocol != "openai" else template.slug
    slug = body.slug or _derive_slug(prefix, body.model_id)

    # Idempotency: a row that already points at exactly this model on this
    # provider is a no-op (200-style success), while a slug that means something
    # ELSE is a genuine conflict the operator must resolve.
    existing = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first()
    if existing is not None:
        if existing.model_identifier == body.model_id:
            return {"slug": existing.slug, "created": False, "runtime": existing.model_dump()}
        raise HTTPException(
            status_code=409,
            detail=(
                f"Runtime-Slug '{slug}' existiert bereits mit Modell "
                f"'{existing.model_identifier}'. Eigenen Slug angeben."
            ),
        )

    create = RuntimeCreate(
        slug=slug,
        display_name=body.display_name or body.model_id,
        runtime_type=template.runtime_type,
        endpoint=template.endpoint,
        healthcheck_path=template.healthcheck_path,
        model_identifier=body.model_id,
        api_key_secret_id=template.api_key_secret_id,
        host_id=template.host_id,
        role_tags=list(template.role_tags or []),
        supports_tools=template.supports_tools,
        supports_reasoning=template.supports_reasoning,
        supports_streaming=template.supports_streaming,
        max_context_len=template.max_context_len,
        preferred_context_len=template.preferred_context_len,
    )
    runtime = await create_runtime_db(create, session=session, current_user=current_user)
    return {"slug": slug, "created": True, "runtime": runtime}


@router.get("/models/{model_id}")
async def get_model(
    model_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Detail info for a single model.

    Phase 29: availability is now derived solely from MODEL_METADATA. Models
    not in the static catalogue return 404. (Phase 31 will fetch live model
    info from the runtimes table per ADR-028.)
    """
    meta = MODEL_METADATA.get(model_id)

    if meta is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    # Agents using this model
    result = await session.exec(select(Agent).where(Agent.model == model_id))
    agents = result.all()
    used_by = [{"id": str(a.id), "name": a.name, "emoji": a.emoji} for a in agents]

    _COST_FIELDS = {"input_cost", "output_cost"}
    return {
        "id": model_id,
        "available": True,
        "used_by": used_by,
        **{k: v for k, v in meta.items() if k not in _COST_FIELDS},
    }
