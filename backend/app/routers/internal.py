"""
Internal endpoints — nur vom Docker-Netzwerk erreichbar.
Caddy darf /api/v1/internal/* NICHT nach aussen weiterleiten.
"""
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.services.secrets_helper import get_secret_plaintext_by_key
from fastapi import Depends

if TYPE_CHECKING:
    from app.models.runtime import Runtime

logger = logging.getLogger("mc.internal")

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


async def build_runtime_env(
    runtime: "Runtime | None",
    session: AsyncSession,
) -> dict[str, str]:
    """Liefert die env-vars die ein Container für diese Runtime braucht.

    Phase 16 (D-14/D-15/D-16/D-17):
      - runtime is None oder enabled=False  → leeres dict
      - slug startet mit "anthropic-claude-" → CLAUDE_CODE_OAUTH_TOKEN aus Vault
        (Key: claude_code_oauth_token), KEINE OPENAI_*-Keys
      - alle anderen Slugs → OPENAI_BASE_URL (aus runtime.endpoint) +
        OPENAI_MODEL (aus runtime.model_identifier wenn gesetzt)

    Phase 24 (HERM-04, ADR-029):
      - runtime_type == "hermes" → host-side Hermes worker, vLLM provider:
        OPENAI_BASE_URL + OPENAI_MODEL, KEINE Anthropic-Tokens. Eigener
        Branch (statt nur über den else-Pfad zu fallen) gibt Phase 25 einen
        sauberen Hook für HERMES_HOME / HERMES_PROFILE / Profile-Switching
        ohne auf Slug-Prefixes zu routen.
    """
    tokens: dict[str, str] = {}
    if runtime is None or not runtime.enabled:
        return tokens
    if runtime.runtime_type == "hermes":
        # ADR-029: host-side Hermes worker — vLLM endpoint, no Anthropic auth.
        # Phase 25 extends this branch with HERMES_HOME / HERMES_PROFILE etc.
        if runtime.endpoint:
            tokens["OPENAI_BASE_URL"] = runtime.endpoint
        if runtime.model_identifier:
            tokens["OPENAI_MODEL"] = runtime.model_identifier
        return tokens
    if runtime.runtime_type == "omp":
        # ADR-045: omp headless runtime — OpenAI-compatible transport (Qwen on
        # the DGX Spark), NO Anthropic auth. Explicit branch (mirroring hermes)
        # rather than falling through the else-path: gives a clean hook for
        # OMP_PROFILE / models.yml rendering without routing on a slug prefix.
        # The container entrypoint renders omp's native models.yml provider from
        # these two vars — runtime.endpoint stays the single source of truth for
        # the URL, and the .env token path is NOT duplicated.
        if runtime.endpoint:
            tokens["OPENAI_BASE_URL"] = runtime.endpoint
        if runtime.model_identifier:
            tokens["OPENAI_MODEL"] = runtime.model_identifier
        return tokens
    if runtime.slug.startswith("anthropic-claude-"):
        oauth = await get_secret_plaintext_by_key(session, "claude_code_oauth_token")
        if oauth:
            tokens["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    else:
        if runtime.endpoint:
            tokens["OPENAI_BASE_URL"] = runtime.endpoint
        if runtime.model_identifier:
            tokens["OPENAI_MODEL"] = runtime.model_identifier
    return tokens


@router.get("/bootstrap")
async def agent_bootstrap(
    agent_name: str = Query(..., description="Agent-Name (z.B. 'rex', 'boss')"),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
):
    """Bootstrap-Endpoint: Container holt seine Tokens beim Start.

    Gibt MC_AGENT_TOKEN + OPENAI_API_KEY zurueck — dekryptiert aus dem Vault.
    Kein Auth-Header noetig (Netzwerk-Restriction via Caddy/Docker).
    """
    from app.models.agent import Agent

    # Agent in DB finden (case-insensitive)
    result = await session.exec(
        select(Agent).where(Agent.name.ilike(agent_name))  # type: ignore[union-attr]
    )
    agent = result.first()
    if not agent:
        raise HTTPException(404, f"Agent '{agent_name}' nicht gefunden")

    slug = agent.name.lower()
    tokens = {}

    # MC_AGENT_TOKEN aus Vault (key: mc_token_{slug})
    mc_token = await get_secret_plaintext_by_key(session, f"mc_token_{slug}")
    if mc_token:
        tokens["MC_AGENT_TOKEN"] = mc_token
    else:
        logger.warning("bootstrap(%s): mc_token_%s nicht im Vault", agent_name, slug)

    # OPENAI_API_KEY: zuerst agent.secret_id (per-Agent Override), dann Fallback auf ollama_api_key
    if agent.secret_id:
        from app.services.secrets_helper import get_secret_plaintext_by_id
        api_key = await get_secret_plaintext_by_id(session, agent.secret_id)
        if api_key:
            tokens["OPENAI_API_KEY"] = api_key
    if "OPENAI_API_KEY" not in tokens:
        api_key = await get_secret_plaintext_by_key(session, "ollama_api_key")
        if api_key:
            tokens["OPENAI_API_KEY"] = api_key

    # GH_TOKEN: globaler GitHub Personal Access Token fuer autonome git-Ops
    # (push, PR, etc.). Liegt einmal im Vault (key="github_token") und wird
    # an alle Agents geliefert die bootstrappen — ungenutzte Agents halten
    # ihn einfach nie aktiv. Per-agent-gating kann spaeter via Agent-Feld
    # nachgeruestet werden falls gewuenscht.
    gh_token = await get_secret_plaintext_by_key(session, "github_token")
    if gh_token:
        tokens["GH_TOKEN"] = gh_token

    # Content/Media tokens — auf demselben Modell wie GH_TOKEN: einmal im
    # Vault, an alle Agents ausgeliefert. Agents die das Tooling nicht
    # nutzen ignorieren die Variable. Verhindert dass Skills mehrfach
    # fuer Auth-Lookups ueber blockierte Endpoints rumprobieren (siehe
    # 2026-05-07 viral-shorts E2E run, wo Davinci 5 min nach dem ElevenLabs-
    # Key gesucht hat). Per-agent gating spaeter via skill_filter / scopes.
    eleven_token = await get_secret_plaintext_by_key(session, "elevenlabs_api_key")
    if eleven_token:
        tokens["ELEVENLABS_API_KEY"] = eleven_token

    higgs_token = await get_secret_plaintext_by_key(session, "higgsfield_api_key")
    if higgs_token:
        tokens["HIGGSFIELD_API_KEY"] = higgs_token

    x_token = await get_secret_plaintext_by_key(session, "x_api_bearer_token")
    if x_token:
        tokens["X_API_BEARER_TOKEN"] = x_token

    # Runtime-Auswahl — per-agent runtime selection.
    # NULL runtime_id → Fallback auf docker-compose env defaults. Wenn die
    # Runtime disabled ist greift der gleiche Fallback, damit eine Fehl-
    # konfiguration den Agent nicht in einen unstartbaren Zustand bringt.
    # Phase 16 (D-17): Routing-Logik in build_runtime_env() konsolidiert.
    if agent.runtime_id:
        from app.models.runtime import Runtime
        runtime = await session.get(Runtime, agent.runtime_id)
        rt_env = await build_runtime_env(runtime, session)
        tokens.update(rt_env)

    # Phase 3 — Claude-Process Recycler kill-switch (MEM-01). Always set,
    # independent of runtime. Container reads this once at start and decides
    # whether to spawn the recycler tmux Window 2 (vs no-op via sleep infinity).
    # Two-tier resolution via the helper from Plan 03-02 — per-agent override
    # wins, else global settings.agent_recycler_enabled (default True).
    # Bootstrap path is the precedence-source for fresh containers without
    # agent.env (or with bootstrap-precedence in entrypoint.sh, Plan 03-05).
    # See ADR-024.
    from app.services.recycler_config import get_effective_recycler_enabled
    tokens["AGENT_RECYCLER_ENABLED"] = (
        "true" if get_effective_recycler_enabled(agent) else "false"
    )

    # CTX-01 (Phase 6): expose context_max to the container so poll.sh has a
    # fallback denominator if the tmux statusline scrape returns no ctx%.
    # 200_000 default matches claude-sonnet-4-6 (CONTEXT.md D-03).
    tokens["CONTEXT_MAX"] = str(agent.context_max or 200_000)

    if not tokens:
        raise HTTPException(404, f"Keine Tokens fuer Agent '{agent_name}' im Vault")

    logger.info(
        "bootstrap(%s): %s",
        agent_name,
        ", ".join(f"{k}={'***' + v[-4:]}" for k, v in tokens.items()),
    )
    return tokens
