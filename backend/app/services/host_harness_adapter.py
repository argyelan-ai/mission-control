"""Host-harness adapters (ADR-060).

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
    protocol: str  # "openai" | "anthropic"

    async def build_agent_env(
        self, agent: Agent, runtime: Runtime, token: str, *, session: AsyncSession
    ) -> dict[str, str]: ...

    async def bootstrap(
        self, session: AsyncSession, agent: Agent, runtime: Runtime
    ) -> dict[str, Any]: ...

    async def reload(self, agent: Agent) -> dict[str, Any]: ...


class HermesAdapter:
    harness = "hermes"
    protocol = "openai"

    async def build_agent_env(self, agent, runtime, token, *, session):
        from app.services.agent_bootstrap import build_hermes_agent_env
        return await build_hermes_agent_env(runtime, token, session=session)

    async def bootstrap(self, session, agent, runtime):
        from app.services.agent_bootstrap import bootstrap_hermes_agent
        return await bootstrap_hermes_agent(session, agent, runtime)

    async def reload(self, agent):
        # Reuse the host lifecycle path (SSH -> hermes-bridge /restart).
        # NOTE: `_host_agent_lifecycle` takes the Agent object itself (not a
        # slug string) — it derives the slug internally via
        # `agent.name.lower().replace(" ", "-")`. Brief assumed `(slug, action)`;
        # adapted here to match the real signature found in cli_terminal.py.
        from app.routers.cli_terminal import _host_agent_lifecycle
        return await _host_agent_lifecycle(agent, "restart")


class GrokAdapter:
    """Grok Build CLI as a host harness (ADR-066).

    Unlike Hermes (a persistent tmux TUI bound to a vLLM runtime), grok is a
    headless per-dispatch subprocess that talks ONLY to xAI cloud over its own
    OAuth. So `protocol` is the fixed "grok" wire protocol (harness_compat), and
    `build_agent_env` renders NO provider env — just the MC_* control-plane vars
    the grok-bridge needs to poll/heartbeat. The runtime binding is a display
    anchor only; grok reads its model/endpoint from its own cloud session.

    reload() reuses the generic host lifecycle path (launchctl kickstart of the
    grok-bridge plist). grok has no persistent LLM session to kill — restarting
    the bridge re-sources agent.env for the next dispatch, which IS the reload.
    """

    harness = "grok"
    protocol = "grok"

    async def build_agent_env(self, agent, runtime, token, *, session):
        from app.services.agent_bootstrap import build_grok_agent_env
        return await build_grok_agent_env(runtime, token, session=session)

    async def bootstrap(self, session, agent, runtime):
        from app.services.agent_bootstrap import bootstrap_grok_agent
        return await bootstrap_grok_agent(session, agent, runtime)

    async def reload(self, agent):
        from app.routers.cli_terminal import _host_agent_lifecycle
        return await _host_agent_lifecycle(agent, "restart")


HOST_ADAPTERS: dict[str, "HostHarnessAdapter"] = {
    "hermes": HermesAdapter(),
    "grok": GrokAdapter(),
}


def get_adapter(harness: str | None) -> "HostHarnessAdapter | None":
    if not harness:
        return None
    return HOST_ADAPTERS.get(harness)


async def sync_host_agent_model(agent: Agent, runtime: Runtime, *, session: AsyncSession) -> None:
    """Rewrite only OPENAI_* in the host agent's agent.env from the runtime binding.

    Preserves MC_AGENT_TOKEN and any other existing keys (a model-drift sync must
    never regenerate the auth token). ADR-060.
    """
    from app.routers.internal import build_runtime_env
    from app.services.agent_bootstrap import _format_env_file, _unquote_env_value, _home_host
    from app.services.harness_compat import runtime_protocol

    # Protocol-fixed host harnesses (grok → xAI cloud OAuth, ADR-066) have no
    # OPENAI_* provider env to sync — build_runtime_env would wrongly derive
    # OPENAI_BASE_URL/OPENAI_MODEL from the display-anchor runtime. Nothing to do.
    if runtime_protocol(runtime) not in ("openai", None):
        return

    slug = agent.slug or "hermes"
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
    existing.update(await build_runtime_env(runtime, session))
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_format_env_file(existing))
