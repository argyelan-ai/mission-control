"""
Skills Router — Phase 29 (Gateway-Sunset) Stage.

Pre-Phase-29 the list/install/update endpoints proxied OpenClaw Gateway RPC
(skills.status / skills.install / skills.update). After the gateway sunset
those endpoints have no remote source of truth, so they return HTTP 410 Gone
until Phase 31 rebuilds the frontend with a different shape.

The file-content endpoints (GET/PUT /skills/{name}/content) and the
per-agent skill_filter / cli_plugins / cli_skills endpoints remain
functional — they operate on local disk + DB and don't need the gateway.
"""

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent
from app.services.activity import emit_event

# Find skills directory (host path via HOME_HOST env, container fallback)
def _skill_dirs() -> list[Path]:
    dirs = []
    for home in filter(None, [os.environ.get("HOME_HOST"), os.path.expanduser("~")]):
        p = Path(home) / ".mc" / "skills"
        if p not in dirs:
            dirs.append(p)
    return dirs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["skills"])


# ── Request/Response Models ─────────────────────────────────────────────────

class SkillInstallRequest(BaseModel):
    install_id: str


class SkillUpdateRequest(BaseModel):
    enabled: bool | None = None
    api_key: str | None = None
    env: dict[str, str] | None = None


class AgentSkillsUpdateRequest(BaseModel):
    skills: list[str] | None = None        # OpenClaw skill_filter
    cli_plugins: list[str] | None = None   # CLI plugins
    update_cli_plugins: bool = False       # True = apply cli_plugins field
    cli_skills: list[str] | None = None    # Custom skills allowlist
    update_cli_skills: bool = False        # True = apply cli_skills field


# ── 410 Gone helper ─────────────────────────────────────────────────────────

_GONE_DETAIL = (
    "Phase 29 (Gateway sunset). Read local skills via filesystem; "
    "Phase 31 rebuild restores list/install via different shape."
)


# ── API Endpoints ───────────────────────────────────────────────────────────

@router.get("/skills")
async def list_skills(current_user=Depends(require_user)):
    """List local skills from ~/.mc/skills/ (post-Phase-29, no Gateway)."""
    skills = []
    for skills_dir in _skill_dirs():
        if not skills_dir.exists():
            continue
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            skill = {"name": entry.name}
            skill_file = entry / "SKILL.md"
            if skill_file.exists():
                try:
                    text = skill_file.read_text(encoding="utf-8")
                    for line in text.splitlines():
                        stripped = line.strip()
                        if stripped.startswith("name:"):
                            val = stripped[5:].strip().strip('"').strip("'")
                            if val:
                                skill["name"] = val
                                skill["key"] = entry.name
                        elif stripped.startswith("description:"):
                            val = stripped[12:].strip().strip('"').strip("'")
                            if val:
                                skill["description"] = val
                        elif stripped == "---" and skill.get("description"):
                            break
                except Exception:
                    pass
            skills.append(skill)
    seen = set()
    deduped = []
    for s in skills:
        key = s.get("key", s["name"])
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    return JSONResponse(
        content={"skills": deduped, "total": len(deduped)},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/skills/{skill_name}")
async def get_skill(skill_name: str, current_user=Depends(require_user)):
    """Phase 29: Gateway-proxied endpoint removed."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_GONE_DETAIL)


@router.get("/skills/{skill_name}/content")
async def get_skill_content(
    skill_name: str,
    current_user=Depends(require_user),
):
    """Reads the SKILL.md file of a custom skill from the local directory."""
    # Security check: no path traversal
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        raise HTTPException(400, "Ungültiger Skill-Name")

    skill_dirs = _skill_dirs()
    for skills_dir in skill_dirs:
        skill_file = skills_dir / skill_name / "SKILL.md"
        if skill_file.exists():
            try:
                content = skill_file.read_text(encoding="utf-8")
                return {
                    "skill_name": skill_name,
                    "path": str(skill_file),
                    "content": content,
                    "found": True,
                }
            except OSError as e:
                raise HTTPException(500, f"Datei konnte nicht gelesen werden: {e}")

    raise HTTPException(404, f"SKILL.md für '{skill_name}' nicht gefunden")


@router.put("/skills/{skill_name}/content")
async def update_skill_content(
    skill_name: str,
    request: "SkillContentUpdateRequest",
    current_user=Depends(require_user),
):
    """Writes the content of a SKILL.md file (creates it if necessary)."""
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        raise HTTPException(400, "Ungültiger Skill-Name")

    # Find directory — prefers HOME_HOST
    skill_dirs = _skill_dirs()
    target_dir: Path | None = None

    for d in skill_dirs:
        if d.is_dir():
            target_dir = d
            break

    if target_dir is None:
        raise HTTPException(500, "Skills-Verzeichnis nicht gefunden")

    skill_dir = target_dir / skill_name
    skill_file = skill_dir / "SKILL.md"

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(request.content, encoding="utf-8")
        return {
            "skill_name": skill_name,
            "path": str(skill_file),
            "saved": True,
        }
    except OSError as e:
        raise HTTPException(500, f"Datei konnte nicht gespeichert werden: {e}")


class SkillContentUpdateRequest(BaseModel):
    content: str


@router.post("/skills/{skill_name}/install")
async def install_skill(
    skill_name: str,
    body: SkillInstallRequest,
    current_user=Depends(require_user),
):
    """Phase 29: Gateway-proxied endpoint removed."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_GONE_DETAIL)


@router.patch("/skills/{skill_name}")
async def update_skill(
    skill_name: str,
    body: SkillUpdateRequest,
    current_user=Depends(require_user),
):
    """Phase 29: Gateway-proxied endpoint removed."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_GONE_DETAIL)


@router.get("/agents/{agent_id}/skills")
async def get_agent_skills(
    agent_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Skills for a given agent (what it can use).

    Phase 29: gateway-side skill listing removed. Returns local plugins +
    custom skills only. `skills` is empty until Phase 31 frontend rebuild
    introduces a new source of truth for the OpenClaw skill catalogue.
    """
    import uuid as uuid_mod
    agent = await session.get(Agent, uuid_mod.UUID(agent_id))
    if not agent:
        raise HTTPException(404, "Agent nicht gefunden")

    # CLI plugins from shared cache
    from app.services.plugin_manager import list_available_plugins, list_custom_skills
    cli_plugins_available = [p.model_dump() for p in list_available_plugins()]
    custom_skills_available = [s.model_dump() for s in list_custom_skills()]

    return {
        "skills": [],  # Phase 29: gateway catalogue removed
        "agent_skill_filter": agent.skill_filter,
        "gateway_connected": False,  # Kept for backward-compat with frontend
        "cli_plugins": cli_plugins_available,
        "agent_cli_plugins": agent.cli_plugins,
        "custom_skills": custom_skills_available,
        "agent_cli_skills": agent.cli_skills,
    }


@router.patch("/agents/{agent_id}/skills")
async def update_agent_skills(
    agent_id: str,
    body: AgentSkillsUpdateRequest,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Set the agent skill filter (which skills the agent may use).

    skill_filter semantics: None = all skills, [] = no skills, ["x"] = only these.

    Phase 29: gateway sync removed — DB write remains canonical. CLI plugin /
    custom-skill flow (which writes settings.json on disk + restarts the
    docker agent container) is unaffected by the gateway sunset.
    """
    import uuid as uuid_mod
    agent = await session.get(Agent, uuid_mod.UUID(agent_id))
    if not agent:
        raise HTTPException(404, "Agent nicht gefunden")

    # Save skill_filter in DB (None = all, [] = none)
    old_filter = set(agent.skill_filter or []) if agent.skill_filter is not None else None
    new_filter = body.skills  # None = all skills, [] = no skills, ["x"] = only x
    agent.skill_filter = new_filter
    agent.skills = list(new_filter or [])  # Keep UI tags in sync
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    new_filter_set = set(new_filter) if new_filter is not None else None
    if old_filter is None and new_filter_set is None:
        changed = False
    elif old_filter is None or new_filter_set is None:
        changed = True
    else:
        changed = old_filter != new_filter_set

    # On changes: activity event (gateway sync removed Phase 29)
    if changed:
        desc = "alle" if new_filter is None else (", ".join(sorted(new_filter)) if new_filter else "keine")

        await emit_event(
            session,
            "agent.skills_updated",
            f"Skill-Filter von {agent.name} aktualisiert: {desc}",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail={
                "agent_name": agent.name,
                "skill_filter": new_filter,
            },
        )

    # CLI plugins update
    cli_synced = False
    worker_restarted = False
    if body.update_cli_plugins:
        agent.cli_plugins = body.cli_plugins
        session.add(agent)
        await session.commit()
        await session.refresh(agent)

        # Render settings.json + installed_plugins.json to disk
        if agent.agent_runtime == "cli-bridge":
            agent_slug = agent.name.lower().replace(" ", "-")
            from app.services.plugin_manager import sync_agent_plugins_to_disk
            import json as _json
            from pathlib import Path
            import os
            home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
            settings_path = Path(home) / ".mc" / "agents" / agent_slug / "settings.json"
            # soul_md from DB is the source of truth for systemPrompt
            current_prompt = (agent.soul_md and agent.soul_md.strip()) or ""
            current_model = agent.model or "minimax-m2.7"
            if not current_prompt and settings_path.exists():
                try:
                    data = _json.loads(settings_path.read_text())
                    current_prompt = data.get("systemPrompt", "")
                    current_model = data.get("model", current_model)
                except Exception:
                    pass
            # W2.1 turn-signal hooks only for the claude harness; openclaude
            # must not receive the unknown `hooks` key.
            from app.models.runtime import Runtime
            from app.services.harness_compat import runtime_protocol
            _rt = await session.get(Runtime, agent.runtime_id) if agent.runtime_id else None
            written = sync_agent_plugins_to_disk(
                agent_slug, current_prompt, current_model, body.cli_plugins,
                turn_signal_hooks=(runtime_protocol(_rt) == "anthropic"),
            )
            cli_synced = all(written.values())

            # Restart worker/container so new plugin files take effect
            worker_restarted = False
            if cli_synced:
                try:
                    from app.routers.cli_terminal import _bridge_post
                    restart_result = _bridge_post(f"/worker/{agent_slug}/restart", {})
                    worker_restarted = restart_result.get("ok", False)
                    if not worker_restarted:
                        logger.warning("Worker restart fehlgeschlagen: %s", restart_result)
                except Exception as e:
                    logger.warning("Worker restart nach Plugin-Update fehlgeschlagen: %s", e)

                # Docker container restart (for V2 Docker agents)
                try:
                    from app.services.docker_agent_sync import restart_docker_agent_container
                    container_result = restart_docker_agent_container(agent)
                    if container_result.get("status") == "restarted":
                        logger.info("Container restarted nach Plugin-Update: %s", agent.name)
                    elif "no container" not in container_result.get("status", ""):
                        logger.warning("Container restart: %s", container_result)
                except Exception as e:
                    logger.warning("Container restart nach Plugin-Update fehlgeschlagen: %s", e)

        desc = "alle" if body.cli_plugins is None else (", ".join(sorted(body.cli_plugins)) if body.cli_plugins else "keine")
        await emit_event(
            session,
            "agent.cli_plugins_updated",
            f"CLI Plugins von {agent.name} aktualisiert: {desc}",
            agent_id=agent.id,
            board_id=agent.board_id,
        )

    # Custom skills update (cli_skills → claude-config/skills/ copies)
    skills_synced = False
    if body.update_cli_skills:
        agent.cli_skills = body.cli_skills
        session.add(agent)
        await session.commit()
        await session.refresh(agent)

        if agent.agent_runtime == "cli-bridge":
            agent_slug = agent.name.lower().replace(" ", "-")
            from app.services.plugin_manager import sync_agent_skills_to_disk
            result = sync_agent_skills_to_disk(agent_slug, body.cli_skills)
            skills_synced = all(result.values()) if result else True

            # Restart container if skills have changed
            if skills_synced and not worker_restarted:
                try:
                    from app.services.docker_agent_sync import restart_docker_agent_container
                    container_result = restart_docker_agent_container(agent)
                    if container_result.get("status") == "restarted":
                        logger.info("Container restarted nach Skill-Update: %s", agent.name)
                except Exception as e:
                    logger.warning("Container restart nach Skill-Update fehlgeschlagen: %s", e)

        desc = "alle" if body.cli_skills is None else (", ".join(sorted(body.cli_skills)) if body.cli_skills else "keine")
        await emit_event(
            session,
            "agent.cli_skills_updated",
            f"Custom Skills von {agent.name} aktualisiert: {desc}",
            agent_id=agent.id,
            board_id=agent.board_id,
        )

    return {
        "agent_id": agent_id,
        "skill_filter": agent.skill_filter,
        "cli_plugins": agent.cli_plugins,
        "cli_skills": agent.cli_skills,
        "changed": changed or body.update_cli_plugins or body.update_cli_skills,
        "gateway_synced": False,  # Phase 29: gateway sync removed
        "cli_synced": cli_synced,
        "skills_synced": skills_synced,
        "worker_restarted": worker_restarted,
    }
