"""Harness/provider compatibility (ADR-056).

Central classification of runtimes into wire protocols and the v1
harness x protocol matrix. Replaces the slug-prefix checks previously
scattered across internal.py, docker_agent_sync.py and compose_renderer.py.
"""
from __future__ import annotations

import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.secrets_helper import (
    get_secret_plaintext_by_id,
    get_secret_plaintext_by_key,
)

logger = logging.getLogger(__name__)

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

# HARNESS_PROTOCOLS intentionally covers "hermes" + "grok" too (ADR-064/066)
# even though HARNESSES/HARNESS_LABELS stay cli-bridge-only: these are host-only
# harnesses (see host_harness_adapter) that must still answer is_compatible()
# checks in the host provisioning/switch dispatch, but must NOT appear in the
# cli-bridge runtime-switch matrix surfaced by routers/runtimes.py (which
# iterates HARNESSES).
#
# "grok" is protocol-fixed: the Grok Build CLI talks ONLY to xAI cloud over its
# own OAuth (~/.grok/auth.json) — it cannot be pointed at an OpenAI/Anthropic
# endpoint, so it carries its own "grok" wire protocol. A grok agent therefore
# only binds to the seed `grok-cloud` runtime (runtime_type "grok"); any
# openai/anthropic runtime is a clean 422 mismatch. The binding is a display
# anchor only — grok reads no provider env from it (ADR-066).
HARNESS_PROTOCOLS: dict[str, frozenset[str]] = {
    "claude": frozenset({"anthropic"}),
    "openclaude": frozenset({"openai"}),
    "omp": frozenset({"openai"}),
    "hermes": frozenset({"openai"}),
    "grok": frozenset({"grok"}),
}


def runtime_protocol(runtime: Runtime | None) -> str | None:
    """Classify a runtime row's wire protocol: "anthropic" | "openai" | None.

    None = special/unknown (e.g. hermes) — not part of the switch matrix.
    """
    if runtime is None:
        return None
    # Slug arm uses the exact legacy prefix "anthropic-claude-" (the seed
    # convention for Claude OAuth runtimes). A broader "anthropic-" would
    # misclassify e.g. an "anthropic-proxy-*" OpenAI-compatible shim as the
    # anthropic protocol. The runtime_type arm still matches any "anthropic*"
    # type (anthropic_cloud, anthropic_vertex, …).
    if (runtime.slug or "").startswith("anthropic-claude-") or (
        runtime.runtime_type or ""
    ).startswith("anthropic"):
        return "anthropic"
    # grok runtimes carry their own fixed wire protocol (xAI cloud OAuth) — they
    # are neither openai- nor anthropic-compatible, and only the grok harness
    # accepts them (ADR-066).
    if (runtime.runtime_type or "").strip() == "grok":
        return "grok"
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

    OpenAI-protocol order: agent.secret_id > runtime.api_key_secret_id. No
    global vault fallback — removed 2026-07-05 (ADR-056 amendment, Finding 5):
    a global "ollama_api_key" fallback meant ANY openai-protocol runtime
    (including local, keyless vLLM/LM Studio) silently inherited a paid cloud
    key as its Bearer token. Neither stage resolving → no OPENAI_API_KEY is
    set at all, matching how local runtimes without a bound secret already
    behave. Anthropic protocol uses the global OAuth token
    ("claude_code_oauth_token") — that's the regular OAuth path, not a
    fallback, and is unaffected by this change.

    agent=None is allowed (the bootstrap helper has no agent context at one
    call site) — stage 1 (agent.secret_id) is simply skipped then.
    """
    proto = runtime_protocol(runtime)
    if proto == "anthropic":
        oauth = await get_secret_plaintext_by_key(session, "claude_code_oauth_token")
        return {"CLAUDE_CODE_OAUTH_TOKEN": oauth} if oauth else {}

    # openai protocol — also the legacy default when runtime is unknown/None.
    if agent is not None and agent.secret_id:
        key = await get_secret_plaintext_by_id(session, agent.secret_id)
        if key:
            return {"OPENAI_API_KEY": key}
        logger.warning(
            "resolve_provider_credentials: agent %s has secret_id set but it "
            "did not resolve — falling back to runtime key",
            agent.name,
        )
    if runtime is not None and runtime.api_key_secret_id:
        key = await get_secret_plaintext_by_id(session, runtime.api_key_secret_id)
        if key:
            return {"OPENAI_API_KEY": key}
    return {}


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
