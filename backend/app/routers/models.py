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

from fastapi import APIRouter, Depends
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent

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
