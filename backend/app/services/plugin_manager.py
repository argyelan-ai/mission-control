"""Plugin Manager — Shared Cache lesen, Agent-Settings rendern.

Verwaltet den zentralen Plugin-Store (~/.mc/plugins/) und rendert
pro Agent die settings.json + installed_plugins.json basierend auf cli_plugins in DB.
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Container-Pfad fuer Plugin-Dateien (Docker-Mount: claude-config → /home/agent/.claude)
CONTAINER_PLUGINS_PATH = "/home/agent/.claude/plugins"


def _plugins_dir() -> Path:
    """Pfad zum shared Plugin-Verzeichnis."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "plugins"


def _agents_dir() -> Path:
    """Pfad zum Agents-Verzeichnis."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "agents"


def _github_skills_dir() -> Path:
    """Pfad zum GitHub Skills-Verzeichnis (fuer CLI-Bridge Agents)."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".agents" / "skills"


def _templates_dir() -> Path:
    """Pfad zu Jinja2-Templates (Backend)."""
    return Path(__file__).parent.parent.parent / "templates"


class CliPlugin(BaseModel):
    key: str           # "frontend-design@claude-plugins-official"
    name: str          # "frontend-design"
    source: str        # "claude-plugins-official"
    version: str       # "unknown"
    installed: bool = True


def list_available_plugins() -> list[CliPlugin]:
    """Liest Master installed_plugins.json aus shared cache."""
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
    """Liest known_marketplaces.json aus shared Plugin-Verzeichnis."""
    km_file = _plugins_dir() / "known_marketplaces.json"
    if not km_file.exists():
        return {}
    try:
        data = json.loads(km_file.read_text())
        return data.get("marketplaces", data)
    except (json.JSONDecodeError, OSError):
        return {}


def _needed_marketplace_sources(cli_plugins: list[str] | None) -> set[str] | None:
    """Extrahiert benoetigte Marketplace-Sources aus Plugin-Keys.

    Returns None wenn alle benoetigt (cli_plugins is None).
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
    """Schreibt installLocation Pfade auf Container-Pfade um.

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
) -> str:
    """Rendert settings.json fuer einen CLI-Bridge Agent.

    cli_plugins: None = alle verfuegbaren Plugins, [] = keine, ["x"] = nur diese.
    """
    available = {p.key for p in list_available_plugins()}

    # Batcode behandelt nicht-gelistete Plugins als enabled.
    # Deshalb: ALLE Plugins auflisten, gewuenschte als true, Rest als false.
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
    )


def render_agent_installed_plugins(cli_plugins: list[str] | None) -> str:
    """Rendert agent-spezifische installed_plugins.json (nur seine Plugins)."""
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
) -> dict[str, bool]:
    """Schreibt settings.json + installed_plugins.json fuer einen Agent auf Disk."""
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
        content = render_agent_settings(agent_slug, system_prompt, model, cli_plugins)
        settings_file.write_text(content)
        if mirror_file.is_symlink():
            mirror_file.unlink()
        mirror_file.parent.mkdir(parents=True, exist_ok=True)
        mirror_file.write_text(content)
        written["settings.json"] = True
    except Exception as e:
        logger.error("Fehler beim Schreiben von settings.json fuer %s: %s", agent_slug, e)
        written["settings.json"] = False

    # Agent-spezifische installed_plugins.json
    ipj_file = agent_dir / "claude-config" / "plugins" / "installed_plugins.json"
    try:
        content = render_agent_installed_plugins(cli_plugins)
        ipj_file.write_text(content)
        written["installed_plugins.json"] = True
    except Exception as e:
        logger.error("Fehler beim Schreiben von installed_plugins.json fuer %s: %s", agent_slug, e)
        written["installed_plugins.json"] = False

    # known_marketplaces.json — Kopie mit Container-Pfaden
    km_out = agent_dir / "claude-config" / "plugins" / "known_marketplaces.json"
    try:
        shared_km = get_known_marketplaces()
        rewritten = _rewrite_marketplace_paths(shared_km)
        # Symlink ersetzen falls vorhanden
        if km_out.is_symlink():
            km_out.unlink()
        km_out.write_text(json.dumps(rewritten, indent=2))
        written["known_marketplaces.json"] = True
    except Exception as e:
        logger.error("Fehler beim Schreiben von known_marketplaces.json fuer %s: %s", agent_slug, e)
        written["known_marketplaces.json"] = False

    # cache/ und marketplaces/ — nur benoetigte Marketplace-Dirs kopieren
    needed = _needed_marketplace_sources(cli_plugins)
    plugin_out = agent_dir / "claude-config" / "plugins"

    for dirname in ("cache", "marketplaces"):
        shared_dir = _plugins_dir() / dirname
        out_dir = plugin_out / dirname
        try:
            # Symlink ersetzen falls vorhanden
            if out_dir.is_symlink():
                out_dir.unlink()

            if not shared_dir.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                written[dirname] = True
                continue

            out_dir.mkdir(parents=True, exist_ok=True)

            # Bestimmen welche Subdirs kopiert werden
            if needed is None:
                dirs_to_copy = [d for d in shared_dir.iterdir() if d.is_dir()]
            else:
                dirs_to_copy = [d for d in shared_dir.iterdir() if d.is_dir() and d.name in needed]

            # Nicht-benoetigte Dirs im Ziel aufraeumen
            wanted_names = {d.name for d in dirs_to_copy}
            for existing in out_dir.iterdir():
                if existing.is_dir() and existing.name not in wanted_names:
                    shutil.rmtree(existing)

            # Kopieren
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
    """Liest skills-lock.json und listet installierte GitHub Skill-Repos."""
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

        # SKILL.md Dateien im Repo finden
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
    """Pfad zur zentralen Custom-Skills-Bibliothek."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "skills"


class CustomSkill(BaseModel):
    name: str           # "mc-debug"
    description: str    # aus SKILL.md frontmatter (erste Zeile nach name:)
    path: str           # absoluter Pfad


def list_custom_skills() -> list[CustomSkill]:
    """Alle Custom Skills aus ~/.mc/skills/ auflisten."""
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

        # Beschreibung aus SKILL.md extrahieren (description: Zeile im frontmatter)
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
    """Synchronisiert Custom Skills in Agent claude-config/skills/.

    Loescht alte Skills und kopiert nur erlaubte als echte Verzeichnisse.
    Symlinks werden aufgeloest (via .resolve() + symlinks=False in copytree) —
    ein Skill im shared dir kann auf ein Deliverable ausserhalb des Mounts
    zeigen, im Ziel brauchen wir echte Files weil der Docker-Mount-Boundary
    Symlinks bricht. Beispiel: client-brand-skill → Sparky-Deliverable-ZIP.

    Call-Site: `sync_docker_agent_files` ruft das bei jedem sync-config auf,
    damit der Container die aktuelle cli_skills-Allowlist als echte Dateien
    im claude-config/skills/ Dir sieht. Vor dem Fix 2026-04-24 wurde diese
    Funktion zwar gebaut, aber nirgends aufgerufen — siehe Boss-Reflection zu
    einer Content-Task wo Shakespeare den Skill via WebFetch rekonstruieren
    musste.

    Args:
        agent_slug: Agent-Verzeichnisname (z.B. "shakespeare")
        cli_skills: Allowlist (None=alle, []=keine, ["mc-debug"]=nur diese)

    Returns:
        Dict mit Skill-Name → True/False (kopiert/fehlgeschlagen)
    """
    central = _custom_skills_dir()
    target = _agents_dir() / agent_slug / "claude-config" / "skills"

    # Alte Skills loeschen (sauberer Zustand)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    # Zentrale Skills auflisten. Sowohl echte Directories als auch Symlinks
    # akzeptieren, solange die resolvte Zielseite eine SKILL.md hat —
    # medewo-gruppe-brand liegt z.B. als Symlink auf ein Sparky-Deliverable.
    available: list[str] = []
    if central.exists():
        for d in central.iterdir():
            try:
                resolved = d.resolve()
                if resolved.is_dir() and (resolved / "SKILL.md").exists():
                    available.append(d.name)
            except OSError:
                continue

    # Filtern
    if cli_skills is None:
        allowed = available  # null = alle
    elif len(cli_skills) == 0:
        allowed = []  # leere Liste = keine
    else:
        allowed = [s for s in cli_skills if s in available]

    # Kopieren (132 KB total — billig). symlinks=False + src.resolve() löst
    # Symlink-Ketten auf, damit im Container echte Files liegen.
    result: dict[str, bool] = {}
    # Build-Artefakte und VCS-State NIEMALS pro-Agent duplizieren — die
    # blowen den Sync auf hunderte MB pro Skill und enthalten platform-
    # spezifische native Binaries (z.B. macOS-arm64 node_modules, useless
    # in Linux-Containern). Working-Copy-Pattern: Agent kopiert Skill in
    # sein /workspace und ruft dort npm install auf. Siehe Skill
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
