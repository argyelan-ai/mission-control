"""Agent archive/restore lifecycle orchestration (2026-07-14).

Two-stage lifecycle: Archive (reversible — stops the runtime, keeps DB+files+
token) must precede Delete (hard, in routers/agents.py). Archive/restore are
runtime-aware: host agents load/unload their launchd job, cli-bridge agents
stop/start their Docker container, manual agents have no managed process.

The `archived_at` timestamp on Agent is the single source of truth. The runtime
stop is best-effort — a stop failure logs a warning but the flag is still set,
because a hung container is hard-removed at Delete anyway.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.services import agent_bootstrap
from app.services import docker_agent_sync
from app.utils import utcnow

logger = logging.getLogger(__name__)

try:
    from app.services.agent_runtime_switch import AgentBusyError
except Exception:  # pragma: no cover — fallback if import cycle
    class AgentBusyError(Exception):
        """Raised when an agent cannot be archived because it is mid-task."""

        def __init__(self, message, *, current_task_id=None):
            super().__init__(message)
            self.current_task_id = current_task_id


class SingletonAgentError(Exception):
    """Raised when archive/restore/delete is attempted on a singleton host bridge.

    boss/hermes/grok are bound to hardcoded launchd jobs (com.openclaw.boss /
    com.mc.hermes-bridge / com.mc.grok-bridge) managed by the Runtime Cockpit —
    NOT the generic com.mc.agent.<slug> job the archive/restore/delete path
    would target. A generic agent that merely uses harness=hermes (e.g. a
    throwaway agent) is unaffected — this only fires for agent_runtime=="host"
    with slug in {boss, hermes, grok}.
    """


_SINGLETON_BRIDGE_SLUGS = {"boss", "hermes", "grok"}


def _is_singleton_bridge(agent: Agent) -> bool:
    return (
        getattr(agent, "agent_runtime", None) == "host"
        and (agent.slug or "").lower() in _SINGLETON_BRIDGE_SLUGS
    )


def _host_agent_plist_label(agent: Agent) -> str:
    """Label of a generic (wizard-provisioned) host agent's launchd job.

    Matches host_provisioning.stage_host_agent_files: label = com.mc.agent.<slug>.
    Singleton bridges (hermes/grok) use different hardcoded labels and are out
    of scope for archive/restore.
    """
    slug = (agent.slug or (agent.name or "").lower().replace(" ", "-"))
    return f"com.mc.agent.{slug}"


def _host_agent_plist_path(agent: Agent) -> Path:
    """Staged plist path: ~/.mc/agents/<slug>/com.mc.agent.<slug>.plist.

    Matches host_provisioning.stage_host_agent_files's workspace layout.
    """
    from app.config import settings
    slug = (agent.slug or (agent.name or "").lower().replace(" ", "-"))
    label = f"com.mc.agent.{slug}"
    return Path(settings.home_host) / ".mc" / "agents" / slug / f"{label}.plist"


def _is_busy(agent: Agent) -> bool:
    return agent.current_task_id is not None


async def archive_agent(session: AsyncSession, agent: Agent) -> Agent:
    """Stop the agent's runtime and mark it archived. Reversible via restore_agent.

    Raises AgentBusyError (→ 409 at the router) if a task is in progress.
    Idempotent: an already-archived agent is a no-op. The stop step is
    best-effort — failure logs a warning but still sets archived_at.
    """
    if _is_singleton_bridge(agent):
        raise SingletonAgentError(
            f"{agent.slug} ist eine Singleton-Host-Bridge und wird über launchd/das Runtime-Cockpit verwaltet, nicht über Archive/Delete"
        )
    if agent.archived_at is not None:
        return agent  # idempotent no-op
    if _is_busy(agent):
        raise AgentBusyError(
            "Agent arbeitet gerade an einem Task — erst abschließen oder umhängen, dann archivieren",
            current_task_id=agent.current_task_id,
        )

    runtime = getattr(agent, "agent_runtime", None)
    try:
        if runtime == "host":
            agent_bootstrap._run_launchctl_bootout(_host_agent_plist_label(agent))
        elif runtime == "cli-bridge":
            docker_agent_sync.stop_docker_agent_container(agent)
        # manual: nothing to stop
    except Exception as e:  # noqa: BLE001 — best-effort; flag is the truth
        logger.warning("archive stop for %s failed (flag set anyway): %s", agent.name, e)

    agent.archived_at = utcnow()
    agent.status = "offline"
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    logger.info("Agent %s archived", agent.name)
    return agent


async def restore_agent(session: AsyncSession, agent: Agent) -> Agent:
    """Clear archived state and bring the runtime back up. Reverse of archive_agent.

    Idempotent: a non-archived agent is a no-op. The start step is best-effort.
    """
    if _is_singleton_bridge(agent):
        raise SingletonAgentError(
            f"{agent.slug} ist eine Singleton-Host-Bridge und wird über launchd/das Runtime-Cockpit verwaltet, nicht über Archive/Delete"
        )
    if agent.archived_at is None:
        return agent  # idempotent no-op

    runtime = getattr(agent, "agent_runtime", None)
    try:
        if runtime == "host":
            agent_bootstrap._run_launchctl_bootstrap(_host_agent_plist_path(agent))
        elif runtime == "cli-bridge":
            docker_agent_sync.start_docker_agent_container(agent)
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("restore start for %s failed (flag cleared anyway): %s", agent.name, e)

    agent.archived_at = None
    agent.status = "offline"
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    logger.info("Agent %s restored", agent.name)
    return agent
