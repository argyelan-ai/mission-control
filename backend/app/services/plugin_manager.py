"""Plugin Manager — read shared cache, render agent settings.

Manages the central plugin store (~/.mc/plugins/) and renders
settings.json + installed_plugins.json per agent, based on cli_plugins in the DB.
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Container path for plugin files (Docker mount: claude-config → /home/agent/.claude)
CONTAINER_PLUGINS_PATH = "/home/agent/.claude/plugins"


def _plugins_dir() -> Path:
    """Path to the shared plugin directory."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "plugins"


def _agents_dir() -> Path:
    """Path to the agents directory."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "agents"


def _github_skills_dir() -> Path:
    """Path to the GitHub skills directory (for CLI-bridge agents)."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".agents" / "skills"


def _templates_dir() -> Path:
    """Path to Jinja2 templates (backend)."""
    return Path(__file__).parent.parent.parent / "templates"


class CliPlugin(BaseModel):
    key: str           # "frontend-design@claude-plugins-official"
    name: str          # "frontend-design"
    source: str        # "claude-plugins-official"
    version: str       # "unknown"
    installed: bool = True


def list_available_plugins() -> list[CliPlugin]:
    """Reads the master installed_plugins.json from the shared cache."""
    master_file = _plugins_dir() / "installed_plugins.json"
    if not master_file.exists():
        logger.warning("Shared installed_plugins.json nicht gefunden: %s", master_file)
        return []

    try:
        data = json.loads(master_file.read_text())
        plugins_dict = data.get("plugins", {})
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Fehler beim Lesen von installed_plugins.json: %s", e)
        return []

    result = []
    for key, entries in plugins_dict.items():
        parts = key.split("@", 1)
        name = parts[0]
        source = parts[1] if len(parts) > 1 else "unknown"
        version = "unknown"
        if isinstance(entries, list) and entries:
            version = entries[0].get("version", "unknown")
        result.append(CliPlugin(key=key, name=name, source=source, version=version))

    return sorted(result, key=lambda p: p.name)


def get_known_marketplaces() -> dict[str, Any]:
    """Reads known_marketplaces.json from the shared plugin directory."""
    km_file = _plugins_dir() / "known_marketplaces.json"
    if not km_file.exists():
        return {}
    try:
        data = json.loads(km_file.read_text())
        return data.get("marketplaces", data)
    except (json.JSONDecodeError, OSError):
        return {}


def _needed_marketplace_sources(cli_plugins: list[str] | None) -> set[str] | None:
    """Extracts needed marketplace sources from plugin keys.

    Returns None if all are needed (cli_plugins is None).
    """
    if cli_plugins is None:
        return None
    sources = set()
    for key in cli_plugins:
        parts = key.split("@", 1)
        if len(parts) > 1:
            sources.add(parts[1])
    return sources


def _rewrite_marketplace_paths(km_data: dict[str, Any]) -> dict[str, Any]:
    """Rewrites installLocation paths to container paths.

    Host: ${HOME_HOST}/.mc/plugins/marketplaces/claude-plugins-official
    Container: /home/agent/.claude/plugins/marketplaces/claude-plugins-official
    """
    rewritten = {}
    for name, info in km_data.items():
        entry = dict(info)
        entry["installLocation"] = f"{CONTAINER_PLUGINS_PATH}/marketplaces/{name}"
        rewritten[name] = entry
    return rewritten


def render_agent_settings(
    agent_slug: str,
    system_prompt: str,
    model: str,
    cli_plugins: list[str] | None,
    *,
    turn_signal_hooks: bool = True,
) -> str:
    """Renders settings.json for a CLI-bridge agent.

    cli_plugins: None = all available plugins, [] = none, ["x"] = only these.

    turn_signal_hooks: render the W2.1 UserPromptSubmit/Stop hooks block. Only
    the claude harness (runtime_protocol == "anthropic") understands them, so
    callers pass False for openclaude agents — its tolerance to an unknown
    top-level `hooks` key is unproven, so we eliminate the key rather than
    rely on it. Default True (claude is the fleet majority).
    """
    available = {p.key for p in list_available_plugins()}

    # Batcode treats non-listed plugins as enabled.
    # So: list ALL plugins, wanted ones as true, the rest as false.
    if cli_plugins is None:
        enabled_plugins = {k: True for k in sorted(available)}
    else:
        wanted = set(cli_plugins)
        enabled_plugins = {k: (k in wanted) for k in sorted(available)}

    all_marketplaces = get_known_marketplaces()
    needed_marketplaces = {}
    for plugin_key, is_enabled in enabled_plugins.items():
        if not is_enabled:
            continue
        parts = plugin_key.split("@", 1)
        source = parts[1] if len(parts) > 1 else None
        if source and source in all_marketplaces:
            needed_marketplaces[source] = all_marketplaces[source]

    # MEM-03 (Phase 2): reuse template_renderer's cached Environment instead of
    # constructing a new one per call. Pitfall 4 (RESEARCH.md): the previous
    # inline Environment(...) re-parsed cli_agent_settings.json.j2 on every
    # render. Both paths use the same backend/templates/ directory, so the
    # shared singleton (auto_reload=False, cache_size=512) is correct here.
    from app.services.template_renderer import _get_env as _shared_env
    template = _shared_env().get_template("cli_agent_settings.json.j2")
    return template.render(
        system_prompt=system_prompt,
        model=model,
        enabled_plugins=enabled_plugins,
        extra_marketplaces=needed_marketplaces,
        turn_signal_hooks=turn_signal_hooks,
    )


def render_agent_installed_plugins(cli_plugins: list[str] | None) -> str:
    """Renders agent-specific installed_plugins.json (only its plugins)."""
    master_file = _plugins_dir() / "installed_plugins.json"
    if not master_file.exists():
        return json.dumps({"version": 2, "plugins": {}}, indent=2)

    try:
        data = json.loads(master_file.read_text())
        all_plugins = data.get("plugins", {})
    except (json.JSONDecodeError, OSError):
        return json.dumps({"version": 2, "plugins": {}}, indent=2)

    if cli_plugins is None:
        filtered = all_plugins
    else:
        plugin_set = set(cli_plugins)
        filtered = {k: v for k, v in all_plugins.items() if k in plugin_set}

    return json.dumps({"version": 2, "plugins": filtered}, indent=2)


def sync_agent_plugins_to_disk(
    agent_slug: str,
    system_prompt: str,
    model: str,
    cli_plugins: list[str] | None,
    *,
    turn_signal_hooks: bool = True,
) -> dict[str, bool]:
    """Writes settings.json + installed_plugins.json for an agent to disk.

    turn_signal_hooks: forwarded to render_agent_settings — False for
    openclaude agents so no `hooks` key is emitted (see render_agent_settings).
    """
    agent_dir = _agents_dir() / agent_slug
    written = {}

    # settings.json — canonical (parent) + claude-config mirror.
    # Docker mounts claude-config/ as /home/agent/.claude/, and openclaude reads
    # settings.json from CLAUDE_CONFIG_DIR. The parent copy is the source of
    # truth the host sees; the mirror is what the container sees. A symlink
    # would be cleaner but breaks across the Docker mount boundary (parent
    # lies outside the mount), so we write both — identical content each time.
    settings_file = agent_dir / "settings.json"
    mirror_file = agent_dir / "claude-config" / "settings.json"
    try:
        content = render_agent_settings(
            agent_slug, system_prompt, model, cli_plugins,
            turn_signal_hooks=turn_signal_hooks,
        )
        settings_file.write_text(content)
        if mirror_file.is_symlink():
            mirror_file.unlink()
        mirror_file.parent.mkdir(parents=True, exist_ok=True)
        mirror_file.write_text(content)
        written["settings.json"] = True
    except Exception as e:
        logger.error("Fehler beim Schreiben von settings.json fuer %s: %s", agent_slug, e)
        written["settings.json"] = False

    # Agent-specific installed_plugins.json
    ipj_file = agent_dir / "claude-config" / "plugins" / "installed_plugins.json"
    try:
        content = render_agent_installed_plugins(cli_plugins)
        ipj_file.write_text(content)
        written["installed_plugins.json"] = True
    except Exception as e:
        logger.error("Fehler beim Schreiben von installed_plugins.json fuer %s: %s", agent_slug, e)
        written["installed_plugins.json"] = False

    # known_marketplaces.json — copy with container paths
    km_out = agent_dir / "claude-config" / "plugins" / "known_marketplaces.json"
    try:
        shared_km = get_known_marketplaces()
        rewritten = _rewrite_marketplace_paths(shared_km)
        # Replace symlink if present
        if km_out.is_symlink():
            km_out.unlink()
        km_out.write_text(json.dumps(rewritten, indent=2))
        written["known_marketplaces.json"] = True
    except Exception as e:
        logger.error("Fehler beim Schreiben von known_marketplaces.json fuer %s: %s", agent_slug, e)
        written["known_marketplaces.json"] = False

    # cache/ and marketplaces/ — only copy needed marketplace dirs
    needed = _needed_marketplace_sources(cli_plugins)
    plugin_out = agent_dir / "claude-config" / "plugins"

    for dirname in ("cache", "marketplaces"):
        shared_dir = _plugins_dir() / dirname
        out_dir = plugin_out / dirname
        try:
            # Replace symlink if present
            if out_dir.is_symlink():
                out_dir.unlink()

            if not shared_dir.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                written[dirname] = True
                continue

            out_dir.mkdir(parents=True, exist_ok=True)

            # Determine which subdirs to copy
            if needed is None:
                dirs_to_copy = [d for d in shared_dir.iterdir() if d.is_dir()]
            else:
                dirs_to_copy = [d for d in shared_dir.iterdir() if d.is_dir() and d.name in needed]

            # Clean up not-needed dirs in the target
            wanted_names = {d.name for d in dirs_to_copy}
            for existing in out_dir.iterdir():
                if existing.is_dir() and existing.name not in wanted_names:
                    shutil.rmtree(existing)

            # Copy
            for src in dirs_to_copy:
                dst = out_dir / src.name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)

            written[dirname] = True
        except Exception as e:
            logger.error("Fehler beim Sync von %s fuer %s: %s", dirname, agent_slug, e)
            written[dirname] = False

    return written


class GithubSkillEntry(BaseModel):
    name: str          # "stitch-design"
    repo_name: str     # "stitch-skills"
    source: str        # "google-labs-code/stitch-skills"
    version: str       # git hash short


class GithubSkillRepo(BaseModel):
    name: str          # "stitch-skills"
    source: str        # "google-labs-code/stitch-skills"
    version: str       # git hash short
    skills: list[str]  # ["stitch-design", "shadcn-ui", ...]


def list_github_skill_repos() -> list[GithubSkillRepo]:
    """Reads skills-lock.json and lists installed GitHub skill repos."""
    skills_dir = _github_skills_dir()
    lock_file = skills_dir / "skills-lock.json"

    if not lock_file.exists():
        return []

    try:
        data = json.loads(lock_file.read_text())
        skills_map = data.get("skills", {})
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Fehler beim Lesen von skills-lock.json: %s", e)
        return []

    result = []
    for repo_name, info in skills_map.items():
        source = info.get("source", repo_name)
        version = info.get("computedHash", "unknown")[:8]
        repo_dir = skills_dir / repo_name

        # Find SKILL.md files in the repo
        skill_names: list[str] = []
        if repo_dir.exists():
            for skill_md in sorted(repo_dir.rglob("SKILL.md")):
                rel = skill_md.parent.relative_to(repo_dir)
                skill_names.append(str(rel) if str(rel) != "." else repo_name)

        result.append(GithubSkillRepo(
            name=repo_name,
            source=source,
            version=version,
            skills=skill_names,
        ))

    return sorted(result, key=lambda r: r.name)


# ── Custom Skills (standalone SKILL.md in ~/.mc/skills/) ──────────────


def _custom_skills_dir() -> Path:
    """Path to the central custom-skills library."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "skills"


class CustomSkill(BaseModel):
    name: str           # "mc-debug"
    description: str    # from SKILL.md frontmatter (first line after name:)
    path: str           # absoluter Pfad


def list_custom_skills() -> list[CustomSkill]:
    """Lists all custom skills from ~/.mc/skills/."""
    skills_dir = _custom_skills_dir()
    if not skills_dir.exists():
        return []

    result = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue

        # Extract description from SKILL.md (description: line in frontmatter)
        description = ""
        try:
            content = skill_md.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.strip().lower().startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass

        result.append(CustomSkill(
            name=entry.name,
            description=description,
            path=str(entry),
        ))

    return result


def sync_agent_skills_to_disk(agent_slug: str, cli_skills: list[str] | None) -> dict[str, bool]:
    """Synchronizes custom skills into the agent's claude-config/skills/.

    Deletes old skills and copies only allowed ones as real directories.
    Symlinks are resolved (via .resolve() + symlinks=False in copytree) —
    a skill in the shared dir can point to a deliverable outside the mount;
    in the target we need real files because the Docker mount boundary
    breaks symlinks. Example: client-brand-skill → Sparky deliverable ZIP.

    Call site: `sync_docker_agent_files` calls this on every sync-config so
    the container sees the current cli_skills allowlist as real files in the
    claude-config/skills/ dir. Before the fix on 2026-04-24, this function
    was built but never called anywhere — see the Boss reflection on a
    content task where Shakespeare had to reconstruct the skill via WebFetch.

    Args:
        agent_slug: Agent directory name (e.g. "shakespeare")
        cli_skills: Allowlist (None=all, []=none, ["mc-debug"]=only these)

    Returns:
        Dict with skill name → True/False (copied/failed)
    """
    central = _custom_skills_dir()
    target = _agents_dir() / agent_slug / "claude-config" / "skills"

    # Delete old skills (clean state)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    # List central skills. Accept both real directories and symlinks,
    # as long as the resolved target has a SKILL.md —
    # medewo-gruppe-brand, for example, is a symlink to a Sparky deliverable.
    available: list[str] = []
    if central.exists():
        for d in central.iterdir():
            try:
                resolved = d.resolve()
                if resolved.is_dir() and (resolved / "SKILL.md").exists():
                    available.append(d.name)
            except OSError:
                continue

    # Filter
    if cli_skills is None:
        allowed = available  # null = all
    elif len(cli_skills) == 0:
        allowed = []  # empty list = none
    else:
        allowed = [s for s in cli_skills if s in available]

    # Copy (132 KB total — cheap). symlinks=False + src.resolve() resolves
    # symlink chains so the container ends up with real files.
    result: dict[str, bool] = {}
    # NEVER duplicate build artifacts and VCS state per agent — that
    # blows the sync up to hundreds of MB per skill and includes platform-
    # specific native binaries (e.g. macOS-arm64 node_modules, useless
    # in Linux containers). Working-copy pattern: agent copies the skill into
    # its /workspace and runs npm install there. See skill
    # viral-shorts/sub-skills/compose-segments.md.
    skill_ignore = shutil.ignore_patterns(
        "node_modules", "out", "dist", ".turbo", ".next",
        ".git", ".cache", "__pycache__", ".venv", "venv",
        "*.pyc", ".DS_Store",
    )
    for skill_name in sorted(allowed):
        try:
            src = (central / skill_name).resolve()
            shutil.copytree(src, target / skill_name, symlinks=False, ignore=skill_ignore)
            result[skill_name] = True
        except (OSError, shutil.Error) as e:
            logger.error("Skill-Kopie fehlgeschlagen %s → %s: %s", skill_name, agent_slug, e)
            result[skill_name] = False

    logger.info("Skills synced for %s: %d/%d (%s)",
                agent_slug, sum(result.values()), len(allowed),
                ", ".join(sorted(result.keys())) or "keine")
    return result
