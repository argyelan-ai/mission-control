"""Host-harness adapters (ADR-064).

One adapter per host CLI. Encapsulates the two things that differ between
host harnesses: rendering the runtime binding into the CLI's native LLM
config, and reloading the agent in place. Shared bootstrap/lifecycle code
(launchctl, agent.env write, workspace layout) stays where it is.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime


@runtime_checkable
class HostHarnessAdapter(Protocol):
    harness: str
    # Human-readable name for the agent wizard's host-harness picker. Lives on
    # the adapter so registering a host harness ships its UI label with it —
    # the frontend used to keep its own HOST_HARNESS_LABELS/HOST_HARNESSES
    # list, which is exactly how "claude" ended up invisible in the wizard.
    label: str
    protocol: str  # "openai" | "anthropic" | "grok" | "kimi"
    # A single-instance host bridge (hermes/grok) hardcodes its config dir +
    # plist to ONE slug; provisioning it onto any other agent would clobber the
    # real singleton. None = the adapter is safe for arbitrary agents.
    singleton_slug: str | None
    # False = this adapter has NO bespoke bootstrap. Being in the registry is
    # then purely about the runtime→agent propagation paths (model sync +
    # in-place switch); the provision endpoint must keep routing such agents to
    # the generic wizard staging path instead of calling bootstrap(). See
    # ClaudeHostAdapter for the one case where this matters.
    supports_bootstrap: bool

    def env_dir(self, agent: Agent) -> str:
        """Directory name under ``~/.mc/agents/`` holding this agent's agent.env.

        Normally the agent slug. Exists as a hook because the LEGACY boss-host
        layout does not follow that convention (see ClaudeHostAdapter).
        """
        ...

    async def build_agent_env(
        self, agent: Agent, runtime: Runtime, token: str, *, session: AsyncSession
    ) -> dict[str, str]: ...

    async def bootstrap(
        self, session: AsyncSession, agent: Agent, runtime: Runtime
    ) -> dict[str, Any]: ...

    async def reload(self, agent: Agent) -> dict[str, Any]: ...


class _SingletonEnvDirMixin:
    """env_dir for the singleton bridges: the slug IS the directory.

    ``singleton_slug`` is the fallback for rows whose ``slug`` column is still
    NULL — previously such a row silently fell back to the literal "hermes"
    directory in ``sync_host_agent_model``, i.e. a grok/kimi sync could have
    written into Hermes' agent.env. Using the adapter's own singleton is the
    only correct answer here.
    """

    def env_dir(self, agent: Agent) -> str:
        return agent.slug or self.singleton_slug  # type: ignore[attr-defined,return-value]


class HermesAdapter(_SingletonEnvDirMixin):
    harness = "hermes"
    label = "Hermes"
    protocol = "openai"
    singleton_slug = "hermes"
    supports_bootstrap = True

    async def build_agent_env(self, agent, runtime, token, *, session):
        from app.services.agent_bootstrap import build_hermes_agent_env
        return await build_hermes_agent_env(runtime, token, session=session)

    async def bootstrap(self, session, agent, runtime):
        from app.services.agent_bootstrap import bootstrap_hermes_agent
        return await bootstrap_hermes_agent(session, agent, runtime)

    async def reload(self, agent):
        # Full process-level restart (orphan sweep + kickstart/fallback +
        # pgrep-verified success + Hermes worker-session respawn), NOT the
        # plain launchd kickstart of _host_agent_lifecycle("restart"). That
        # weaker path only bounces the hermes-bridge HTTP server — the tmux
        # 'hermes-worker' session (where the actual model lives) survives
        # untouched, so a runtime switch reported success while the TUI kept
        # running the stale model (live bug 2026-08-08/09, Task #25/#26). See
        # _host_agent_process_restart's docstring in cli_terminal.py.
        from app.routers.cli_terminal import _host_agent_process_restart
        return await _host_agent_process_restart(agent)


class GrokAdapter(_SingletonEnvDirMixin):
    """Grok Build CLI as a host harness (ADR-066, delivery model superseded by ADR-068).

    Like Hermes, grok runs as a persistent tmux TUI (session "grok") that the
    grok-bridge pastes dispatches into (ADR-068 — the v1 headless per-dispatch
    subprocess is retired; `-p` is banned fleet-wide). It talks ONLY to xAI
    cloud over its own OAuth. So `protocol` is the fixed "grok" wire protocol
    (harness_compat), and `build_agent_env` renders NO provider env — just the
    MC_* control-plane vars the grok-bridge needs to poll/heartbeat. The
    runtime binding is a display anchor only; grok reads its model/endpoint
    from its own cloud session. The bridge resets the TUI session (`/new`) on
    a genuine task switch — see ADR-068 Nachtrag 2026-07-12.

    reload() reuses the generic host lifecycle path (launchctl kickstart of the
    grok-bridge plist), which re-sources agent.env for the next dispatch.
    """

    harness = "grok"
    label = "Grok Build"
    protocol = "grok"
    singleton_slug = "grok"
    supports_bootstrap = True

    async def build_agent_env(self, agent, runtime, token, *, session):
        from app.services.agent_bootstrap import build_grok_agent_env
        return await build_grok_agent_env(runtime, token, session=session)

    async def bootstrap(self, session, agent, runtime):
        from app.services.agent_bootstrap import bootstrap_grok_agent
        return await bootstrap_grok_agent(session, agent, runtime)

    async def reload(self, agent):
        # Full process-level restart (see HermesAdapter.reload for why the
        # plain launchd kickstart is not enough) — harmless no-op extra work
        # for grok (no worker-session split), same strong guarantee for free.
        from app.routers.cli_terminal import _host_agent_process_restart
        return await _host_agent_process_restart(agent)


class KimiHostAdapter(_SingletonEnvDirMixin):
    """Kimi Code CLI as a host harness (2026-07-24, boss-host pattern).

    Kimi runs as a persistent tmux TUI (session "kimi-host", Window 0) with
    the SHARED docker/shared/poll.sh (Window 1) driving dispatch/messages via
    the TCK-pinned adapter libs — unlike grok/hermes there is no bespoke
    bridge script; the container and host paths share one adapter surface.

    Auth is OAuth FILES in the per-agent KIMI_CODE_HOME
    (~/.mc/agents/kimi/kimi-config) — protocol-fixed "kimi" like grok's
    "grok": build_agent_env renders only MC_* control-plane vars. The
    runtime binding (kimi-cloud seed) is a display anchor.

    NOTE: harness "kimi" exists in BOTH worlds (cli-bridge image AND host).
    This registry is only consulted on the host provisioning path
    (agent_runtime == "host"), so the dual use is safe — the cli-bridge path
    never calls get_adapter().
    """

    harness = "kimi"
    label = "Kimi Code"
    protocol = "kimi"
    singleton_slug = "kimi"
    supports_bootstrap = True

    async def build_agent_env(self, agent, runtime, token, *, session):
        from app.services.agent_bootstrap import build_kimi_agent_env
        return await build_kimi_agent_env(runtime, token, session=session)

    async def bootstrap(self, session, agent, runtime):
        from app.services.agent_bootstrap import bootstrap_kimi_agent
        return await bootstrap_kimi_agent(session, agent, runtime)

    async def reload(self, agent):
        # Full process-level restart — see HermesAdapter.reload.
        from app.routers.cli_terminal import _host_agent_process_restart
        return await _host_agent_process_restart(agent)


class ClaudeHostAdapter:
    """Native Claude Code CLI as a host harness (boss-host, 2026-07-25).

    WHY THIS EXISTS
    ---------------
    boss-host is the one host agent running the real `claude` binary, but it
    predates this registry: it was provisioned by hand (docker/boss-host/
    *.plist + entrypoint.sh). Because HOST_ADAPTERS had no "claude" entry, every
    propagation path gated on ``get_adapter(...) is not None`` skipped Boss — so
    NOTHING ever wrote ANTHROPIC_MODEL into ~/.mc/agents/boss-host/agent.env,
    and start-claude.sh had to pin a model literal by hand (which then rotted to
    claude-opus-4-8 while the account default had moved on). Registering the
    adapter closes that hole: ``runtime.model_identifier`` becomes the single
    truth for Boss exactly as it already is for hermes/grok/kimi.

    protocol "anthropic" → ``sync_host_agent_model`` runs ``build_runtime_env``,
    whose claude branch emits ANTHROPIC_MODEL (routers/internal.py). Absent
    model_identifier stays absent — no pin beats a stale pin.

    singleton_slug is None ON PURPOSE. Unlike hermes/grok/kimi the claude
    harness is not bound to one slug: host_provisioning.stage_host_agent_files
    already stages arbitrary wizard-created claude host agents into
    ~/.mc/agents/<slug>/. Only the legacy Boss row has a hand-made layout.

    supports_bootstrap is False: there is no build_claude_agent_env /
    bootstrap_claude_agent, and inventing one would REPLACE the working generic
    wizard path in routers/agents.py for every claude host agent. That endpoint
    therefore skips non-bootstrap adapters and falls through to
    stage_host_agent_files exactly as before this adapter existed.
    """

    harness = "claude"
    label = "Claude Code"
    protocol = "anthropic"
    singleton_slug = None
    supports_bootstrap = False

    # The legacy Boss row's on-disk directory is "boss-host", NOT its slug
    # "boss": docker/boss-host/{entrypoint,poll,start-claude}.sh all read
    # ~/.mc/agents/boss-host/agent.env. Writing to ~/.mc/agents/boss/ would
    # produce a file that nothing on the host ever sources — the sync would
    # look green and change nothing.
    _LEGACY_ENV_DIRS = {"boss": "boss-host"}

    def env_dir(self, agent: Agent) -> str:
        slug = (agent.slug or (agent.name or "").lower().replace(" ", "-")).strip()
        if not slug:
            raise ValueError(
                "claude host agent without slug/name — cannot locate its agent.env"
            )
        return self._LEGACY_ENV_DIRS.get(slug, slug)

    async def build_agent_env(self, agent, runtime, token, *, session):
        # No bespoke bootstrap exists (see class docstring), so this is not on
        # any live provisioning path today; it is implemented to satisfy the
        # adapter contract and to keep one description of what a claude host
        # agent.env contains. Shape mirrors host_provisioning.stage_host_agent_
        # files: MC_* control plane + whatever build_runtime_env decides
        # (ANTHROPIC_MODEL for an anthropic runtime).
        from app.config import settings
        from app.routers.internal import build_runtime_env

        env: dict[str, str] = {
            "MC_AGENT_TOKEN": token,
            "MC_API_URL": settings.mc_base_url.rstrip("/"),
        }
        env.update(await build_runtime_env(runtime, session, agent))
        return env

    async def bootstrap(self, session, agent, runtime):
        raise NotImplementedError(
            "harness 'claude' has no bespoke host bootstrap: boss-host was "
            "provisioned manually (docker/boss-host/) and wizard-created claude "
            "host agents are staged by host_provisioning.stage_host_agent_files. "
            "Callers must check supports_bootstrap before calling this."
        )

    async def reload(self, agent):
        # Full process-level restart — see HermesAdapter.reload.
        from app.routers.cli_terminal import _host_agent_process_restart
        return await _host_agent_process_restart(agent)


class _GenericStagedHostAdapter:
    """Shared body for host harnesses served by the GENERIC wizard staging path.

    claude/openclaude/omp all run as a plain binary in a tmux session that
    ``host_provisioning.stage_host_agent_files`` sets up (plist + run.sh +
    agent.env + poll.sh). They differ only in binary and provider env, both of
    which are already handled elsewhere (``_HARNESS_BINARY`` /
    ``build_runtime_env``), so the adapter body is identical:

      * ``supports_bootstrap = False`` — routers/agents.py::provision skips
        non-bootstrap adapters and falls through to the generic staging path.
        Inventing a bootstrap() here would REPLACE that working path.
      * ``singleton_slug = None`` — arbitrarily many may exist. Only the
        single-instance BRIDGES (hermes/grok/kimi) are pinned to one slug.
      * ``env_dir`` = the agent slug (the default staging layout).

    Being in HOST_ADAPTERS is what makes these harnesses (a) offered as host
    harnesses in the agent wizard, (b) switchable in place, and (c) reached by
    the runtime→agent model propagation. Before this, "openclaude"/"omp" host
    agents were creatable in principle (host_provisioning._HARNESS_BINARY knew
    them) but invisible in the wizard and permanently runtime-locked.
    """

    singleton_slug = None
    supports_bootstrap = False

    def env_dir(self, agent: Agent) -> str:
        slug = (agent.slug or (agent.name or "").lower().replace(" ", "-")).strip()
        if not slug:
            raise ValueError(
                f"{self.harness} host agent without slug/name "  # type: ignore[attr-defined]
                f"— cannot locate its agent.env"
            )
        return slug

    async def build_agent_env(self, agent, runtime, token, *, session):
        # Mirrors what stage_host_agent_files writes: MC_* control plane plus
        # whatever build_runtime_env decides for this harness. Not on a live
        # path (no bootstrap) — it exists to satisfy the adapter contract with
        # one description of the file's shape.
        from app.config import settings
        from app.routers.internal import build_runtime_env

        env: dict[str, str] = {
            "MC_AGENT_TOKEN": token,
            "MC_API_URL": settings.mc_base_url.rstrip("/"),
        }
        env.update(await build_runtime_env(runtime, session, agent))
        return env

    async def bootstrap(self, session, agent, runtime):
        raise NotImplementedError(
            f"harness {self.harness!r} has no bespoke host bootstrap: it is "  # type: ignore[attr-defined]
            f"staged by host_provisioning.stage_host_agent_files. Callers must "
            f"check supports_bootstrap before calling this."
        )

    async def reload(self, agent):
        # Full process-level restart — see HermesAdapter.reload.
        from app.routers.cli_terminal import _host_agent_process_restart
        return await _host_agent_process_restart(agent)


class OpenClaudeHostAdapter(_GenericStagedHostAdapter):
    """OpenClaude as a host harness (2026-07-28).

    protocol "openai": build_runtime_env's openclaude branch emits
    OPENAI_BASE_URL + OPENAI_MODEL, run.sh sources agent.env and exports both
    into the process — the same contract the cli-bridge openclaude container
    uses, so nothing harness-specific has to be staged.
    """

    harness = "openclaude"
    label = "OpenClaude"
    protocol = "openai"


class OmpHostAdapter(_GenericStagedHostAdapter):
    """omp as a host harness (2026-07-28).

    protocol "openai": build_runtime_env's omp branch emits OPENAI_BASE_URL +
    OPENAI_MODEL plus the omp sizing vars (OMP_CONTEXT_WINDOW / OMP_MAX_TOKENS,
    and OMP_TURN_IDLE_TIMEOUT for slow local runtimes).

    Unlike openclaude, omp does NOT resolve a served model from
    OPENAI_BASE_URL — it needs its own models.yml (docker/omp-bridge/
    entrypoint.sh §2 calls that file mandatory). The container renders it from
    exactly these env vars; the host path renders the same file into a
    per-agent omp PROFILE via host_provisioning.render_omp_host_models_yml, so
    both worlds are fed by one runtime row. OMP_TURN_IDLE_TIMEOUT is inert on
    the host (it belongs to docker/omp-bridge/bridge.py's watchdog, which has
    no host counterpart) — harmless, and kept rather than special-cased so
    build_runtime_env stays the one place that describes an omp binding.
    """

    harness = "omp"
    label = "omp"
    protocol = "openai"


HOST_ADAPTERS: dict[str, "HostHarnessAdapter"] = {
    "hermes": HermesAdapter(),
    "grok": GrokAdapter(),
    "kimi": KimiHostAdapter(),
    "claude": ClaudeHostAdapter(),
    "openclaude": OpenClaudeHostAdapter(),
    "omp": OmpHostAdapter(),
}

# INVARIANT (asserted in tests/test_host_harness_catalog.py): every cli-bridge
# harness in harness_compat.HARNESSES must also appear above, so any CLI type
# can be created BOTH as a container and as a host agent. The reverse does not
# hold — hermes/grok are host-only bridges with no cli-bridge form.


def get_adapter(harness: str | None) -> "HostHarnessAdapter | None":
    if not harness:
        return None
    return HOST_ADAPTERS.get(harness)


# ── Runtime-switch eligibility — THE single source of truth ────────────────
#
# Everything that needs to answer "can MC switch this agent's runtime?" MUST
# call `runtime_switch_availability` (or the `is_host_inplace` shorthand):
#
#   * services/agent_runtime_switch.py — the switch endpoint's own guard.
#   * models/agent.py — the derived `runtime_switchable` /
#     `runtime_switch_blocked_reason` fields the API serialises.
#   * frontend-v2 — reads those fields, NEVER re-derives the rule.
#
# The rule used to be re-implemented in the frontend against a hardcoded
# `harness === "hermes"`, which silently locked every host harness added to
# HOST_ADAPTERS afterwards (grok, kimi, claude/Boss) out of the UI even though
# the backend had supported them for weeks. Adding a new host adapter must be
# the ONLY edit needed to make that harness switchable end to end.

def is_host_inplace(agent: Agent) -> bool:
    """True when this is a host agent that owns a HostHarnessAdapter.

    Such agents (ADR-064) switch runtime in place — the adapter re-renders
    agent.env + reloads the single host session sequentially, so there is never
    a parallel instance.
    """
    return (
        getattr(agent, "agent_runtime", None) == "host"
        and get_adapter(getattr(agent, "harness", None)) is not None
    )


def runtime_switch_availability(agent: Agent) -> tuple[bool, str | None]:
    """Can MC switch this agent's runtime, and if not — why not (plain text)?

    Returns ``(switchable, blocked_reason)``. ``blocked_reason`` is None exactly
    when ``switchable`` is True, and is user-facing English otherwise (it is
    both the API's `runtime_switch_blocked_reason` and the message of the
    AgentNotSwitchableError the switch endpoint raises).
    """
    agent_runtime = getattr(agent, "agent_runtime", None)

    if agent_runtime == "cli-bridge":
        return True, None

    if agent_runtime == "host":
        if get_adapter(getattr(agent, "harness", None)) is not None:
            return True, None
        harness = getattr(agent, "harness", None) or "none"
        supported = ", ".join(sorted(HOST_ADAPTERS))
        return False, (
            f"Host agent with harness '{harness}' has no host adapter, so its "
            f"runtime is managed outside Mission Control (launchd on the Mac). "
            f"Switchable are cli-bridge agents and host agents on one of these "
            f"harnesses: {supported}."
        )

    return False, (
        f"Runtime switch is not supported for agent runtime "
        f"'{agent_runtime or 'unknown'}'. Only cli-bridge agents and host "
        f"agents with a host adapter can pick a runtime in Mission Control."
    )


def host_harness_catalog() -> list[dict[str, Any]]:
    """The host-harness registry, rendered for the agent-creation wizard.

    Same principle as `runtime_switch_availability`: the registry answers,
    the UI asks. The wizard previously carried its OWN list of host harnesses
    (hermes/grok/kimi) plus its own protocol map — so `claude` never appeared
    as a host harness at all, and every host harness was assumed to be a
    singleton bridge. `claude` is deliberately NOT a singleton
    (``singleton_slug is None``): host_provisioning.stage_host_agent_files
    stages arbitrary claude host agents, so a second one is legitimate.

    Keys of the returned dicts:
      key            — the harness value written to agents.harness
      label          — display name for the picker
      protocol       — wire protocol, for filtering compatible runtimes
      singleton      — True → at most ONE agent may hold this harness
      singleton_slug — the slug that agent must have (None when not singleton)
      supports_bootstrap — False → provisioning uses the generic host staging
                           path rather than the adapter's own bootstrap()
    """
    return [
        {
            "key": key,
            "label": getattr(adapter, "label", key),
            "protocol": adapter.protocol,
            "singleton": getattr(adapter, "singleton_slug", None) is not None,
            "singleton_slug": getattr(adapter, "singleton_slug", None),
            "supports_bootstrap": getattr(adapter, "supports_bootstrap", True),
        }
        for key, adapter in HOST_ADAPTERS.items()
    ]


async def sync_host_agent_model(agent: Agent, runtime: Runtime, *, session: AsyncSession) -> None:
    """Rewrite the provider model env in the host agent's agent.env from the runtime binding.

    Preserves MC_AGENT_TOKEN and any other existing keys (a model-drift sync must
    never regenerate the auth token). ADR-064.

    Writes OPENAI_BASE_URL/OPENAI_MODEL for openai-protocol hosts (hermes) and
    ANTHROPIC_MODEL for anthropic-protocol hosts (boss-host) — see
    build_runtime_env, which is the single place that decides the shape.
    """
    from app.routers.internal import build_runtime_env
    from app.services.agent_bootstrap import _format_env_file, _unquote_env_value, _home_host
    from app.services.harness_compat import derive_harness, runtime_protocol

    # grok (xAI cloud OAuth, ADR-066) and kimi (OAuth credential files) are
    # protocol-fixed: their runtime binding is a display anchor only, so there
    # is no provider model env to sync and build_runtime_env would wrongly
    # derive one from that anchor. anthropic IS syncable — host claude reads
    # ANTHROPIC_MODEL from agent.env (see build_runtime_env's claude branch).
    if runtime_protocol(runtime) not in ("openai", "anthropic", None):
        return

    # The on-disk directory is the adapter's business, not always the slug: the
    # legacy boss-host layout uses "boss-host" for the agent whose slug is
    # "boss" (ClaudeHostAdapter.env_dir). Falling back to the raw slug keeps
    # unknown/unregistered harnesses behaving as before.
    adapter = get_adapter(agent.harness or derive_harness(runtime))
    slug = adapter.env_dir(agent) if adapter is not None else (agent.slug or "hermes")
    env_path = _home_host() / ".mc" / "agents" / slug / "agent.env"
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, val = line.partition("=")
                # Reverse _format_env_file's escaping exactly — a naive
                # .strip("'") leaves '"'"' sequences that re-escape and grow
                # ~3× on every model-drift sync (13 KB token corruption).
                existing[key.strip()] = _unquote_env_value(val)
    # Pass the agent so an explicitly set agent.harness wins over
    # derive_harness(runtime) — that is what decides ANTHROPIC_MODEL vs
    # OPENAI_MODEL inside build_runtime_env.
    existing.update(await build_runtime_env(runtime, session, agent))

    # omp reads its model from models.yml, not from OPENAI_* — rewriting only
    # agent.env would leave a host omp agent pointed at the PREVIOUS runtime
    # while MC reports the new one (exactly the drift this sync exists to
    # prevent). OMP_PROFILE goes in before the write so a row staged before
    # this existed gets it retro-fitted too.
    is_host_omp = adapter is not None and getattr(adapter, "harness", None) == "omp"
    if is_host_omp:
        from app.services.host_provisioning import omp_host_profile

        existing.setdefault("OMP_PROFILE", omp_host_profile(slug))

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_format_env_file(existing))

    if is_host_omp:
        from app.services.host_provisioning import render_omp_host_models_yml

        render_omp_host_models_yml(slug, existing)
