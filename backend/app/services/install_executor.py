"""Execute install/uninstall after approval.

This service calls Service-Layer-functions directly (docker_agent_sync,
skills filesystem ops). It does NOT call HTTP-endpoints — those require
user-auth, and bypassing them would create an auth-hole. The privileged
install path is its own boundary.

Dispatch table:
  "install_skill"   -> _install_skill()
  "uninstall_skill" -> _uninstall_skill()

Plugin handlers are added in Task 5.
"""
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.install_log import InstallLog

logger = logging.getLogger(__name__)


@dataclass
class InstallResult:
    result: str  # "success" | "failed" | "rolled_back"
    error: str | None = None
    installed_version: str | None = None
    install_log_id: uuid.UUID | None = None


class InstallExecutor:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def execute(self, approval: Approval) -> InstallResult:
        """Dispatch based on action_type. Writes InstallLog, returns result."""
        action = approval.action_type
        if action not in _HANDLERS:
            raise ValueError(f"Unsupported action_type: {action!r}")
        handler = _HANDLERS[action]
        try:
            return await handler(self, approval)
        except Exception as e:
            logger.exception("Install-Executor failed for approval %s", approval.id)
            log = await self._write_log(approval, result="failed", error=str(e))
            return InstallResult(result="failed", error=str(e), install_log_id=log.id)

    # ───────────────────── Skill handlers ─────────────────────

    async def _install_skill(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]
        source = payload.get("source", "")

        agent = await self._get_agent(target_id)
        previous_state = {"cli_skills": list(agent.cli_skills) if agent.cli_skills else None}

        # Append name to cli_skills allowlist (or leave as None if already None=all)
        current = list(agent.cli_skills) if agent.cli_skills is not None else []
        if name not in current:
            current.append(name)
        agent.cli_skills = current
        await self.session.commit()

        try:
            result = await _call_skill_install(source, name)
            installed_version = result.get("installed_version") if result else None
            await _trigger_sync_config(agent.id)
        except Exception as e:
            # Rollback
            agent.cli_skills = previous_state["cli_skills"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        log = await self._write_log(
            approval, result="success",
            installed_version=installed_version,
            previous_state=previous_state,
        )
        return InstallResult(result="success", installed_version=installed_version,
                             install_log_id=log.id)

    async def _uninstall_skill(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]

        agent = await self._get_agent(target_id)
        previous_state = {"cli_skills": list(agent.cli_skills) if agent.cli_skills else None}

        current = list(agent.cli_skills) if agent.cli_skills is not None else []
        agent.cli_skills = [s for s in current if s != name]
        await self.session.commit()

        try:
            await _trigger_sync_config(agent.id)
        except Exception as e:
            agent.cli_skills = previous_state["cli_skills"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        log = await self._write_log(
            approval, result="success", previous_state=previous_state,
        )
        return InstallResult(result="success", install_log_id=log.id)

    # ───────────────────── Plugin handlers ─────────────────────

    async def _install_plugin(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]
        source = payload.get("source", "")

        agent = await self._get_agent(target_id)
        previous_state = {"cli_plugins": list(agent.cli_plugins) if agent.cli_plugins else None}

        current = list(agent.cli_plugins) if agent.cli_plugins is not None else []
        if name not in current:
            current.append(name)
        agent.cli_plugins = current
        await self.session.commit()

        try:
            result = await _call_plugin_install(source, name)
            installed_version = result.get("installed_version") if result else None
            await _trigger_sync_config(agent.id)
        except Exception as e:
            agent.cli_plugins = previous_state["cli_plugins"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        log = await self._write_log(
            approval, result="success",
            installed_version=installed_version,
            previous_state=previous_state,
        )
        return InstallResult(result="success", installed_version=installed_version,
                             install_log_id=log.id)

    async def _uninstall_plugin(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]

        agent = await self._get_agent(target_id)
        previous_state = {"cli_plugins": list(agent.cli_plugins) if agent.cli_plugins else None}

        current = list(agent.cli_plugins) if agent.cli_plugins is not None else []
        agent.cli_plugins = [p for p in current if p != name]
        await self.session.commit()

        try:
            await _trigger_sync_config(agent.id)
        except Exception as e:
            agent.cli_plugins = previous_state["cli_plugins"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        log = await self._write_log(
            approval, result="success", previous_state=previous_state,
        )
        return InstallResult(result="success", install_log_id=log.id)

    # ───────────────────── MCP handlers ─────────────────────

    async def _install_mcp(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]
        source = payload.get("source", "")

        agent = await self._get_agent(target_id)
        previous_state = {
            "mcp_servers": list(agent.mcp_servers) if agent.mcp_servers else None
        }

        current = list(agent.mcp_servers) if agent.mcp_servers is not None else []
        if name not in current:
            current.append(name)
        agent.mcp_servers = current
        await self.session.commit()

        proposed_config = payload.get("proposed_config")
        try:
            install_info = await _call_mcp_install(source, name, proposed_config)
            installed_version = install_info.get("installed_version") if install_info else None
            ok = await _call_mcp_smoke_test(name)
            if not ok:
                raise RuntimeError(
                    f"MCP smoke test failed for {name!r} — server did not respond "
                    "to JSON-RPC initialize/tools-list. For Python MCPs this "
                    "usually means runtime dependencies aren't installed (check "
                    "requirements.txt / pyproject.toml) or required env vars "
                    "(e.g. API keys) are missing."
                )
            await _trigger_sync_config(agent.id)
        except Exception as e:
            agent.mcp_servers = previous_state["mcp_servers"]
            await self.session.commit()
            try:
                await _call_mcp_uninstall(name)
            except Exception:
                pass
            # Always include exception type so empty-string errors (e.g. raw
            # ProcessLookupError) stay debuggable in install_log.
            err_text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log = await self._write_log(
                approval, result="rolled_back", error=err_text,
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=err_text,
                                 install_log_id=log.id)

        log = await self._write_log(
            approval, result="success",
            installed_version=installed_version,
            previous_state=previous_state,
        )
        return InstallResult(result="success", installed_version=installed_version,
                             install_log_id=log.id)

    async def _uninstall_mcp(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]

        agent = await self._get_agent(target_id)
        previous_state = {
            "mcp_servers": list(agent.mcp_servers) if agent.mcp_servers else None
        }

        current = list(agent.mcp_servers) if agent.mcp_servers is not None else []
        agent.mcp_servers = [s for s in current if s != name]
        await self.session.commit()

        try:
            await _trigger_sync_config(agent.id)
        except Exception as e:
            agent.mcp_servers = previous_state["mcp_servers"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        # Registry-Cleanup: wenn der MCP nach dem Unassign verwaist ist
        # (kein Agent referenziert ihn mehr, und kein Agent hat mcp_servers=None
        # = „alle installierten MCPs aktiv"), loeschen wir das Registry-
        # Verzeichnis. Rollback wenn das fehlschlaegt — Agent-Allowlist-Cleanup
        # war erfolgreich, aber orphaned dir lassen ist explizit gewollt: der
        # User hat nur fuer diesen Agent un-assigned.
        registry_cleanup_error: str | None = None
        try:
            if await self._mcp_is_orphaned(name):
                await _call_mcp_uninstall(name)
        except Exception as e:
            # Non-fatal: Allowlist-Update + sync sind bereits durch.
            # Registry-Verzeichnis bleibt liegen, wird im Log vermerkt.
            registry_cleanup_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.warning(
                "MCP %r registry cleanup failed after un-assign from agent %s: %s",
                name, agent.id, registry_cleanup_error,
            )

        log = await self._write_log(
            approval, result="success", previous_state=previous_state,
            error=registry_cleanup_error,
        )
        return InstallResult(result="success", install_log_id=log.id)

    async def _mcp_is_orphaned(self, name: str) -> bool:
        """True wenn kein Agent den MCP mehr referenziert.

        `mcp_servers = None` bedeutet „alle installierten MCPs aktiv" — in
        diesem Fall ist der MCP NICHT verwaist, irgendein Agent nutzt ihn
        implizit weiter. Nur wenn alle Agents explizite Allowlists haben und
        `name` in keiner vorkommt, ist der MCP wirklich frei.
        """
        from app.models.agent import Agent
        result = await self.session.exec(select(Agent))
        for a in result.all():
            if a.mcp_servers is None:
                return False
            if name in a.mcp_servers:
                return False
        return True

    # ───────────────────── Helpers ─────────────────────

    async def _get_agent(self, agent_id: uuid.UUID) -> Agent:
        result = await self.session.exec(select(Agent).where(Agent.id == agent_id))
        agent = result.first()
        if agent is None:
            raise ValueError(f"Agent {agent_id} not found")
        return agent

    async def _write_log(
        self,
        approval: Approval,
        *,
        result: str,
        error: str | None = None,
        installed_version: str | None = None,
        previous_state: dict[str, Any] | None = None,
    ) -> InstallLog:
        payload = approval.payload or {}
        log = InstallLog(
            approval_id=approval.id,
            requester_agent_id=uuid.UUID(payload["requester_agent_id"])
                if payload.get("requester_agent_id") else None,
            target_agent_id=uuid.UUID(payload["target_agent_id"]),
            action_type=approval.action_type,
            resource_name=payload.get("name", ""),
            source=payload.get("source"),
            result=result,
            error=error,
            installed_version=installed_version,
            previous_state=previous_state,
        )
        self.session.add(log)
        await self.session.commit()
        await self.session.refresh(log)
        return log


# Dispatch table
_HANDLERS: dict[str, Any] = {
    "install_skill": InstallExecutor._install_skill,
    "uninstall_skill": InstallExecutor._uninstall_skill,
    "install_plugin": InstallExecutor._install_plugin,
    "uninstall_plugin": InstallExecutor._uninstall_plugin,
    "install_mcp": InstallExecutor._install_mcp,
    "uninstall_mcp": InstallExecutor._uninstall_mcp,
}


# ───────────────────── Service-layer wrappers (patchable in tests) ─────────────────────

async def _call_skill_install(source: str, name: str) -> dict[str, Any]:
    """Install a skill from source. Supports github: clone and local ref.

    Repo-Layout-Handling:
    - Single-Skill-Repo (z.B. obra/skill-foo): `<repo>/SKILL.md` im Root →
      clone direkt nach `~/.mc/skills/<name>/`.
    - Multi-Skill-Monorepo (z.B. google-labs-code/stitch-skills mit
      `skills/<name>/SKILL.md`): clone nach temp, extract nur den gewuenschten
      Sub-Skill (`skills/<name>/` oder `skill-<name>/`) nach
      `~/.mc/skills/<name>/`, Rest verwerfen.

    Live-Bug 2026-04-24: Install von shadcn-ui aus google-labs-code/stitch-skills
    clonete das GANZE Repo nach ~/.mc/skills/shadcn-ui/. Die echte SKILL.md
    lag unter skills/shadcn-ui/SKILL.md. sync_agent_skills_to_disk fand kein
    SKILL.md im Root → skipped den Skill → Worker-Container sah den Skill nicht.

    WICHTIG: `~/.mc/skills` gegen den HOST-Pfad aufloesen, nicht gegen
    das Container-User-Home (Backend laeuft als mcuser). HOME_HOST env-var
    ist explizit im Backend-Container gesetzt.
    """
    import asyncio
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    _home_host = os.environ.get("HOME_HOST", os.path.expanduser("~"))
    skills_dir = Path(_home_host) / ".mc" / "skills" / name

    if source.startswith("github:"):
        repo = source.removeprefix("github:")
        url = f"https://github.com/{repo}.git"

        # Clone in temp location, dann den richtigen Pfad extrahieren.
        # Das vermeidet dass ein falsch-strukturiertes Repo die Skills-
        # Bibliothek verschmutzt.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_clone = Path(tmp) / "clone"
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", url, str(tmp_clone),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"git clone failed: {stderr.decode()}")

            # Repo-Layout erkennen:
            # a) <repo>/SKILL.md        → Single-Skill-Repo
            # b) <repo>/skills/<name>/  → Multi-Skill-Monorepo (gängig bei
            #    google-labs-code/stitch-skills, anthropic/agent-skills usw.)
            # c) <repo>/<name>/SKILL.md → manche orgs legen ein Sub-Dir pro Skill
            if (tmp_clone / "SKILL.md").exists():
                source_dir = tmp_clone
            elif (tmp_clone / "skills" / name / "SKILL.md").exists():
                source_dir = tmp_clone / "skills" / name
            elif (tmp_clone / name / "SKILL.md").exists():
                source_dir = tmp_clone / name
            else:
                # Layout unklar — sinnvolle Fehlermeldung mit gefundenem Dir-Inhalt
                top = ", ".join(sorted(p.name for p in tmp_clone.iterdir()))[:200]
                raise RuntimeError(
                    f"Repo {url} hat kein erkennbares Skill-Layout: weder "
                    f"/SKILL.md, /skills/{name}/SKILL.md, noch /{name}/SKILL.md. "
                    f"Top-Level Inhalt: {top}"
                )

            # Existing install-dir ueberschreiben
            if skills_dir.exists():
                shutil.rmtree(skills_dir)
            shutil.copytree(source_dir, skills_dir)

        # Version aus SKILL.md frontmatter (optional)
        skill_md = skills_dir / "SKILL.md"
        version = None
        if skill_md.exists():
            for line in skill_md.read_text().splitlines()[:20]:
                if line.startswith("version:"):
                    version = line.split(":", 1)[1].strip().strip('"\'')
                    break
        return {"installed_version": version}
    elif source.startswith("~/.mc/skills/"):
        if not skills_dir.exists():
            raise FileNotFoundError(f"Local skill {name!r} not found at {skills_dir}")
        return {"installed_version": None}
    else:
        raise ValueError(f"Unsupported skill source: {source!r}")


async def _call_plugin_install(source: str, name: str) -> dict[str, Any]:
    """Delegate plugin install to CLI-Bridge via HTTP.

    MC hat keine lokale Install-Logik fuer Plugins — der shared cache
    (~/.mc/plugins/) wird ausschliesslich ueber die CLI-Bridge
    verwaltet (cli-bridge.py POST /plugins/install).

    source: Marketplace-Name (z.B. "claude-plugins-official") — wird
            als Kontext mitgegeben, aber der plugin_key (name) bestimmt
            welches Plugin installiert wird.

    Returns {"installed_version": str | None} — version aus dem
    shared cache nach Installation, oder None wenn nicht ermittelbar.
    """
    import asyncio

    def _sync_install() -> dict[str, Any]:
        from app.routers.cli_terminal import _bridge_post
        result = _bridge_post("/plugins/install", {"plugin_key": name}, timeout=130)
        if not result.get("ok"):
            raise RuntimeError(
                f"CLI-Bridge Plugin-Install fehlgeschlagen: {result.get('error', 'unknown')}"
            )
        return result

    result = await asyncio.get_event_loop().run_in_executor(None, _sync_install)

    # Versuche Version aus shared cache zu lesen
    from app.services.plugin_manager import list_available_plugins
    installed_version: str | None = None
    for plugin in list_available_plugins():
        if plugin.key == name:
            installed_version = plugin.version if plugin.version != "unknown" else None
            break

    return {"installed_version": installed_version}


async def _call_mcp_install(
    source: str, name: str, proposed_config: dict | None = None,
) -> dict[str, Any]:
    """Install MCP via registry. Runs in thread executor because subprocess is sync."""
    import asyncio
    from app.services.mcp_registry import MCPRegistry

    def _sync_install():
        mgr = MCPRegistry()
        manifest = mgr.install(source, name, proposed_config=proposed_config)
        return {"installed_version": manifest.installed_version}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_install)


async def _call_mcp_smoke_test(name: str) -> bool:
    from app.services.mcp_registry import MCPRegistry
    return await MCPRegistry().smoke_test(name)


async def _call_mcp_uninstall(name: str) -> None:
    import asyncio
    from app.services.mcp_registry import MCPRegistry

    def _sync_uninstall():
        MCPRegistry().uninstall(name)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _sync_uninstall)


async def _trigger_sync_config(agent_id: uuid.UUID) -> None:
    """Fire sync-config for the agent with Redis-lock.

    Runtime-Weiche:
    - cli-bridge agents: calls sync_docker_agent_files() directly (service layer).
      This renders templates into the claude-config bind-mount that the Docker
      container reads — no HTTP auth required, pure service call.
    - openclaw / host agents: sync-config requires the RPC WebSocket singleton
      that lives only in the router layer. We log a warning and skip — the
      operator must trigger sync-config manually via UI or API after the install.
      This is acceptable for Phase 1; a background RPC helper can be extracted
      in a later task if needed.

    A Redis lock ensures at most one concurrent install per agent.
    """
    from app.redis_client import get_redis
    from app.database import engine
    from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

    redis = await get_redis()
    lock_key = f"mc:agent:{agent_id}:install_lock"
    acquired = await redis.set(lock_key, "1", nx=True, ex=60)
    if not acquired:
        raise RuntimeError(f"Install lock busy for agent {agent_id}")
    try:
        async with _AsyncSession(engine, expire_on_commit=False) as session:
            agent = await session.get(Agent, agent_id)
            if agent is None:
                raise ValueError(f"Agent {agent_id} not found for sync-config")

            runtime = getattr(agent, "agent_runtime", "openclaw")
            if runtime == "cli-bridge":
                from app.services.docker_agent_sync import (
                    sync_docker_agent_files,
                    restart_docker_agent_container,
                )
                await sync_docker_agent_files(session, agent)
                # Also sync MCP config per agent
                from app.services.mcp_sync import sync_agent_mcp_to_disk
                try:
                    sync_agent_mcp_to_disk(agent)
                except Exception as e:
                    logger.warning("MCP sync failed for agent %s: %s", agent.id, e)
                # Restart container so claude-code picks up the new .mcp.json /
                # settings.json / SOUL.md on next session start. Without this,
                # a running session reads the old configs until it organically
                # restarts — new MCP/skill/plugin assignments would not take
                # effect until then.
                try:
                    restart_result = restart_docker_agent_container(agent)
                    logger.info(
                        "_trigger_sync_config: cli-bridge sync + restart done "
                        "for agent %s: %s", agent_id, restart_result,
                    )
                except Exception as e:
                    # Restart failure is non-fatal: files are on disk, agent
                    # will pick them up on its next organic restart. Log loud.
                    logger.error(
                        "Container restart failed for agent %s after install — "
                        "files synced but new config not active until next restart: %s",
                        agent.id, e,
                    )
            elif runtime == "host":
                # Host agents (Boss): DB state updated, but .mcp.json / settings.json
                # sync is deliberately skipped here — Boss's claude-config dir name
                # diverges from the canonical slug (Boss → "boss" per convention,
                # but actual dir is "boss-host" per ADR-014). Writing to the
                # derived slug would land in the wrong directory. For now, operator
                # must regenerate host configs manually after an install affecting
                # a host agent. See follow-up: add explicit slug column to Agent.
                logger.warning(
                    "_trigger_sync_config: host agent %s — DB state updated but "
                    "claude-config files NOT synced (slug-vs-dirname mismatch). "
                    "Regenerate manually + restart: launchctl kickstart -k "
                    "gui/$(id -u)/com.openclaw.%s",
                    agent.name, agent.name.lower(),
                )
            else:
                # openclaw agents (Henry) — self-managed via Gateway config.patch.
                # Skip here; Gateway handles its own session-reset protocol.
                logger.info(
                    "_trigger_sync_config: agent %s runtime=openclaw — "
                    "self-managed via Gateway, skipping.", agent.name,
                )
    finally:
        await redis.delete(lock_key)
