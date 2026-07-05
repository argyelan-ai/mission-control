"""Harness/provider compatibility (ADR-056).

Central classification of runtimes into wire protocols and the v1
harness x protocol matrix. Replaces the slug-prefix checks previously
scattered across internal.py, docker_agent_sync.py and compose_renderer.py.
"""
from __future__ import annotations

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.secrets_helper import (
    get_secret_plaintext_by_id,
    get_secret_plaintext_by_key,
)

HARNESSES: tuple[str, ...] = ("claude", "openclaude", "omp")
HARNESS_LABELS: dict[str, str] = {
    "claude": "Claude Code",
    "openclaude": "OpenClaude",
    "omp": "omp",
}

# runtime_type values that speak the OpenAI-completions protocol. "omp" is a
# legacy runtime_type from before decoupling — such rows are plain OpenAI
# providers (the harness aspect now lives on the agent).
_OPENAI_TYPES = frozenset(
    {"vllm_docker", "lmstudio", "openai_compatible", "unsloth", "cloud", "omp"}
)

HARNESS_PROTOCOLS: dict[str, frozenset[str]] = {
    "claude": frozenset({"anthropic"}),
    "openclaude": frozenset({"openai"}),
    "omp": frozenset({"openai"}),
}


def runtime_protocol(runtime: Runtime | None) -> str | None:
    """Classify a runtime row's wire protocol: "anthropic" | "openai" | None.

    None = special/unknown (e.g. hermes) — not part of the switch matrix.
    """
    if runtime is None:
        return None
    if (runtime.slug or "").startswith("anthropic-") or (
        runtime.runtime_type or ""
    ).startswith("anthropic"):
        return "anthropic"
    if (runtime.runtime_type or "").strip() in _OPENAI_TYPES:
        return "openai"
    return None


def is_compatible(harness: str | None, runtime: Runtime | None) -> bool:
    if harness not in HARNESS_PROTOCOLS:
        return False
    proto = runtime_protocol(runtime)
    return proto is not None and proto in HARNESS_PROTOCOLS[harness]


def incompat_reason(harness: str, runtime: Runtime) -> str | None:
    """German explanation for the UI tooltip; None when compatible."""
    if is_compatible(harness, runtime):
        return None
    label = HARNESS_LABELS.get(harness, harness)
    proto = runtime_protocol(runtime)
    if harness == "claude" and proto == "openai":
        return (
            f"{label} spricht nur das Anthropic-Protokoll — "
            f"'{runtime.slug}' ist ein OpenAI-kompatibler Provider. "
            f"Nutze omp oder OpenClaude (Claude Code x OpenAI kommt in v2 via Proxy)."
        )
    if proto == "anthropic":
        return (
            f"{label} unterstuetzt keine Anthropic-OAuth-Provider — "
            f"'{runtime.slug}' braucht das Harness Claude Code."
        )
    return (
        f"Provider '{runtime.slug}' (Typ '{runtime.runtime_type}') ist kein "
        f"Standard-Protokoll und kann nicht frei kombiniert werden."
    )


async def resolve_provider_credentials(
    session: AsyncSession,
    agent: Agent | None,
    runtime: Runtime | None,
) -> dict[str, str]:
    """Resolve auth material for (agent, runtime) — single source for the
    internal bootstrap AND the .env render, so the two can never drift.

    OpenAI-protocol order: agent.secret_id > runtime.api_key_secret_id >
    global vault fallback ("ollama_api_key"). Anthropic protocol uses the
    global OAuth token ("claude_code_oauth_token").

    agent=None is allowed (the bootstrap helper has no agent context at one
    call site) — stage 1 (agent.secret_id) is simply skipped then.
    """
    proto = runtime_protocol(runtime)
    if proto == "anthropic":
        oauth = await get_secret_plaintext_by_key(session, "claude_code_oauth_token")
        return {"CLAUDE_CODE_OAUTH_TOKEN": oauth} if oauth else {}

    # openai protocol — also the legacy default when runtime is unknown/None but
    # an agent-level secret exists (pre-ADR-056 behaviour). Agents WITHOUT a
    # runtime still fall through to the ollama_api_key global fallback, matching
    # today's bootstrap behaviour.
    if agent is not None and agent.secret_id:
        key = await get_secret_plaintext_by_id(session, agent.secret_id)
        if key:
            return {"OPENAI_API_KEY": key}
    if runtime is not None and runtime.api_key_secret_id:
        key = await get_secret_plaintext_by_id(session, runtime.api_key_secret_id)
        if key:
            return {"OPENAI_API_KEY": key}
    key = await get_secret_plaintext_by_key(session, "ollama_api_key")
    return {"OPENAI_API_KEY": key} if key else {}


def derive_harness(runtime: Runtime | None) -> str | None:
    """Legacy fallback when agents.harness is NULL: derive from the runtime.

    Mirrors the pre-ADR-056 image coupling so unmigrated rows behave identically.
    """
    if runtime is None:
        return None
    if (runtime.runtime_type or "").strip() == "omp":
        return "omp"
    proto = runtime_protocol(runtime)
    if proto == "anthropic":
        return "claude"
    if proto == "openai":
        return "openclaude"
    return None
