"""
Internal endpoints — only reachable from the Docker network.
Caddy must NOT forward /api/v1/internal/* to the outside.
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
    """Returns the env vars a container needs for this runtime.

    Phase 16 (D-14/D-15/D-16/D-17):
      - runtime is None or enabled=False  → empty dict
      - anthropic protocol → empty dict here; provider auth
        (CLAUDE_CODE_OAUTH_TOKEN) is resolved centrally in
        resolve_provider_credentials (ADR-056), NO OPENAI_* keys
      - all other runtimes → OPENAI_BASE_URL (from runtime.endpoint) +
        OPENAI_MODEL (from runtime.model_identifier if set)

    Phase 24 (HERM-04, ADR-029):
      - runtime_type == "hermes" → host-side Hermes worker, vLLM provider:
        OPENAI_BASE_URL + OPENAI_MODEL, NO Anthropic tokens. A dedicated
        branch (instead of just falling through the else path) gives Phase 25
        a clean hook for HERMES_HOME / HERMES_PROFILE / profile switching
        without routing on slug prefixes.
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
    from app.services.harness_compat import runtime_protocol
    if runtime_protocol(runtime) == "anthropic":
        # Provider auth (CLAUDE_CODE_OAUTH_TOKEN) is resolved centrally in
        # resolve_provider_credentials (ADR-056) — no longer loaded here to
        # avoid a double-fetch. Anthropic runtimes need no BASE_URL/MODEL env:
        # the claude binary talks to api.anthropic.com directly.
        return tokens
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
    """Bootstrap endpoint: container fetches its tokens at startup.

    Returns MC_AGENT_TOKEN + OPENAI_API_KEY — decrypted from the vault.
    No auth header needed (network restriction via Caddy/Docker).
    """
    from app.models.agent import Agent

    # Find agent in DB (case-insensitive)
    result = await session.exec(
        select(Agent).where(Agent.name.ilike(agent_name))  # type: ignore[union-attr]
    )
    agent = result.first()
    if not agent:
        raise HTTPException(404, f"Agent '{agent_name}' nicht gefunden")

    slug = agent.name.lower()
    tokens = {}

    # MC_AGENT_TOKEN from vault (key: mc_token_{slug})
    mc_token = await get_secret_plaintext_by_key(session, f"mc_token_{slug}")
    if mc_token:
        tokens["MC_AGENT_TOKEN"] = mc_token
    else:
        logger.warning("bootstrap(%s): mc_token_%s nicht im Vault", agent_name, slug)

    # OPENAI_API_KEY / CLAUDE_CODE_OAUTH_TOKEN are resolved together below,
    # after the runtime is loaded, via resolve_provider_credentials (ADR-056).

    # GH_TOKEN: global GitHub Personal Access Token for autonomous git ops
    # (push, PR, etc.). Lives once in the vault (key="github_token") and is
    # delivered to all agents that bootstrap — agents that don't use it
    # simply never activate it. Per-agent gating can be retrofitted later
    # via an agent field if desired.
    gh_token = await get_secret_plaintext_by_key(session, "github_token")
    if gh_token:
        tokens["GH_TOKEN"] = gh_token

    # Content/media tokens — same model as GH_TOKEN: lives once in the
    # vault, delivered to all agents. Agents that don't use the tooling
    # just ignore the variable. Prevents skills from repeatedly probing
    # blocked endpoints for auth lookups (see the 2026-05-07 viral-shorts
    # E2E run, where Davinci spent 5 min looking for the ElevenLabs key).
    # Per-agent gating later via skill_filter / scopes.
    eleven_token = await get_secret_plaintext_by_key(session, "elevenlabs_api_key")
    if eleven_token:
        tokens["ELEVENLABS_API_KEY"] = eleven_token

    higgs_token = await get_secret_plaintext_by_key(session, "higgsfield_api_key")
    if higgs_token:
        tokens["HIGGSFIELD_API_KEY"] = higgs_token

    x_token = await get_secret_plaintext_by_key(session, "x_api_bearer_token")
    if x_token:
        tokens["X_API_BEARER_TOKEN"] = x_token

    # Runtime selection — per-agent runtime selection.
    # NULL runtime_id → falls back to docker-compose env defaults. If the
    # runtime is disabled the same fallback applies, so a misconfiguration
    # doesn't leave the agent in an unstartable state.
    # Phase 16 (D-17): routing logic consolidated in build_runtime_env().
    runtime = None
    if agent.runtime_id:
        from app.models.runtime import Runtime
        runtime = await session.get(Runtime, agent.runtime_id)
        rt_env = await build_runtime_env(runtime, session)
        tokens.update(rt_env)

    # Provider auth (ADR-056, amended 2026-07-05): agent secret > runtime
    # secret for openai protocol; CLAUDE_CODE_OAUTH_TOKEN for anthropic. No
    # global vault fallback — an agent with neither secret bound simply gets
    # no OPENAI_API_KEY. Single source shared with the .env render so the two
    # can never drift.
    from app.services.harness_compat import resolve_provider_credentials
    creds = await resolve_provider_credentials(session, agent, runtime)
    tokens.update(creds)

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
