"""Harness/provider compatibility (ADR-056).

Central classification of runtimes into wire protocols and the v1
harness x protocol matrix. Replaces the slug-prefix checks previously
scattered across internal.py, docker_agent_sync.py and compose_renderer.py.
"""
from __future__ import annotations

from app.models.runtime import Runtime

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
