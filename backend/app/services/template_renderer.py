"""
Jinja2 template renderer for agent configuration files.

Templates live in backend/templates/*.j2
Used by _provision_agent_background() and sync_agent_config_to_gateway().
"""
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

if TYPE_CHECKING:
    from app.models.agent import Agent

from app.config import settings

logger = logging.getLogger("mc.template_renderer")


def _github_owner() -> str:
    """Lazy import — git_service itself doesn't import anything heavy."""
    from app.services.git_service import GITHUB_OWNER
    return GITHUB_OWNER

# /app/templates/ in the Docker container (backend/templates/ locally)
TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"

_env: Environment | None = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            # MEM-03 (Phase 2): production cache config.
            #
            # Jinja's Environment has a built-in LRU cache for compiled templates
            # (default capacity 400). The cache works as long as auto_reload is
            # False — when True, Jinja calls os.stat() on the template file on
            # every get_template() to check freshness, which dominates render
            # latency for repeated calls with the same context.
            #
            # Trade-off: with auto_reload=False, edits to backend/templates/*.j2
            # at runtime require a backend rebuild (docker compose up --build -d
            # backend) to take effect. This matches the production workflow
            # (CLAUDE.md "Common Commands"); dev-time hot-reload is sacrificed
            # for the cache hit. cache_size=512 covers the >=256 Roadmap spec
            # with headroom (we have ~10 templates today).
            auto_reload=False,
            cache_size=512,
        )
    return _env


def render_agent_file(template_name: str, context: dict) -> str:
    """Renders a Jinja2 template with the given context."""
    env = _get_env()
    try:
        template = env.get_template(template_name)
        return template.render(**context)
    except TemplateNotFound:
        logger.error("Template nicht gefunden: %s (Pfad: %s)", template_name, TEMPLATES_DIR)
        raise
    except Exception as e:
        logger.error("Template-Render-Fehler fuer %s: %s", template_name, e)
        raise


def _reflection_fields() -> list[str]:
    from app.constants import REFLECTION_REQUIRED_FIELDS
    return list(REFLECTION_REQUIRED_FIELDS)


def _reflection_min_chars() -> int:
    from app.constants import REFLECTION_MIN_CHARS
    return REFLECTION_MIN_CHARS


def _reflection_charter() -> list[str]:
    from app.constants import REFLECTION_CHARTER
    return list(REFLECTION_CHARTER)


def build_agent_context(
    agent: "Agent",
    board_id: str | None = None,
    agents_on_board: list["Agent"] | None = None,
) -> dict:
    """Builds the Jinja2 context for an agent.

    team_ids maps well-known agent names to template variables so
    templates can write `{{ boss_agent_id }}` directly without needing a
    {% for %} loop. Keep the list in sync with the actual
    DB agent names (case-insensitive, spaces → '-').

    Cody (deleted 2026-04-09): `cody_agent_id` removed.
    Planner + Neo (deleted 2026-04-28): `planner_agent_id`, `neo_agent_id` removed.
    Canonical delegation targets: boss_agent_id, freecode_agent_id, sparky_agent_id.
    """
    effective_board_id = board_id or str(agent.board_id) if agent.board_id else ""

    # Look up team UUIDs from the active board agents.
    # Planner + Neo removed in Migration 0086 (ADR-020 workstream E).
    team_ids: dict[str, str] = {}
    role_map = {
        "boss": "boss_agent_id",              # Board Lead / Orchestrator (host)
        "henry": "henry_agent_id",            # Messenger (openclaw gateway)
        "freecode": "freecode_agent_id",      # Cloud Developer
        "sparky": "sparky_agent_id",          # Local Developer (DGX Spark)
        "rex": "rex_agent_id",                # Code Reviewer
        "tester": "tester_agent_id",          # QA
        "deployer": "deployer_agent_id",      # DevOps
        "researcher": "researcher_agent_id",  # Research
        "shakespeare": "shakespeare_agent_id",# Writer
        "davinci": "davinci_agent_id",        # Visual
    }
    team_list: list[dict] = []
    if agents_on_board:
        for a in agents_on_board:
            key = role_map.get(a.name.lower())
            if key:
                team_ids[key] = str(a.id)
            if a.id != agent.id:
                team_list.append({
                    "name": a.name,
                    "id": str(a.id),
                    "role": a.role or a.name,
                    "emoji": a.emoji or "🤖",
                    "runtime": getattr(a, "agent_runtime", "openclaw") or "openclaw",
                })

    # role_type: normalised short key for Jinja2 conditionals in templates
    # Maps agent name to AgentRole enum value (lowercase)
    _role_type_map = {
        "henry": "lead", "rex": "reviewer",
        "researcher": "researcher", "deployer": "deployer",
        "freecode": "developer", "shakespeare": "writer", "tester": "tester",
        "davinci": "designer", "sparky": "developer",
        "boss": "orchestrator",
    }
    raw_role = agent.role.lower().split()[0] if agent.role else "developer"
    # relay has no dedicated SOUL.md.j2 branch — map to "lead" so relay agents
    # (e.g. gateway relays) get the lead lifecycle rules rather than falling
    # through to the generic developer {% else %} block.
    fallback = "lead" if raw_role == "relay" else raw_role
    role_type = _role_type_map.get(agent.name.lower(), fallback)

    return {
        "agent_name": agent.name,
        "agent_id": str(agent.id),
        "agent_emoji": agent.emoji or "🤖",
        "board_id": effective_board_id,
        "is_board_lead": agent.is_board_lead,
        "agent_runtime": getattr(agent, "agent_runtime", "openclaw") or "openclaw",
        "role": role_type,
        "role_description": agent.role or agent.name,
        "api_base": "$MC_API_URL/api/v1",
        # Workstream D — per-agent persona section. Empty string when the
        # DB field is NULL (legacy agents); SOUL.md.j2 skips the Persona
        # block when this is falsy.
        "agent_persona_md": (getattr(agent, "soul_persona_md", None) or "").strip(),
        # Reflection SSoT — shared format constants rendered into the
        # template so the four field names come from one place
        # (app/constants.py). When these change, the error message in
        # agent_scoped.py and the extraction regex in
        # _extract_reflection_lesson also need updating.
        "reflection_required_fields": _reflection_fields(),
        "reflection_min_chars": _reflection_min_chars(),
        "reflection_charter": _reflection_charter(),
        # Operator identity + GitHub owner — from settings/env, never hardcode
        # (repo is public). OPERATOR_NAME / TELEGRAM_CHAT_ID / GITHUB_OWNER in .env.
        "operator_name": settings.operator_name,
        "github_owner": _github_owner(),
        "telegram_chat_id": settings.telegram_chat_id,
        # Canonical delegation targets (populated from role_map above)
        "boss_agent_id": team_ids.get("boss_agent_id", ""),
        "henry_agent_id": team_ids.get("henry_agent_id", ""),
        "freecode_agent_id": team_ids.get("freecode_agent_id", ""),
        "sparky_agent_id": team_ids.get("sparky_agent_id", ""),
        "rex_agent_id": team_ids.get("rex_agent_id", ""),
        "tester_agent_id": team_ids.get("tester_agent_id", ""),
        "deployer_agent_id": team_ids.get("deployer_agent_id", ""),
        "researcher_agent_id": team_ids.get("researcher_agent_id", ""),
        "shakespeare_agent_id": team_ids.get("shakespeare_agent_id", ""),
        "davinci_agent_id": team_ids.get("davinci_agent_id", ""),
        "team": team_list,
        # Scopes — for conditional sections (e.g. vault:write) in templates
        "scopes": _get_agent_scopes(agent),
    }


def _get_agent_scopes(agent: "Agent") -> list[str]:
    """Returns the agent's effective scopes (empty DB list = ALL_SCOPES)."""
    from app.scopes import get_agent_effective_scopes
    return get_agent_effective_scopes(agent)


def render_all_agent_files(
    agent: "Agent",
    board_id: str | None = None,
    agents_on_board: list["Agent"] | None = None,
) -> dict[str, str]:
    """Renders all config files for an agent.

    Returns: {"SOUL.md": "...", "USER.md": "...", "MEMORY.md": "..."}

    HEARTBEAT.md removed in migration 0125 — was rendered but never read by
    agents (only SOUL.md is injected via --append-system-prompt).
    """
    context = build_agent_context(agent, board_id, agents_on_board)

    result: dict[str, str] = {}

    for filename, template_name in [
        ("SOUL.md", "SOUL.md.j2"),
        ("USER.md", "USER.md.j2"),
        ("MEMORY.md", "MEMORY.md.j2"),
    ]:
        try:
            result[filename] = render_agent_file(template_name, context)
        except Exception as e:
            logger.error("Fehler beim Rendern von %s fuer Agent %s: %s", filename, agent.name, e)
            result[filename] = ""

    return result
