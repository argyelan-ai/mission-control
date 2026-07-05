#!/usr/bin/env python3
"""
Generic CLI Agent Bridge — startet claude-code CLI Agents via tmux.
Laeuft als Service ausserhalb Docker, empfaengt Requests vom MC Backend.

Jeder Agent hat eine eigene settings.json:
  ~/.mc/agents/{agent_name}/settings.json

POST   /start               Body: {"agent_name": "freecode", "task_id": "...", "workspace": "...", "prompt": "..."}
POST   /input/{task_id}     Body: {"text": "..."}
GET    /health
GET    /sessions
GET    /status/{task_id}
GET    /output/{task_id}
DELETE /sessions/{task_id}
"""
import asyncio
import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import struct
import subprocess as _sp
import sys
import termios
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import time
from typing import Optional
import uuid

PORT = 18792
HOME = Path.home()
LOG_DIR = Path(os.environ.get("CLI_BRIDGE_LOG_DIR", HOME / "Workspace/Sandboxes/free-code-local/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "cli-bridge.log"

# Standard CLI-Binary für cli-bridge Host-Agents — openclaude direkt
# (bis 2026-04-20 lief das via ~/.local/bin/batcode Wrapper-Script, das ist
# mit der Claude-Fleet-Migration obsolet; batcode war nur ein dünner Shell-
# Wrapper um dasselbe openclaude-Binary).
CLI_BIN = os.environ.get("CLI_BRIDGE_CLI_BIN", str(HOME / ".npm-global/bin/openclaude"))
# Shared env-File — wird vom Template nur für openclaude-Agents gesourced
# (nicht für claude-Binary-Agents wie Boss).
CLI_ENV = os.environ.get("CLI_BRIDGE_CLI_ENV", str(HOME / "Workspace/Sandboxes/openclaude-local/openclaude.local.env"))
MODEL_PROFILES = os.environ.get("CLI_BRIDGE_MODEL_PROFILES", str(HOME / "Workspace/Sandboxes/openclaude-local/model-profiles.json"))
AGENTS_DIR = HOME / ".mc" / "agents"
PLUGINS_DIR = Path.home() / ".mc" / "plugins"

TMUX_BIN = (
    shutil.which("tmux")
    or "/opt/homebrew/bin/tmux"
    or "/usr/local/bin/tmux"
)


def _check_tmux():
    if not os.path.isfile(TMUX_BIN):
        raise RuntimeError("tmux nicht gefunden — bitte installieren: brew install tmux")
    result = _sp.run([TMUX_BIN, "-V"], capture_output=True, text=True)
    log(f"tmux version: {result.stdout.strip()}")


active_sessions: dict[str, str] = {}  # task_id -> tmux session name

QUEUE_BASE = AGENTS_DIR  # ~/.mc/agents/


def _queue_dir(agent_name: str) -> Path:
    return AGENTS_DIR / agent_name / "queue"


def _ensure_queue(agent_name: str):
    q = _queue_dir(agent_name)
    for sub in ("pending", "running", "done", "failed"):
        (q / sub).mkdir(parents=True, exist_ok=True)


def _enqueue_task(agent_name: str, task_id: str, workspace: str, prompt: str) -> str:
    """Schreibt Task-JSON in pending Queue. Gibt Fehlermeldung zurück bei OS-Fehler."""
    import base64
    _ensure_queue(agent_name)
    task_file = _queue_dir(agent_name) / "pending" / f"{task_id}.json"
    # Kein Überschreiben bei doppelter task_id
    if task_file.exists():
        log(f"WARN: Task bereits in Queue: {task_file}")
        return f"already_queued: {task_file}"
    payload = {
        "task_id": task_id,
        "workspace": workspace,
        "prompt_b64": base64.b64encode(prompt.encode("utf-8")).decode("ascii"),
        "created_at": datetime.now().isoformat(),
    }
    try:
        with open(task_file, "w") as f:
            import json as _json
            _json.dump(payload, f)
    except OSError as e:
        log(f"ERROR writing task file {task_file}: {e}")
        return f"error: {e}"
    log(f"Task enqueued: agent={agent_name} task_id={task_id} workspace={workspace}")
    return f"enqueued: {task_file}"


def _queue_status(agent_name: str, task_id: str) -> str:
    """Gibt Status eines Tasks zurück: pending|running|done|failed|unknown."""
    q = _queue_dir(agent_name)
    for status in ("running", "pending", "done", "failed"):
        if (q / status / f"{task_id}.json").exists():
            return status
    return "unknown"


def _known_agent_names() -> set:
    names = set()
    if AGENTS_DIR.exists():
        for d in AGENTS_DIR.iterdir():
            if d.is_dir() and (d / "settings.json").exists():
                names.add(d.name)
    return names



def _start_plugins_shell() -> bool:
    """Startet eine interaktive Installer-Agent-Session.

    Hostet einen claude-cli mit Sonnet 4.6 + Installer-Persona im Workspace
    ~/.mc/agents/installer/. Persona+Tools werden als
    --append-system-prompt geladen, MC-API-Token aus agent.env injiziert.
    Tab heisst weiterhin "plugins-shell" damit Frontend-Code unverändert bleibt.
    """
    session = "plugins-shell"

    result = _sp.run(
        [TMUX_BIN, "has-session", "-t", f"={session}"],
        capture_output=True,
    )
    if result.returncode == 0:
        log(f"Installer shell bereits aktiv: {session}")
        return True

    installer_dir = Path(os.path.expanduser("~/.mc/agents/installer"))
    claude_md = installer_dir / "CLAUDE.md"
    agent_env = installer_dir / "agent.env"
    config_dir = installer_dir / "claude-config"

    if not claude_md.exists() or not agent_env.exists():
        log(f"ERROR: Installer setup incomplete — missing {claude_md} or {agent_env}")
        return False

    # OAuth token aus root .env (gleicher Token wie Boss + Docker-Agents)
    repo_env = Path(__file__).parent.parent / ".env"
    oauth_token = ""
    if repo_env.exists():
        for line in repo_env.read_text().splitlines():
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                oauth_token = line.split("=", 1)[1].strip().strip('"')
                break
    if not oauth_token:
        log("ERROR: CLAUDE_CODE_OAUTH_TOKEN not found in .env")
        return False

    # Tokens aus agent.env. MC_AGENT_TOKEN für agent/* Endpoints (mc me etc),
    # MC_USER_JWT für admin-Endpoints (POST /mcp-servers, /plugins, PATCH
    # /agents/{id}/{mcp-servers,skills}, /credentials).
    mc_url = "http://localhost:8000"
    mc_token = ""
    mc_jwt = ""
    for line in agent_env.read_text().splitlines():
        if line.startswith("MC_AGENT_TOKEN="):
            mc_token = line.split("=", 1)[1].strip()
        elif line.startswith("MC_USER_JWT="):
            mc_jwt = line.split("=", 1)[1].strip()
        elif line.startswith("MC_API_URL="):
            mc_url = line.split("=", 1)[1].strip()

    # CLAUDE.md ist Single Source of Truth für die Installer-Persona +
    # Tool-Reference. claude-CLI lädt sie automatisch als Project-Memory aus
    # cwd. Wir generieren NICHT — der Operator pflegt sie direkt damit Updates beim
    # Restart nicht verlorengehen.
    config_dir.mkdir(parents=True, exist_ok=True)

    claude_bin = str(HOME / ".local/bin/claude")
    cmd = (
        f"CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(oauth_token)} "
        f"ANTHROPIC_MODEL=claude-sonnet-4-6 "
        f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))} "
        f"MC_API_URL={shlex.quote(mc_url)} "
        f"MC_AGENT_TOKEN={shlex.quote(mc_token)} "
        f"MC_USER_JWT={shlex.quote(mc_jwt)} "
        f"{shlex.quote(claude_bin)} "
        f"--dangerously-skip-permissions"
    )

    try:
        _sp.run(
            [TMUX_BIN, "new-session", "-d", "-s", session,
             "-x", "220", "-y", "50",
             "-c", str(installer_dir)],
            check=True, capture_output=True,
        )
        _sp.run(
            [TMUX_BIN, "set-option", "-t", session, "mouse", "on"],
            capture_output=True,
        )
        _sp.run(
            [TMUX_BIN, "send-keys", "-t", session, cmd, "Enter"],
            check=True, capture_output=True,
        )

        # Window 1: installer-poll.sh — pollt /api/v1/agent/me/poll und leitet
        # neue Task-Prompts an Window 0 weiter. Der Operator kann damit Tasks via
        # `mc delegate --to Installer ...` aus jedem Agent zuweisen.
        poll_script = Path(__file__).parent / "installer-poll.sh"
        if poll_script.exists():
            poll_cmd = (
                f"MC_API_URL={shlex.quote(mc_url)} "
                f"MC_AGENT_TOKEN={shlex.quote(mc_token)} "
                f"TMUX_BIN={shlex.quote(TMUX_BIN)} "
                f"bash {shlex.quote(str(poll_script))}"
            )
            _sp.run(
                [TMUX_BIN, "new-window", "-t", f"{session}:1",
                 "-c", str(installer_dir),
                 "-n", "poll"],
                capture_output=True,
            )
            _sp.run(
                [TMUX_BIN, "send-keys", "-t", f"{session}:1", poll_cmd, "Enter"],
                capture_output=True,
            )
            # Auf Window 0 zurückspringen damit der User claude direkt sieht
            _sp.run(
                [TMUX_BIN, "select-window", "-t", f"{session}:0"],
                capture_output=True,
            )

        log(f"Installer shell gestartet: {session} (claude+sonnet + poll) → {installer_dir}")
        return True
    except _sp.CalledProcessError as e:
        log(f"ERROR starting installer shell: {e.stderr}")
        return False


def _start_worker_session(agent_name: str) -> bool:
    """Startet permanente Worker-Session falls sie nicht läuft."""
    session = agent_name  # Permanent session heisst einfach agent_name, z.B. "freecode"
    worker_script = str(AGENTS_DIR / agent_name / "worker.sh")

    if not Path(worker_script).exists():
        log(f"WARN: worker.sh nicht gefunden: {worker_script}")
        return False

    # Prüfen ob Session bereits läuft (= erzwingt exakten Match, kein Prefix-Matching)
    result = _sp.run(
        [TMUX_BIN, "has-session", "-t", f"={session}"],
        capture_output=True,
    )
    if result.returncode == 0:
        # Session läuft — neu starten damit neues worker.sh aktiv wird
        _sp.run([TMUX_BIN, "kill-session", "-t", f"={session}"], capture_output=True)
        log(f"Worker session gestoppt fuer Neustart: {session}")

    # Stale Queue-Lock aufräumen (kann nach Kill einer laufenden Session übrig bleiben)
    lock_dir = AGENTS_DIR / agent_name / "queue.lock.d"
    if lock_dir.exists():
        try:
            lock_dir.rmdir()
            log(f"Stale Queue-Lock entfernt: {agent_name}")
        except OSError:
            pass  # Nicht leer — Worker läuft noch parallel, kein Problem

    # Neue permanente Session starten — direkt mit bash, kein .zshrc sourcen
    try:
        _sp.run(
            [TMUX_BIN, "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
             "bash", worker_script],
            check=True, capture_output=True,
        )
        log(f"Worker session gestartet: {session} → {worker_script}")
        return True
    except _sp.CalledProcessError as e:
        log(f"ERROR starting worker session {session}: {e.stderr}")
        return False


def _resolve_enabled_plugins(cli_plugins=None) -> list:
    """Bestimmt enabledPlugins aus cli_plugins (DB) + shared installed_plugins.json."""
    master = PLUGINS_DIR / "installed_plugins.json"
    if not master.exists():
        return []
    try:
        data = json.loads(master.read_text())
        available = list(data.get("plugins", {}).keys())
    except (json.JSONDecodeError, OSError):
        return []
    if cli_plugins is None:
        return sorted(available)
    return [k for k in cli_plugins if k in set(available)]


def _resolve_extra_marketplaces(cli_plugins=None) -> dict:
    """Liest known_marketplaces.json und filtert auf benoetigte."""
    km_file = PLUGINS_DIR / "known_marketplaces.json"
    if not km_file.exists():
        return {}
    try:
        data = json.loads(km_file.read_text())
        all_mp = data.get("marketplaces", data)
    except (json.JSONDecodeError, OSError):
        return {}
    if cli_plugins is None:
        return all_mp
    needed_sources = set()
    for key in cli_plugins:
        parts = key.split("@", 1)
        if len(parts) > 1:
            needed_sources.add(parts[1])
    return {k: v for k, v in all_mp.items() if k in needed_sources}


def _provision_agent(agent_name: str, mc_agent_token: str, model: str,
                     system_prompt: str, extra_plugins: list, **kwargs) -> dict:
    """Provisioniert einen neuen CLI-Bridge Agent komplett auf dem Filesystem."""
    import shutil as _shutil
    from jinja2 import Environment, FileSystemLoader

    agent_dir = AGENTS_DIR / agent_name
    template_dir = AGENTS_DIR / "_template"
    created = []

    # 1. Queue-Verzeichnisse
    for sub in ("pending", "running", "done", "failed"):
        p = agent_dir / "queue" / sub
        p.mkdir(parents=True, exist_ok=True)
    created.append("queue/")

    # 2. claude-config aus _template kopieren (falls _template existiert)
    claude_config_dst = agent_dir / "claude-config"
    if not claude_config_dst.exists():
        template_claude = template_dir / "claude-config"
        if template_claude.exists():
            _shutil.copytree(str(template_claude), str(claude_config_dst))
            created.append("claude-config/")
        else:
            claude_config_dst.mkdir(parents=True, exist_ok=True)
            created.append("claude-config/ (leer — kein _template gefunden)")

    # 3. Jinja2 Templates rendern
    template_search_path = Path(__file__).parent.parent / "backend" / "templates"
    if not template_search_path.exists():
        template_search_path = Path(__file__).parent / "templates"

    try:
        env = Environment(loader=FileSystemLoader(str(template_search_path)),
                          keep_trailing_newline=True)

        effective_cli_bin = kwargs.get("cli_bin") or str(CLI_BIN)
        ctx = {
            "agent_name": agent_name,
            "agent_slug": agent_name,
            "model": model,
            "system_prompt": system_prompt,
            "extra_plugins": extra_plugins,
            "cli_bin": effective_cli_bin,
            "is_claude_bin": effective_cli_bin.endswith("/claude") or effective_cli_bin == "claude",
            "model_profiles": str(MODEL_PROFILES),
            "shared_env": str(CLI_ENV),
            "mc_agent_token": mc_agent_token,
            "claude_config_dir": str(agent_dir / "claude-config"),
            "home_dir": str(HOME),
        }

        # settings.json — IMMER neu rendern (cli_plugins als Source of Truth)
        settings_file = agent_dir / "settings.json"
        cli_plugins = kwargs.get("cli_plugins")
        enabled_plugins_list = _resolve_enabled_plugins(cli_plugins)
        # enabledPlugins muss IMMER ein Record/Object sein — sowohl claude-Binary
        # als auch openclaude (= identisches Schema, openclaude ist Fork von Claude Code).
        # Zuvor war das für openclaude als Array — das ist falsch, schlägt Schema-Validation
        # fehl und openclaude verwirft dann die komplette settings.json (inkl.
        # skipDangerousModePermissionPrompt → Bypass-Dialog erscheint bei jedem Start).
        enabled_plugins_val = {k: True for k in enabled_plugins_list}
        settings_ctx = {
            **ctx,
            "enabled_plugins": enabled_plugins_val,
            "extra_marketplaces": _resolve_extra_marketplaces(cli_plugins),
        }
        rendered = env.get_template("cli_agent_settings.json.j2").render(settings_ctx)
        settings_file.write_text(rendered)
        created.append("settings.json")

        # claude-config/settings.json — echte Kopie (KEIN Symlink).
        # Früher Symlink auf ../settings.json, aber das bricht im Docker-Mount
        # (claude-config/ wird nach /home/agent/.claude/ gemountet — das Parent
        # liegt ausserhalb des Mounts). Jetzt schreiben wir an beide Stellen;
        # bleibt synchron weil beide aus demselben Template rendern.
        claude_config_settings = claude_config_dst / "settings.json"
        if claude_config_settings.is_symlink():
            claude_config_settings.unlink()
        claude_config_settings.write_text(rendered)
        created.append("claude-config/settings.json")

        # agent.env
        agent_env_file = agent_dir / "agent.env"
        rendered = env.get_template("cli_agent.env.j2").render(ctx)
        agent_env_file.write_text(rendered)
        agent_env_file.chmod(0o600)
        created.append("agent.env")

        # worker.sh — immer neu erstellen (cli_bin kann sich aendern)
        worker_file = agent_dir / "worker.sh"
        rendered = env.get_template("cli_agent_worker.sh.j2").render(ctx)
        worker_file.write_text(rendered)
        worker_file.chmod(0o755)
        created.append("worker.sh")

    except Exception as e:
        return {"ok": False, "created": created, "error": f"Template error: {e}"}

    log(f"Agent provisioned: {agent_name} → {created}")
    return {"ok": True, "created": created}


def _deprovision_agent(agent_name: str) -> dict:
    """Entfernt Agent-Verzeichnis komplett."""
    import shutil as _shutil
    agent_dir = AGENTS_DIR / agent_name
    if not agent_dir.exists():
        return {"ok": False, "error": "Agent-Verzeichnis nicht gefunden"}
    if agent_name == "_template":
        return {"ok": False, "error": "Kann _template nicht loeschen"}
    _shutil.rmtree(str(agent_dir))
    log(f"Agent deprovisioned: {agent_name}")
    return {"ok": True}


def _provision_status(agent_name: str) -> dict:
    """Gibt Provisioning-Status eines Agents zurueck."""
    agent_dir = AGENTS_DIR / agent_name
    has_settings = (agent_dir / "settings.json").exists()
    has_worker = (agent_dir / "worker.sh").exists()
    has_queue = (agent_dir / "queue").exists()
    has_agent_env = (agent_dir / "agent.env").exists()
    has_claude_config = (agent_dir / "claude-config").exists()

    result = _sp.run(
        [TMUX_BIN, "has-session", "-t", agent_name],
        capture_output=True,
    )
    worker_running = result.returncode == 0

    return {
        "agent_name": agent_name,
        "provisioned": has_settings and has_worker and has_queue and has_agent_env,
        "has_settings": has_settings,
        "has_worker": has_worker,
        "has_queue": has_queue,
        "has_agent_env": has_agent_env,
        "has_claude_config": has_claude_config,
        "worker_running": worker_running,
    }


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_env():
    env = os.environ.copy()
    env["FREE_CODE_MODEL_PROFILES_FILE"] = MODEL_PROFILES
    env["HOME"] = str(HOME)
    if Path(CLI_ENV).exists():
        with open(CLI_ENV) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    return env


def _settings_path(agent_name: str) -> str:
    return str(AGENTS_DIR / agent_name / "settings.json")


def _session_name(agent_name: str, task_id: str) -> str:
    return f"{agent_name}-{task_id[:8]}"


def start_agent(agent_name: str, task_id: str, workspace: str, prompt: str) -> str:
    session = _session_name(agent_name, task_id)
    settings_file = _settings_path(agent_name)

    if not Path(settings_file).exists():
        msg = f"settings.json nicht gefunden: {settings_file}"
        log(f"ERROR: {msg}")
        return f"error: {msg}"

    # Evtl. alte Session bereinigen
    _sp.run([TMUX_BIN, "kill-session", "-t", session], capture_output=True)

    env = load_env()

    prompt_file = str(LOG_DIR / f"prompt-{task_id[:8]}.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)

    cmd = (
        f"cd {shlex.quote(workspace)} && "
        f"FREE_CODE_MODEL_PROFILES_FILE={shlex.quote(env.get('FREE_CODE_MODEL_PROFILES_FILE', MODEL_PROFILES))} "
        f"{shlex.quote(CLI_BIN)} "
        f"--settings {shlex.quote(settings_file)} "
        f"--dangerously-skip-permissions "
        f'-p "$(cat {shlex.quote(prompt_file)})"'
    )

    try:
        _sp.run(
            [TMUX_BIN, "new-session", "-d", "-s", session, "-x", "220", "-y", "50"],
            check=True, capture_output=True,
        )
        _sp.run(
            [TMUX_BIN, "send-keys", "-t", session, cmd, "Enter"],
            check=True, capture_output=True,
        )
        active_sessions[task_id] = session
        log(f"CLI agent started: agent={agent_name} task_id={task_id} session={session} workspace={workspace}")
        return f"started: session={session}"
    except _sp.CalledProcessError as e:
        log(f"ERROR starting tmux session: {e.stderr}")
        return f"error: {e}"


def get_output(task_id: str) -> Optional[str]:
    session = active_sessions.get(task_id)
    if not session:
        result = _sp.run(
            [TMUX_BIN, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        for name in result.stdout.strip().splitlines():
            if f"-{task_id[:8]}" in name:
                session = name
                break
    if not session:
        return None
    result = _sp.run(
        [TMUX_BIN, "capture-pane", "-t", session, "-p", "-e", "-S", "-"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def send_input(task_id: str, text: str) -> bool:
    session = active_sessions.get(task_id)
    if not session:
        result = _sp.run(
            [TMUX_BIN, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        for name in result.stdout.strip().splitlines():
            if f"-{task_id[:8]}" in name:
                session = name
                break
    if not session:
        return False
    result = _sp.run(
        [TMUX_BIN, "send-keys", "-t", session, text, "Enter"],
        capture_output=True,
    )
    return result.returncode == 0


def kill_session(task_id: str) -> bool:
    session = active_sessions.get(task_id)
    if not session:
        result = _sp.run(
            [TMUX_BIN, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        for name in result.stdout.strip().splitlines():
            if f"-{task_id[:8]}" in name:
                session = name
                break
    if not session:
        return False
    result = _sp.run(
        [TMUX_BIN, "kill-session", "-t", session],
        capture_output=True,
    )
    active_sessions.pop(task_id, None)
    return result.returncode == 0


def list_sessions() -> list[dict]:
    """Alle laufenden Agent-tmux-Sessions auflisten."""
    result = _sp.run(
        [TMUX_BIN, "list-sessions", "-F",
         "#{session_name}\t#{session_created}\t#{session_windows}"],
        capture_output=True, text=True,
    )
    sessions = []
    agent_names = _known_agent_names()
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0]
        segments = name.rsplit("-", 1)
        # Accept: {agent}-{8chars} (per-task) OR just {agent} (permanent) OR {agent}-shell
        is_shell = name.endswith("-shell") and name[:-6] in agent_names
        is_per_task = not is_shell and len(segments) == 2 and len(segments[1]) == 8
        is_permanent = not is_shell and name in agent_names
        if not is_per_task and not is_permanent and not is_shell:
            continue
        if is_shell:
            task_prefix = name  # e.g. "freecode-shell"
        elif is_per_task:
            task_prefix = segments[1]
        else:
            task_prefix = name
        task_id = next(
            (tid for tid, sname in active_sessions.items() if sname == name),
            task_prefix if is_per_task else name,
        )
        created_ts = int(parts[1]) if parts[1].isdigit() else 0
        elapsed = int(time.time()) - created_ts
        sessions.append({
            "task_id": task_id,
            "session": name,
            "elapsed_seconds": elapsed,
            "permanent": is_permanent,
            "shell": is_shell,
        })
    return sessions


# --- Agent-Image Build Endpoints (Task 4, CLI-Tool-Updates) -----------------
#
# Baut mc-claude-agent / mc-agent-base / mc-omp-agent Images via
# scripts/build-agent-images.sh im Hintergrund-Thread. Version wird als
# env-Override gesetzt (Task-1-Kontrakt: OPENCLAUDE_VERSION / CLAUDE_VERSION /
# OMP_VERSION / OMP_SHA256 gewinnen über docker/cli-versions.json).
BUILD_LOG_FILE = HOME / ".mc" / "logs" / "agent-image-build.log"
BUILD_SCRIPT = Path(__file__).parent / "build-agent-images.sh"
REPO_ROOT = Path(__file__).resolve().parent.parent

_HARNESS_ARG = {"openclaude": "openclaude", "claude": "claude", "omp": "omp"}

# Versions-Strings fliessen in env-Overrides und (omp-sha256) in eine
# GitHub-Download-URL — striktes Charset verhindert Pfad-Tricks (`../`)
# innerhalb der URL. Defense-in-Depth, kein Ersatz für die Allowlist oben.
_VERSION_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}$")

# Hängender `docker build` würde sonst state=running + Thread für immer
# halten und jeden Folge-Build bis zum Bridge-Neustart blockieren.
_BUILD_TIMEOUT_S = 1800

_build_lock = threading.Lock()
_build_state = {
    "state": "idle",  # idle|running|success|failed
    "tool": None,
    "returncode": None,
    "thread": None,
}


def _build_log_tail(n=80) -> str:
    if not BUILD_LOG_FILE.exists():
        return ""
    lines = BUILD_LOG_FILE.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


def _run_agent_image_build(tool: str, version: str, sha256: Optional[str]):
    """Läuft im Hintergrund-Thread — führt build-agent-images.sh aus, schreibt Log."""
    env = os.environ.copy()
    # launchd startet die Bridge mit minimalem PATH (kein /usr/local/bin) —
    # ohne diese Ergänzung endet der Build mit "docker: command not found"
    # (returncode 127, live gesehen beim ersten E2E 2026-07-05).
    extra_paths = ("/usr/local/bin", "/opt/homebrew/bin")
    path_parts = env.get("PATH", "").split(":")
    env["PATH"] = ":".join(path_parts + [p for p in extra_paths if p not in path_parts])
    if tool == "openclaude":
        env["OPENCLAUDE_VERSION"] = version
    elif tool == "claude":
        env["CLAUDE_VERSION"] = version
    elif tool == "omp":
        env["OMP_VERSION"] = version
        if sha256:
            env["OMP_SHA256"] = sha256

    harness_arg = _HARNESS_ARG[tool]
    log(f"Agent image build started: tool={tool} version={version}")
    try:
        with open(BUILD_LOG_FILE, "w") as logf:
            proc = _sp.run(
                ["bash", str(BUILD_SCRIPT), harness_arg],
                cwd=str(REPO_ROOT), env=env,
                stdout=logf, stderr=_sp.STDOUT,
                timeout=_BUILD_TIMEOUT_S,
            )
        returncode = proc.returncode
    except _sp.TimeoutExpired:
        log(f"ERROR agent image build timed out after {_BUILD_TIMEOUT_S}s (tool={tool})")
        with open(BUILD_LOG_FILE, "a") as logf:
            logf.write(f"\n[bridge] ERROR build timed out after {_BUILD_TIMEOUT_S}s — killed\n")
        returncode = -1
    except Exception as e:
        log(f"ERROR running agent image build: {e}")
        with open(BUILD_LOG_FILE, "a") as logf:
            logf.write(f"\n[bridge] ERROR launching build: {e}\n")
        returncode = -1

    with _build_lock:
        _build_state["returncode"] = returncode
        _build_state["state"] = "success" if returncode == 0 else "failed"
    log(f"Agent image build finished: tool={tool} returncode={returncode}")


def _start_agent_image_build(tool: str, version: str, sha256: Optional[str]) -> Optional[str]:
    """Startet Build-Thread. Gibt Fehlermeldung zurück wenn bereits ein Build läuft."""
    with _build_lock:
        thread = _build_state.get("thread")
        if thread is not None and thread.is_alive():
            return "build läuft bereits"
        BUILD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        BUILD_LOG_FILE.write_text("")  # truncate pro Lauf
        _build_state["state"] = "running"
        _build_state["tool"] = tool
        _build_state["returncode"] = None
        new_thread = threading.Thread(
            target=_run_agent_image_build, args=(tool, version, sha256), daemon=True,
        )
        _build_state["thread"] = new_thread
        new_thread.start()
    return None


def _fetch_omp_sha256(version: str) -> dict:
    """Lädt das omp-Release-Binary temporär, berechnet sha256, löscht es wieder (TOFU)."""
    import hashlib
    import tempfile
    import urllib.request
    import urllib.error

    url = f"https://github.com/can1357/oh-my-pi/releases/download/v{version}/omp-linux-arm64"
    fd, tmp_path = tempfile.mkstemp(prefix="omp-sha256-")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mission-control-cli-bridge"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        h = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return {"ok": True, "sha256": h.hexdigest()}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"ERROR omp-sha256 download failed (version={version}): {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body)

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok", "bridge": "cli-agent"})

        elif self.path.startswith("/status/"):
            # Always returns "unknown" — tmux bridge does not track Popen objects.
            # The backend poller (_poll_bridge_status) exits gracefully on "unknown".
            task_id = self.path.split("/status/")[1]
            self._json({"task_id": task_id, "status": "unknown"})

        elif self.path.startswith("/queue/status/"):
            # Format: /queue/status/{agent_name}/{task_id}
            parts = self.path.split("/queue/status/")[1].split("/")
            if len(parts) == 2:
                agent_name, task_id = parts
                status = _queue_status(agent_name, task_id)
                self._json({"task_id": task_id, "status": status})
            else:
                self.send_response(400)
                self.end_headers()

        elif self.path == "/sessions":
            self._json(list_sessions())

        elif self.path.startswith("/output/"):
            task_id = self.path.split("/output/")[1]
            output = get_output(task_id)
            if output is None:
                self.send_response(404)
                self.end_headers()
            else:
                self._json({"task_id": task_id, "output": output})

        elif self.path.startswith("/provision/"):
            agent_name = self.path.split("/provision/")[1]
            self._json(_provision_status(agent_name))

        elif self.path == "/agent-images/build/status":
            with _build_lock:
                state = _build_state["state"]
                tool = _build_state["tool"]
                returncode = _build_state["returncode"]
            self._json({
                "state": state, "tool": tool, "returncode": returncode,
                "log_tail": _build_log_tail(),
            })

        elif self.path == "/plugins":
            master = PLUGINS_DIR / "installed_plugins.json"
            if not master.exists():
                self._json({"plugins": [], "error": "shared cache nicht gefunden"})
                return
            try:
                data = json.loads(master.read_text())
                plugins_raw = data.get("plugins", {})
                plugins = []
                for key, entries in plugins_raw.items():
                    parts = key.split("@", 1)
                    version = "unknown"
                    if isinstance(entries, list) and entries:
                        version = entries[0].get("version", "unknown")
                    plugins.append({
                        "key": key,
                        "name": parts[0],
                        "source": parts[1] if len(parts) > 1 else "unknown",
                        "version": version,
                    })
                self._json({"plugins": sorted(plugins, key=lambda p: p["name"])})
            except Exception as e:
                self._json({"plugins": [], "error": str(e)})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/start":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
            except Exception as e:
                self.send_error(400, str(e))
                return

            agent_name = data.get("agent_name", "freecode")
            task_id = data.get("task_id", str(uuid.uuid4())[:8])
            workspace = data.get("workspace", str(HOME / "Workspace"))
            prompt = data.get("prompt", "")

            result = start_agent(agent_name, task_id, workspace, prompt)
            self._json({"task_id": task_id, "result": result})

        elif self.path.startswith("/input/"):
            task_id = self.path.split("/input/")[1]
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
            except Exception as e:
                self.send_error(400, str(e))
                return
            text = data.get("text", "")
            ok = send_input(task_id, text)
            self._json({"ok": ok})

        elif self.path == "/enqueue":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
            except Exception as e:
                self.send_error(400, str(e))
                return

            agent_name = data.get("agent_name", "freecode")
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace = data.get("workspace", str(HOME / "Workspace"))
            prompt = data.get("prompt", "")

            # Worker sicherstellen
            _start_worker_session(agent_name)

            result = _enqueue_task(agent_name, task_id, workspace, prompt)
            self._json({"task_id": task_id, "result": result})

        elif self.path.startswith("/provision/"):
            agent_name = self.path.split("/provision/")[1]
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
            except Exception as e:
                self.send_error(400, str(e))
                return
            mc_agent_token = data.get("mc_agent_token", "")
            model = data.get("model", "nvidia/nemotron-3-super")
            system_prompt = data.get("system_prompt",
                f"Du bist {agent_name}, ein autonomer Developer Agent in Mission Control. "
                f"Bearbeite zugewiesene Tasks selbststaendig.\n\n"
                f"Verhaltens-Grundsaetze:\n"
                f"- ACK sofort am Anfang (PATCH status: in_progress)\n"
                f"- Bei Blockierung sofort blocked setzen + Kommentar\n"
                f"- Kein Task gilt als fertig ohne status: review gesetzt\n"
                f"- Nach max. 5 Minuten ohne Fortschritt → blocked\n\n"
                f"Progress-Kommentar Format:\n"
                f"**Update** — Was getan\n**Evidence** — Dateipfade, Test-Output\n**Next** — Naechste Schritte"
            )
            extra_plugins = data.get("extra_plugins", [])
            cli_plugins = data.get("cli_plugins")
            cli_bin = data.get("cli_bin")  # None = use default (openclaude)
            result = _provision_agent(agent_name, mc_agent_token, model, system_prompt, extra_plugins,
                                      cli_plugins=cli_plugins, cli_bin=cli_bin)
            if result["ok"]:
                worker_ok = _start_worker_session(agent_name)
                result["worker_started"] = worker_ok
                if not worker_ok:
                    result["ok"] = False
                    result["error"] = "Worker-Session konnte nicht gestartet werden (worker.sh fehlt?)"
            self._json(result)

        elif self.path.startswith("/worker/") and self.path.endswith("/restart"):
            # POST /worker/{agent_name}/restart — Kill + Neustart der Worker-Session
            agent_name = self.path.split("/worker/")[1].split("/restart")[0]
            session = agent_name
            import time as _time
            # Worker killen
            _sp.run([TMUX_BIN, "kill-session", "-t", f"={session}"], capture_output=True)
            log(f"Worker session gestoppt: {session}")
            _time.sleep(0.5)
            worker_ok = _start_worker_session(agent_name)
            self._json({"ok": worker_ok, "agent": agent_name, "session": session,
                        "worker_started": worker_ok})

        elif self.path == "/plugins/shell":
            ok = _start_plugins_shell()
            self._json({"ok": ok, "session": "plugins-shell"})

        elif self.path == "/plugins/install":
            body = self._read_json()
            plugin_key = body.get("plugin_key", "")
            if not plugin_key:
                self._json({"ok": False, "error": "plugin_key required"}, status=400)
                return
            config_dir = str(PLUGINS_DIR.parent / "plugin-store")
            cmd = f"{CLI_BIN} plugins install {shlex.quote(plugin_key)}"
            log(f"Installing plugin: {plugin_key}")
            try:
                result = _sp.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=120,
                    env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                )
                if result.returncode == 0:
                    log(f"Plugin installed: {plugin_key}")
                    self._json({"ok": True, "plugin_key": plugin_key, "output": result.stdout})
                else:
                    log(f"Plugin install failed: {result.stderr}")
                    self._json({"ok": False, "error": result.stderr or "Installation fehlgeschlagen"})
            except _sp.TimeoutExpired:
                self._json({"ok": False, "error": "Timeout (120s)"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif self.path.startswith("/plugins/") and self.path.endswith("/update"):
            plugin_key = self.path[len("/plugins/"):-len("/update")]
            config_dir = str(PLUGINS_DIR.parent / "plugin-store")
            cmd = f"{CLI_BIN} plugins update {shlex.quote(plugin_key)}"
            log(f"Updating plugin: {plugin_key}")
            try:
                result = _sp.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=120,
                    env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                )
                self._json({"ok": result.returncode == 0, "output": result.stdout, "error": result.stderr or None})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif self.path == "/agent-images/build":
            try:
                body = self._read_json()
            except Exception as e:
                self.send_error(400, str(e))
                return
            tool = body.get("tool", "")
            version = body.get("version", "")
            sha256 = body.get("sha256")
            if tool not in _HARNESS_ARG:
                self._json({"error": f"unbekanntes tool: {tool}"}, status=400)
                return
            if not version or not _VERSION_RE.match(version):
                self._json({"error": "version required (charset [0-9A-Za-z._-], max 64)"}, status=400)
                return
            if sha256 is not None and not _VERSION_RE.match(str(sha256)):
                self._json({"error": "sha256 hat ungültiges Format"}, status=400)
                return
            error = _start_agent_image_build(tool, version, sha256)
            if error:
                self._json({"error": error}, status=409)
                return
            self._json({"status": "started"})

        elif self.path == "/agent-images/omp-sha256":
            try:
                body = self._read_json()
            except Exception as e:
                self.send_error(400, str(e))
                return
            version = body.get("version", "")
            if not version or not _VERSION_RE.match(version):
                self._json({"error": "version required (charset [0-9A-Za-z._-], max 64)"}, status=400)
                return
            result = _fetch_omp_sha256(version)
            if result["ok"]:
                self._json({"sha256": result["sha256"]})
            else:
                self._json({"error": result["error"]}, status=502)

        elif self.path == "/restart":
            self._json({"ok": True, "message": "Bridge wird neu gestartet..."})
            def _do_restart():
                time.sleep(0.5)
                log("Bridge restart via /restart endpoint")
                os.execv(sys.executable, [sys.executable] + sys.argv)
            threading.Thread(target=_do_restart, daemon=True).start()

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/provision/"):
            agent_name = self.path.split("/provision/")[1]
            result = _deprovision_agent(agent_name)
            self._json(result)

        elif self.path == "/plugins/shell":
            session = "plugins-shell"
            result = _sp.run(
                [TMUX_BIN, "kill-session", "-t", session],
                capture_output=True,
            )
            self._json({"ok": result.returncode == 0, "session": session})

        elif self.path.startswith("/plugins/"):
            plugin_key = self.path[len("/plugins/"):]
            config_dir = str(PLUGINS_DIR.parent / "plugin-store")
            cmd = f"{CLI_BIN} plugins remove {shlex.quote(plugin_key)}"
            log(f"Removing plugin: {plugin_key}")
            try:
                result = _sp.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=30,
                    env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                )
                self._json({"ok": result.returncode == 0, "output": result.stdout, "error": result.stderr or None})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif self.path.startswith("/sessions/"):
            task_id = self.path.split("/sessions/")[1]
            ok = kill_session(task_id)
            self._json({"ok": ok})
        else:
            self.send_response(404)
            self.end_headers()


WS_PORT = 18793

async def _pty_ws_handler(websocket):
    """WebSocket handler: proxies browser ↔ PTY ↔ tmux attach."""
    # Path format: /{task_id}
    path = websocket.request.path  # websockets v13+ uses request.path
    task_id = path.strip("/")

    # Find session name
    session = active_sessions.get(task_id)
    if not session:
        # Try to find by exact name (permanent session) or prefix (per-task session)
        result = _sp.run(
            [TMUX_BIN, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        for name in result.stdout.strip().splitlines():
            if name == task_id or f"-{task_id[:8]}" in name:
                session = name
                break

    if not session:
        log(f"WS: session not found for task_id={task_id}")
        await websocket.close(1008, "Session not found")
        return

    log(f"WS PTY attach: task_id={task_id} session={session}")

    # tmux Mouse-Mode aktivieren damit der Browser-Client scrollen kann.
    # Ohne mouse on: tmux sendet kein ?1000h an xterm → kein Mouse-Tracking
    # → Scroll-Events werden als Cursor-Keys gesendet (^[[A/^[[B im Terminal).
    _sp.run(
        [TMUX_BIN, "set-option", "-t", session, "mouse", "on"],
        capture_output=True,
    )

    # Open PTY
    master_fd, slave_fd = pty.openpty()
    # Set default terminal size (220 cols x 50 rows — matches session creation)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0))

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    proc = _sp.Popen(
        [TMUX_BIN, "attach-session", "-t", session],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True,
        env=env,
    )
    os.close(slave_fd)

    loop = asyncio.get_running_loop()

    async def read_pty_to_ws():
        """PTY output → WebSocket."""
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                if not data:
                    break
                await websocket.send(data)
            except OSError:
                break
            except Exception as e:
                log(f"WS PTY read error: {e}")
                break

    async def read_ws_to_pty():
        """WebSocket input → PTY."""
        async for msg in websocket:
            try:
                if isinstance(msg, bytes):
                    os.write(master_fd, msg)
                else:
                    # Text frame: try JSON control message first (resize),
                    # fall back to raw keystroke write
                    try:
                        ctrl = json.loads(msg)
                        if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                            cols = int(ctrl.get("cols", 220))
                            rows = int(ctrl.get("rows", 50))
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                        struct.pack("HHHH", rows, cols, 0, 0))
                        else:
                            # Valid JSON but not a control message (e.g. bare number like "1")
                            os.write(master_fd, msg.encode("utf-8"))
                    except (json.JSONDecodeError, ValueError):
                        # Plain keystroke string from xterm.js
                        os.write(master_fd, msg.encode("utf-8"))
            except Exception as e:
                log(f"WS write error: {e}")
                break

    try:
        await asyncio.gather(read_pty_to_ws(), read_ws_to_pty())
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass
        log(f"WS PTY closed: session={session}")


async def _run_ws_server():
    import websockets as _ws
    async with _ws.serve(_pty_ws_handler, "0.0.0.0", WS_PORT):
        log(f"WS PTY server listening on ws://0.0.0.0:{WS_PORT}")
        await asyncio.Future()  # run forever


def _start_ws_server():
    """Start WebSocket server in its own asyncio event loop (separate thread)."""
    asyncio.run(_run_ws_server())


if __name__ == "__main__":
    log(f"CLI Agent Bridge starting on port {PORT}")
    _check_tmux()
    ws_thread = threading.Thread(target=_start_ws_server, daemon=True)
    ws_thread.start()
    log(f"WS PTY server started on port {WS_PORT}")
    # Worker-Sessions für alle konfigurierten Agents starten
    for agent_dir in AGENTS_DIR.iterdir():
        if agent_dir.is_dir() and (agent_dir / "settings.json").exists() and (agent_dir / "worker.sh").exists():
            _start_worker_session(agent_dir.name)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on http://0.0.0.0:{PORT}")
    server.serve_forever()
