"""Harness/provider compatibility (ADR-056).

Central classification of runtimes into wire protocols and the v1
harness x protocol matrix. Replaces the slug-prefix checks previously
scattered across internal.py, docker_agent_sync.py and compose_renderer.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.secrets_helper import (
    get_secret_plaintext_by_id,
    get_secret_plaintext_by_key,
)

logger = logging.getLogger(__name__)

HARNESSES: tuple[str, ...] = ("claude", "openclaude", "omp", "kimi")
HARNESS_LABELS: dict[str, str] = {
    "claude": "Claude Code",
    "openclaude": "OpenClaude",
    "omp": "omp",
    "kimi": "Kimi Code",
}

# runtime_type values that speak the OpenAI-completions protocol. "omp" is a
# legacy runtime_type from before decoupling — such rows are plain OpenAI
# providers (the harness aspect now lives on the agent).
_OPENAI_TYPES = frozenset(
    {
        "vllm_docker", "llamacpp_docker", "ssh_process", "lmstudio",
        "openai_compatible", "unsloth", "cloud", "omp",
    }
)

#: Realtime voice runtimes → the provider name the voice-worker understands.
#: The ONE place that decides which runtime rows Jarvis may bind to; the
#: resolver, the seed check and the worker's allowlist all read from here, so
#: adding a third arm (e.g. "voice_local") is one entry plus one seed row.
VOICE_RUNTIME_TYPES: dict[str, str] = {
    "voice_openai": "openai",
    "voice_xai": "xai",
}

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
# "kimi" is protocol-fixed like grok: the Kimi Code CLI talks to the Moonshot
# managed endpoint (api.kimi.com/coding/v1) over its own OAuth device-code
# grant (credentials/ files in the per-agent KIMI_CODE_HOME mount — no
# long-lived token exists, and refresh-token ROTATION kills any copied
# credential file; spike 2026-07-24). A kimi agent therefore only binds to a
# `kimi-cloud` seed runtime (runtime_type "kimi"); the binding is a display
# anchor — kimi reads no provider env from it. Kimi CAN speak to custom
# OpenAI-compatible providers via config.toml — if that is ever wired, extend
# this set with "openai" and render a provider block into the config template.
HARNESS_PROTOCOLS: dict[str, frozenset[str]] = {
    "claude": frozenset({"anthropic"}),
    "openclaude": frozenset({"openai"}),
    "omp": frozenset({"openai"}),
    "hermes": frozenset({"openai"}),
    "grok": frozenset({"grok"}),
    "kimi": frozenset({"kimi"}),
    # jarvis is the voice concierge, not a CLI harness: it speaks realtime
    # speech-to-speech to one cloud provider. Its runtime rows are display
    # anchors that carry WHICH provider, nothing else — the voice-worker reads
    # the binding per call and holds the API keys in its own env (ADR-082).
    "jarvis": frozenset({"voice"}),
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
    # kimi runtimes carry their own fixed wire protocol (Moonshot managed
    # endpoint + per-agent OAuth files) — only the kimi harness accepts them.
    if (runtime.runtime_type or "").strip() == "kimi":
        return "kimi"
    # Voice runtimes must be classified BEFORE the _OPENAI_TYPES check: the
    # OpenAI voice arm talks to api.openai.com, but its wire protocol is the
    # realtime speech socket, not chat completions. Letting it fall through
    # would make every openai-speaking CLI harness look compatible with it.
    if (runtime.runtime_type or "").strip() in VOICE_RUNTIME_TYPES:
        return "voice"
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
    if (runtime.runtime_type or "").strip() == "kimi":
        return "kimi"
    proto = runtime_protocol(runtime)
    if proto == "anthropic":
        return "claude"
    if proto == "openai":
        return "openclaude"
    return None


# Harnesses that CANNOT boot without a runtime binding.
#
# Background (incident 2026-09-05): the omp container renders its provider
# config from OPENAI_BASE_URL/OPENAI_MODEL and refuses to start when they are
# missing — `docker/omp-bridge/entrypoint.sh` prints
# "FATAL: OPENAI_BASE_URL/OPENAI_MODEL not set" and exits 1, which turns into
# a container restart loop. Those two variables come ONLY from the bound
# runtime row (routers/internal.py::agent_bootstrap → build_runtime_env), so
# an omp agent with runtime_id = NULL is dead on arrival. The claude /
# openclaude images do have docker-compose env defaults, so clearing their
# binding stays survivable.
HARNESSES_REQUIRING_RUNTIME_BINDING: frozenset[str] = frozenset({"omp"})


def requires_runtime_binding(harness: str | None) -> bool:
    """True when an agent on this harness must keep a runtime binding.

    Callers pass the EFFECTIVE harness (``agent.harness`` first, then
    ``derive_harness(runtime)`` for legacy NULL rows) — the same precedence
    the bootstrap and the switch service use.
    """
    return (harness or "") in HARNESSES_REQUIRING_RUNTIME_BINDING


# ── ADR-084: ACP als Harness-Eigenschaft + Fähigkeiten-Matrix ───────────────
#
# Verhalten leitet sich aus Harness und Runtime-Fähigkeit ab, NIE aus der
# Identität eines Agenten. ADR-081 hatte den omp-ACP-Treiber per
# Namensliste (OMP_ACP_AGENT_SLUGS) deployment-konfiguriert — ein frisch
# angelegter Agent fiel dadurch auf den nativen TUI-Pfad zurück (Pane-Scrape,
# Komposer-Verifikation, Hook-Signal-Datei; docs/dispatch-path-parity.md
# Zeilen 20/33/43), mit vier gescheiterten Versuchen als Live-Evidenz.


def omp_driver_for(harness: str | None) -> str:
    """Which driver the omp bridge runs for this harness: ``"acp"`` | ``"native"``.

    THE one decision point (ADR-084). ACP is a property of the omp harness:
    every omp agent gets it, including one created five minutes ago in a
    fresh install. The single global escape hatch is ``OMP_DRIVER_DEFAULT``
    (deployment config, read via ``settings.omp_driver_default``) — it
    switches the WHOLE fleet, never a name list.

    Non-omp harnesses always answer ``"native"`` (their containers never run
    the omp bridge, so the answer is only meaningful for compose injection).
    """
    if (harness or "") != "omp":
        return "native"
    driver = (getattr(settings, "omp_driver_default", "acp") or "acp").strip().lower()
    return driver if driver in ("acp", "native") else "acp"


@dataclass(frozen=True)
class HarnessCapabilities:
    """What a harness supports — the matrix ADR-084 consolidates.

    Every field is grounded in one code site (cited in the comment); before
    this matrix the same facts were scattered over protocol checks and
    image-name comparisons. ``settings_extras`` is the turn-signal-hooks +
    statusLine pair from ``plugin_manager.render_agent_settings``: claude is
    anthropic-protocol by definition (True), openclaude is protocol-flexible
    (None → decide via ``runtime_protocol(runtime) == "anthropic"``), every
    other harness never receives the unknown keys (False).
    """

    #: Claude-Code settings.json extras (hooks + statusLine) — plugin_manager.
    settings_extras: bool | None
    #: shared-mcp compose volume — compose_renderer only mounts it on the
    #: claude-agent anchor.
    shared_mcp_mount: bool
    #: native start-claude.sh launcher for host agents — docker_agent_sync
    #: renders it only for harness claude.
    host_native_launcher: bool
    #: Claude-Code plugin/skill files take effect on this harness (settings.json
    #: consumer). omp/kimi carry their own config surfaces and ignore them.
    cli_plugins: bool
    cli_skills: bool


HARNESS_CAPABILITIES: dict[str, HarnessCapabilities] = {
    "claude": HarnessCapabilities(
        settings_extras=True,
        shared_mcp_mount=True,
        host_native_launcher=True,
        cli_plugins=True,
        cli_skills=True,
    ),
    "openclaude": HarnessCapabilities(
        settings_extras=None,  # decide per bound runtime's protocol
        shared_mcp_mount=False,
        host_native_launcher=False,
        cli_plugins=True,
        cli_skills=True,
    ),
    "omp": HarnessCapabilities(
        settings_extras=False,
        shared_mcp_mount=False,
        host_native_launcher=False,
        cli_plugins=False,
        cli_skills=False,
    ),
    "kimi": HarnessCapabilities(
        settings_extras=False,
        shared_mcp_mount=False,
        host_native_launcher=False,
        cli_plugins=False,
        cli_skills=False,
    ),
}

# Host-only harnesses (ADR-064/066) — same shape, all capabilities off except
# the hermes ACP driver knob, which is deployment config (settings.hermes_driver).
for _h in ("hermes", "grok"):
    HARNESS_CAPABILITIES[_h] = HarnessCapabilities(
        settings_extras=False,
        shared_mcp_mount=False,
        host_native_launcher=False,
        cli_plugins=False,
        cli_skills=False,
    )


def capabilities_for(harness: str | None) -> HarnessCapabilities:
    """Capabilities for ``harness``; unknown harnesses get the all-off row."""
    return HARNESS_CAPABILITIES.get(
        harness or "",
        HarnessCapabilities(
            settings_extras=False,
            shared_mcp_mount=False,
            host_native_launcher=False,
            cli_plugins=False,
            cli_skills=False,
        ),
    )


def settings_extras_for(harness: str | None, runtime: Runtime | None) -> bool:
    """Turn-signal hooks + statusLine decision, previously inlined at three
    call sites as ``runtime_protocol(runtime) == "anthropic"``."""
    caps = capabilities_for(harness)
    if caps.settings_extras is None:
        return runtime_protocol(runtime) == "anthropic"
    return caps.settings_extras
