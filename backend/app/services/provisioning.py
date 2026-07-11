"""Agent Provisioning — runtime-aware (post-gateway-sunset, Phase 29).

Before Phase 29: provisioning.py orchestrated the gateway push (config.patch +
agents.files.set + sessions.reset). With the OpenClaw sunset (D-11), that
entire path is gone. provision_agent_background() now delegates
runtime-aware:

- agent_runtime == "cli-bridge": re-render compose + sync docker files +
  set status to 'provisioned'.
- agent_runtime == "host": Boss-on-host runs via launchd, no provisioning
  needed — we just mark it 'provisioned'.
- Other runtimes: warning + status 'local'.

The former gateway sync helpers (skills and model push) and the inline
RPC calls have been completely removed.

Phase 30: cleanup_sync_ghosts was deleted — its only consumer was the
gateway startup sync (since removed), and post-Phase-30 gateway_agent_id
no longer exists on the agent model.

convert_model_to_oc_format is still exported with a TODO-Phase-31 note
because routers/agents.py:1781 still imports it; plan 29-05 will clean this up.
"""

import logging
import re
import uuid

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Subset that used to be pushed to the gateway. Still used by routers/agents.py
# (TOOLS.md / SOUL.md / HEARTBEAT.md / MEMORY.md on the disk layer);
# plan 29-05 will clean up this constant once the gateway push paths there
# are fully removed.
GATEWAY_SYNC_FILE_TYPES = {"tools_md", "identity_md", "soul_md", "memory_md"}

# Mapping of MC field names → file names on disk (cli-bridge uses these names
# for the rendered files under ~/.mc/agents/{slug}/agent/).
# heartbeat_md removed in migration 0125 — was never read by agents.
OC_FILENAME_MAP = {
    "soul_md": "SOUL.md",
    "tools_md": "TOOLS.md",
    "identity_md": "IDENTITY.md",
    "memory_md": "MEMORY.md",
}


# ── Helper functions ─────────────────────────────────────────────────────────

def convert_model_to_oc_format(model: str | None) -> str:
    """MC model format → OpenClaw provider/model format.

    glm-5:cloud           → ollama-cloud/glm-5
    minimax-m2.5:cloud    → ollama-cloud/minimax-m2.5
    qwen3.5:397b-cloud    → ollama-cloud/qwen3.5:397b
    openai/gpt-4          → openai/gpt-4  (unchanged)

    TODO Phase 31: once the last gateway-specific model names in
    routers/agents.py are cleaned up, this function can be removed outright.
    """
    if not model:
        return "ollama-cloud/minimax-m2.5"
    if ":" in model and "/" not in model:
        name, tag = model.rsplit(":", 1)
        if tag == "cloud":
            return f"ollama-cloud/{name}"
        if tag.endswith("-cloud"):
            version = tag[: -len("-cloud")]
            return f"ollama-cloud/{name}:{version}"
    return model


def extract_token_from_tools_md(tools_md: str) -> str | None:
    """Extracts the bearer token from an existing TOOLS.md."""
    match = re.search(r"Authorization: Bearer ([A-Za-z0-9_\-]+)", tools_md)
    return match.group(1) if match else None


# ── Provisioning (D-11: runtime-aware) ─────────────────────────────────────────

async def provision_agent_background(agent_id: uuid.UUID) -> None:
    """Provisions an agent runtime-aware (D-11).

    cli-bridge → write_compose_agents + sync_docker_agent_files + mark provisioned
    host       → mark provisioned (Boss-on-host boots via launchd)
    other      → warn + mark provision_status='local'

    Commits exactly once per branch (D-15: no try/except around SQL). Failures
    propagate to the BackgroundTask caller, which logs them.
    """
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        agent = await session.get(Agent, agent_id)
        if not agent:
            logger.error("provision_agent_background: Agent %s nicht gefunden", agent_id)
            return

        runtime = getattr(agent, "agent_runtime", "cli-bridge") or "cli-bridge"

        if runtime == "cli-bridge":
            from app.services.compose_renderer import write_compose_agents
            from app.services.docker_agent_sync import (
                ensure_agent_container_started,
                sync_docker_agent_files,
            )

            if not agent.workspace_path:
                # Migration 0087 was a ONE-TIME backfill (~/.mc/workspaces/<slug>
                # for every cli-bridge agent that existed at that time). It never
                # became an ongoing invariant — the only code path that ever set
                # `workspace_path` afterwards was the dead legacy Free-Code-Bridge
                # setter (`provision_cli_agent` in routers/cli_terminal.py,
                # hardcoded to ~/FreeCode/projects). Any cli-bridge agent created
                # or reset after 0087 got workspace_path=NULL forever and hard-
                # failed on first dispatch (`_resolve_workspace()` in
                # cli_bridge_runner.py) — this happened to the "Installer" agent.
                # Assign the same convention deterministically here so it's an
                # invariant of provisioning, not a one-off migration side effect.
                # Slug must match migration 0087's SQL exactly — see
                # `slugify_project()` (git_service.py), which the migration's
                # regexp_replace mirrors.
                from app.config import settings
                from app.services.git_service import slugify_project

                agent.workspace_path = (
                    f"{settings.home_host}/.mc/workspaces/{slugify_project(agent.name)}"
                )
                session.add(agent)
                await session.commit()
                await session.refresh(agent)

            await write_compose_agents(session)
            sync_results = await sync_docker_agent_files(session, agent)

            if "_error" in sync_results:
                # No silent fail (OSS fresh-install path): right after
                # template instantiation, ~/.mc/agents/{slug}/claude-config/
                # doesn't exist yet — only the provision step (cli-bridge
                # host helper) creates it. The agent is then NOT provisioned;
                # honestly leave status at 'local' instead of falsely
                # reporting 'provisioned' (otherwise ProvisionBadge would show
                # "Live" without files/container).
                logger.warning(
                    "provision_agent_background(%s): file-sync failed — %s",
                    agent.name, sync_results["_error"],
                )
                agent.provision_status = "local"
                agent.updated_at = utcnow()
                session.add(agent)
                await session.commit()
                await emit_event(
                    session,
                    "agent.provision_failed",
                    f"{agent.name}: Config-Files noch nicht auf Disk "
                    f"({sync_results['_error']}) — auf der Agent-Seite "
                    "«Provision» ausfuehren (cli-bridge Host-Helper noetig, "
                    "siehe docs/setup/first-agent.md).",
                    severity="warning",
                    agent_id=agent.id,
                    board_id=agent.board_id,
                )
                return

            # Autostart: files+compose are in place — bring up the container so
            # one-click-deploy really ends with a running agent instead of
            # waiting on a runtime switch or start-all.sh. If the container is
            # already running (re-provision), it's deliberately left untouched.
            start_result = ensure_agent_container_started(agent)
            start_status = start_result.get("status", "")

            if start_status.startswith("error"):
                logger.warning(
                    "provision_agent_background(%s): container start failed — %s",
                    agent.name, start_status,
                )
                agent.provision_status = "error"
                agent.updated_at = utcnow()
                session.add(agent)
                await session.commit()
                await emit_event(
                    session,
                    "agent.provision_failed",
                    f"{agent.name}: Config-Files ok, aber Container-Start "
                    f"fehlgeschlagen ({start_status}) — docker logs "
                    f"{start_result.get('container') or 'mc-agent-<slug>'} prüfen.",
                    severity="warning",
                    agent_id=agent.id,
                    board_id=agent.board_id,
                )
                return

            agent.provision_status = "provisioned"
            agent.provisioned_at = utcnow()
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()

            await emit_event(
                session,
                "agent.provisioned",
                f"{agent.name} (cli-bridge) provisioned — Container: {start_status}",
                severity="info",
                agent_id=agent.id,
                board_id=agent.board_id,
            )
            return

        if runtime == "host":
            agent.provision_status = "provisioned"
            agent.provisioned_at = utcnow()
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()

            await emit_event(
                session,
                "agent.provisioned",
                f"{agent.name} (host) provisioned (no-op)",
                severity="info",
                agent_id=agent.id,
                board_id=agent.board_id,
            )
            return

        # Other runtimes: not provisionable via this service.
        logger.warning(
            "Agent %s has unsupported runtime '%s' for provisioning — marking 'local'",
            agent_id, runtime,
        )
        agent.provision_status = "local"
        agent.updated_at = utcnow()
        session.add(agent)
        await session.commit()
        await emit_event(
            session,
            "agent.created",
            f"{agent.name} erstellt (runtime '{runtime}' nicht provisionierbar — manuelles Setup noetig)",
            severity="warning",
            agent_id=agent.id,
            board_id=agent.board_id,
        )
