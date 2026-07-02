"""Agent-Provisioning — runtime-aware (post-Gateway-Sunset, Phase 29).

Vor Phase 29: provisioning.py orchestrierte den Gateway-Push (config.patch +
agents.files.set + sessions.reset). Mit dem OpenClaw-Sunset (D-11) entfaellt
dieser komplette Pfad. provision_agent_background() delegiert jetzt
runtime-aware:

- agent_runtime == "cli-bridge": Compose neu rendern + Docker-Files syncen +
  Status auf 'provisioned' setzen.
- agent_runtime == "host": Boss-on-host laeuft via launchd, kein Provisioning
  noetig — wir markieren nur 'provisioned'.
- Sonstige Runtimes: Warnung + Status 'local'.

Die ehemaligen Gateway-Sync-Helper (Skills- und Model-Push) und die Inline-
RPC-Aufrufe sind komplett entfernt.

Phase 30: cleanup_sync_ghosts wurde geloescht — der einzige Konsument war der
inzwischen entfernte Gateway-Startup-Sync, und post-Phase-30 existiert
gateway_agent_id auf dem Agent-Model nicht mehr.

convert_model_to_oc_format ist mit TODO-Phase-31-Hinweis weiterhin exportiert,
weil routers/agents.py:1781 ihn noch importiert; Plan 29-05 raeumt das auf.
"""

import logging
import re
import uuid

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger(__name__)

# ── Konstanten ─────────────────────────────────────────────────────────────────

# Subset das ehemals auf den Gateway gepusht wurde. Wird von routers/agents.py
# noch verwendet (TOOLS.md / SOUL.md / HEARTBEAT.md / MEMORY.md auf Disk-Layer);
# Plan 29-05 raeumt diese Konstante mit auf wenn die Gateway-Push-Pfade dort
# vollstaendig entfernt sind.
GATEWAY_SYNC_FILE_TYPES = {"tools_md", "identity_md", "soul_md", "memory_md"}

# Mapping MC field names → File names auf Disk (cli-bridge nutzt diese Namen
# fuer die gerenderten Dateien unter ~/.mc/agents/{slug}/agent/).
# heartbeat_md removed in migration 0125 — was never read by agents.
OC_FILENAME_MAP = {
    "soul_md": "SOUL.md",
    "tools_md": "TOOLS.md",
    "identity_md": "IDENTITY.md",
    "memory_md": "MEMORY.md",
}


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def convert_model_to_oc_format(model: str | None) -> str:
    """MC model format → OpenClaw provider/model format.

    glm-5:cloud           → ollama-cloud/glm-5
    minimax-m2.5:cloud    → ollama-cloud/minimax-m2.5
    qwen3.5:397b-cloud    → ollama-cloud/qwen3.5:397b
    openai/gpt-4          → openai/gpt-4  (unveraendert)

    TODO Phase 31: nach Cleanup der letzten Gateway-spezifischen Modelnamen
    in routers/agents.py kann diese Funktion ersatzlos entfallen.
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
    """Extrahiert den Bearer-Token aus einer bestehenden TOOLS.md."""
    match = re.search(r"Authorization: Bearer ([A-Za-z0-9_\-]+)", tools_md)
    return match.group(1) if match else None


# ── Provisioning (D-11: runtime-aware) ─────────────────────────────────────────

async def provision_agent_background(agent_id: uuid.UUID) -> None:
    """Provisioniert Agent runtime-aware (D-11).

    cli-bridge → write_compose_agents + sync_docker_agent_files + mark provisioned
    host       → mark provisioned (Boss-on-host bootet via launchd)
    other      → warn + mark provision_status='local'

    Commits exactly once per branch (D-15: kein try/except um SQL). Failures
    propagate to the BackgroundTask-Caller, der sie loggt.
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

            await write_compose_agents(session)
            sync_results = await sync_docker_agent_files(session, agent)

            if "_error" in sync_results:
                # Kein Silent-Fail (OSS Fresh-Install-Pfad): direkt nach
                # Template-Instantiate existiert ~/.mc/agents/{slug}/claude-config/
                # noch nicht — das legt erst der Provision-Schritt (cli-bridge
                # Host-Helper) an. Der Agent ist dann NICHT provisioniert;
                # Status ehrlich auf 'local' lassen statt faelschlich
                # 'provisioned' zu melden (ProvisionBadge wuerde sonst "Live"
                # zeigen ohne Files/Container).
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

            # Autostart: Files+Compose liegen — Container hochbringen, damit
            # One-Click-Deploy wirklich in einem laufenden Agent endet statt
            # auf einen Runtime-Switch oder start-all.sh zu warten. Läuft der
            # Container schon (Re-Provision), wird er bewusst nicht angefasst.
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

        # Andere Runtimes: nicht provisionierbar via diesem Service.
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
