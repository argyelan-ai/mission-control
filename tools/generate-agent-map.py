#!/usr/bin/env python3
"""
Agent State Map Generator fuer Mission Control.

Scannt den CLI-Bridge-Agent-Layer und generiert docs/agent-state.md mit:
- Binary-Chain (openclaude / claude — je Agent-Runtime)
- Alle CLI-Bridge-Agents aus der DB (Modell, Status, Scopes)
- Filesystem-Check pro Agent (Verzeichnis, Settings-Symlink, worker.sh, agent.env)
- tmux-Session-Status (Worker + Shell)
- Queue-Status (pending/running Tasks)
- Env-Chain-Zusammenfassung

Nur stdlib + psql — kein pip install noetig.
Ausfuehren: python3 tools/generate-agent-map.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = Path.home() / ".mc" / "agents"
# Host-Sandbox-Dir für shared env / model-profiles (openclaude für Sparky).
SANDBOXES = Path.home() / "Workspace" / "Sandboxes" / "openclaude-local"
OUTPUT = PROJECT_ROOT / "docs" / "agent-state.md"

def _db_password() -> str:
    """DB-Passwort aus Env oder .env lesen — nie hardcoden (Repo ist public)."""
    if pw := os.environ.get("DB_PASSWORD"):
        return pw
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("DB_PASSWORD="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


DB_PASSWORD = _db_password()
DB_USER = "mc"
DB_NAME = "mission_control"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: str, **kwargs) -> str:
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10, **kwargs
        )
        return result.stdout.strip()
    except Exception:
        return ""


def tmux_sessions() -> set[str]:
    out = run("tmux ls 2>/dev/null")
    sessions = set()
    for line in out.splitlines():
        name = line.split(":")[0].strip()
        sessions.add(name)
    return sessions


def db_query(sql: str) -> list[dict]:
    cmd = f'PGPASSWORD={DB_PASSWORD} psql -U {DB_USER} -d {DB_NAME} -h localhost -p 5432 -t -A -F"|" -c "{sql}" 2>/dev/null'
    out = run(cmd)
    if not out:
        # try via docker
        cmd = f'docker compose -f {PROJECT_ROOT}/docker-compose.yml exec -T db sh -c "PGPASSWORD={DB_PASSWORD} psql -U {DB_USER} -d {DB_NAME} -t -A -F\'|\' -c \'{sql}\'" 2>/dev/null'
        out = run(cmd)
    rows = []
    for line in out.splitlines():
        if line.strip():
            rows.append(line.split("|"))
    return rows


def read_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def check_symlink(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "FEHLT"
    if path.is_symlink():
        target = os.readlink(path)
        return f"symlink → {target}"
    return "eigenstaendig (kein Symlink!)"


def claude_config_settings_ok(fs: dict) -> bool:
    """Prueft ob claude-config/settings.json vorhanden ist (Symlink oder echte Kopie — beides OK)."""
    return "FEHLT" not in fs.get("claude_config_symlink", "FEHLT")


def queue_count(agent_dir: Path, sub: str) -> int:
    d = agent_dir / "queue" / sub
    if not d.exists():
        return 0
    return len(list(d.glob("*.json")))


# ---------------------------------------------------------------------------
# Binary Chain
# ---------------------------------------------------------------------------

def section_binary_chain() -> str:
    lines = ["## Binary Chain\n"]

    openclaude_path = Path(run("which openclaude 2>/dev/null") or str(Path.home() / ".npm-global" / "bin" / "openclaude"))

    # openclaude version (Sparky + Plugin-Shell)
    oc_version = run(f"{openclaude_path} --version 2>/dev/null")
    lines.append(f"- `openclaude` Version: `{oc_version or 'unbekannt'}` (Path: `{openclaude_path}`)")

    # claude native (Boss + 9 Docker-Agents nach Claude-Fleet Migration)
    claude_path = Path.home() / ".local" / "bin" / "claude"
    claude_version = run(f"{claude_path} --version 2>/dev/null") if claude_path.exists() else "nicht gefunden"
    lines.append(f"- `claude` Version: `{claude_version}` (Path: `{claude_path}`)")

    # node (Runtime für openclaude + Plugins)
    node_path = run("which node 2>/dev/null")
    node_version = run("node --version 2>/dev/null")
    lines.append(f"- `node` → `{node_path}` ({node_version})")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared Env
# ---------------------------------------------------------------------------

def section_shared_env() -> str:
    lines = ["## Shared Env (openclaude.local.env)\n"]
    env_file = SANDBOXES / "openclaude.local.env"
    env = read_env_file(env_file)

    important = [
        "CLAUDE_CODE_USE_OPENAI", "OPENAI_BASE_URL", "OPENAI_MODEL",
        "CLAUDE_CONFIG_DIR", "MC_BASE_URL",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ]
    for key in important:
        val = env.get(key, "(nicht gesetzt)")
        lines.append(f"- `{key}` = `{val}`")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agents aus DB
# ---------------------------------------------------------------------------

def fetch_db_agents() -> list[dict]:
    rows = db_query(
        "SELECT id, name, gateway_agent_id, model, agent_runtime, provision_status, "
        "scopes FROM agents ORDER BY created_at"
    )
    agents = []
    for r in rows:
        if len(r) >= 6:
            agents.append({
                "id": r[0], "name": r[1], "gateway_id": r[2],
                "model": r[3], "runtime": r[4], "provision": r[5],
                "scopes": r[6] if len(r) > 6 else "[]",
            })
    return agents


# ---------------------------------------------------------------------------
# Agent Filesystem Check
# ---------------------------------------------------------------------------

def agent_fs_info(slug: str, name: str = "") -> dict:
    # Versuche verschiedene Directory-Namen — DB-slug stimmt nicht immer mit Filesystem überein
    candidates = [
        slug,
        slug.replace("-", ""),   # free-code → freecode
        slug.replace("-", "_"),
        name.lower(),             # Sparky → sparky
        name.lower().replace(" ", "-"),
        name.lower().replace(" ", ""),
    ]
    # Bevorzuge Verzeichnis mit worker.sh (= vollständig provisioniert)
    d = None
    for candidate in dict.fromkeys(candidates):  # deduplizieren, Reihenfolge behalten
        p = AGENTS_DIR / candidate
        if p.exists() and (p / "worker.sh").exists():
            d = p
            break
    # Fallback: erstes existierendes Verzeichnis
    if d is None:
        for candidate in dict.fromkeys(candidates):
            p = AGENTS_DIR / candidate
            if p.exists():
                d = p
                break
    if d is None:
        return {"exists": False}

    settings = d / "settings.json"
    agent_env = d / "agent.env"
    worker = d / "worker.sh"
    claude_config_settings = d / "claude-config" / "settings.json"

    # Model aus settings.json
    model_in_file = "?"
    if settings.exists() or settings.is_symlink():
        try:
            data = json.loads(settings.read_text())
            model_in_file = data.get("model", "?")
        except Exception:
            model_in_file = "parse-fehler"

    # CLI_BIN aus worker.sh
    cli_bin = "?"
    if worker.exists():
        for line in worker.read_text().splitlines():
            if line.startswith("CLI_BIN="):
                cli_bin = line.split("=", 1)[1].strip('"')
                break

    # Token aus agent.env (nur erste 8 Zeichen)
    token_preview = "?"
    env_data = read_env_file(agent_env)
    token = env_data.get("MC_AGENT_TOKEN", "")
    token_preview = token[:8] + "..." if token else "FEHLT"

    # ANTHROPIC_MODEL override?
    model_override = env_data.get("ANTHROPIC_MODEL", "")

    return {
        "exists": True,
        "dir_name": d.name,  # Tatsaechlicher Verzeichnis-/Session-Name
        "settings_symlink": check_symlink(settings),
        "claude_config_symlink": check_symlink(claude_config_settings),
        "model_in_file": model_in_file,
        "model_override": model_override,
        "cli_bin": Path(cli_bin).name if cli_bin != "?" else "?",
        "worker_exists": worker.exists(),
        "agent_env_exists": agent_env.exists(),
        "token_preview": token_preview,
        "queue_pending": queue_count(d, "pending"),
        "queue_running": queue_count(d, "running"),
    }


# ---------------------------------------------------------------------------
# Main Section: Agent-Tabelle
# ---------------------------------------------------------------------------

def section_agents(db_agents: list[dict], sessions: set[str]) -> str:
    lines = ["## CLI Bridge Agents\n"]

    cli_agents = [a for a in db_agents if a["runtime"] == "cli-bridge"]
    openclaw_agents = [a for a in db_agents if a["runtime"] != "cli-bridge"]

    # CLI Bridge Tabelle
    lines.append("### cli-bridge Agents\n")
    lines.append("| Name | Slug | DB-Modell | settings.json Modell | CLI-Bin | Worker-tmux | Shell-tmux | Queue P/R | Symlink |")
    lines.append("|------|------|-----------|----------------------|---------|-------------|------------|-----------|---------|")

    for a in cli_agents:
        slug = a["gateway_id"] or a["name"].lower()
        fs = agent_fs_info(slug, a["name"])

        # tmux-Session-Name = Verzeichnisname (nicht DB-slug!)
        session_name = fs.get("dir_name", slug) if fs.get("exists") else slug
        worker_ok = "✓" if session_name in sessions else "✗"
        shell_ok = "✓" if f"{session_name}-shell" in sessions else "-"

        if not fs["exists"]:
            lines.append(f"| {a['name']} | `{slug}` | {a['model']} | DIR FEHLT | - | {worker_ok} | {shell_ok} | - | - |")
            continue

        override = f" ({fs['model_override']})" if fs["model_override"] else ""
        # claude-config/settings.json ist eine echte Kopie (kein Symlink) — das ist by design (ADR-013)
        cc_ok = "✓" if claude_config_settings_ok(fs) else "⚠"
        queue = f"{fs['queue_pending']}/{fs['queue_running']}"

        lines.append(
            f"| {a['name']} | `{slug}` | {a['model']} | {fs['model_in_file']}{override} "
            f"| `{fs['cli_bin']}` | {worker_ok} | {shell_ok} | {queue} | {cc_ok} |"
        )

    lines.append("")

    # Host-Side Agents (Phase 24: Hermes Worker)
    host_agents = [a for a in openclaw_agents if a["runtime"] == "host"]
    if host_agents:
        lines.append("### Host-Side Agents (launchd)\n")
        lines.append("| Name | Workspace | Modell | tmux Session | plist | Status |")
        lines.append("|------|-----------|--------|--------------|-------|--------|")
        for a in host_agents:
            slug = a["name"].lower()
            # Hermes-specific tmux session name (ADR-029); generic fallback uses slug
            tmux_name = "hermes-worker" if slug == "hermes" else f"{slug}-worker"
            tmux_ok = "✓" if tmux_name in sessions else "✗"
            plist_name = "com.mc.hermes-bridge.plist" if slug == "hermes" else f"com.mc.{slug}-bridge.plist"
            lines.append(
                f"| {a['name']} | `{Path.home()}/.mc/agents/{slug}` "
                f"| {a['model'] or '-'} | `{tmux_name}` {tmux_ok} | `{plist_name}` | {a['provision']} |"
            )
        lines.append("")

    # OpenClaw / andere Agents (kurz)
    other_agents = [a for a in openclaw_agents if a["runtime"] != "host"]
    if other_agents:
        lines.append("### openclaw / andere Agents\n")
        lines.append("| Name | Gateway-ID | Modell | Runtime | Status |")
        lines.append("|------|-----------|--------|---------|--------|")
        for a in other_agents:
            lines.append(f"| {a['name']} | `{a['gateway_id'] or '-'}` | {a['model'] or '-'} | {a['runtime']} | {a['provision']} |")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Provisioning-Ablauf
# ---------------------------------------------------------------------------

def section_provisioning() -> str:
    return """## Provisioning-Flow (CLI Bridge)

```
POST /api/v1/agents/{id}/provision
  → cli_terminal.py: liest model, generiert Token, baut bridge_payload
  → is_claude_model? (model.startswith("claude-"))
      ja  → cli_bin = $HOME/.local/bin/claude  (Anthropic API direkt)
      nein → cli_bin = $HOME/.npm-global/bin/openclaude (+ LM Studio env)
  → POST cli-bridge.py /provision/{slug}
      1. queue/ Verzeichnisse anlegen
      2. _template/claude-config/ → agent/claude-config/ kopieren
      3. Jinja2: settings.json, agent.env, worker.sh rendern
      4. claude-config/settings.json → Symlink auf ../settings.json (Single Source of Truth)
      5. tmux: worker-Session + shell-Session starten
```

**Key Files:**
- `backend/app/routers/cli_terminal.py` — Provision-Endpoint, Protokoll-Konstante
- `backend/app/services/cli_bridge_runner.py` — dispatch_to_cli_bridge(), _build_cli_prompt()
- `scripts/cli-bridge.py` — Bridge-Prozess (Port 18792), _provision_agent()
- `backend/templates/cli_agent_settings.json.j2` — settings.json Template
- `backend/templates/cli_agent_worker.sh.j2` — worker.sh Template
- `backend/templates/cli_agent.env.j2` — agent.env Template

**Settings-Symlink:**
`~/.mc/agents/{slug}/claude-config/settings.json` → `../settings.json`
Shell-Session und Worker lesen dieselbe Datei → Modellaenderungen in Shell gelten sofort fuer Worker.

**Env-Reihenfolge (Worker + Shell):**
1. `openclaude.local.env` (shared: OPENAI_BASE_URL, Model-Default, CLAUDE_CONFIG_DIR-Global)
2. `agent.env` (pro Agent: MC_AGENT_TOKEN, CLAUDE_CONFIG_DIR-Override, ggf. OPENAI_MODEL)
→ agent.env gewinnt immer bei Konflikten

"""


# ---------------------------------------------------------------------------
# Bekannte Eigenheiten
# ---------------------------------------------------------------------------

def section_gotchas() -> str:
    return """## Bekannte Eigenheiten & Fallstricke

- **node nicht in tmux PATH**: tmux erbt Homebrew-PATH nicht → Launcher-Scripts setzen `/opt/homebrew/bin` explizit
- **Stale Queue-Lock**: Bei hartem Worker-Stop bleibt `queue.lock.d` liegen → `rmdir ~/.mc/agents/{slug}/queue.lock.d`
- **Settings Single Source of Truth**: `claude-config/settings.json` ist ein Symlink auf `../settings.json`. Nie direkt editieren — immer `settings.json` bearbeiten
- **Token Lost Update**: Token NIEMALS via externem Script oder direktem DB-Write setzen → immer `POST /api/v1/agents/{id}/reset-token` API nutzen
- **Provisioning ueberschreibt agent.env**: `/provision` API immer mit echten Werten aufrufen — nie mit Dummy-Daten (ueberschreibt Token!)
- **Boss + 9 Docker-Agents: claude native**: Boss (Host) + Rex/FreeCode/etc (Docker) → `claude` Binary + Anthropic OAuth (Pro/Max Sub). Sparky bleibt `openclaude` + LM Studio/Ollama Cloud.
- **Stale Spinner nach `mc finish`**: openclaude TUI lässt den letzten Spinner-Frame (`✻ Manifesting…`, `✶ Ideating…`, etc.) auf dem Bildschirm stehen, bis ein neues Render-Event kommt (nächster Tool-Call, Tastatureingabe, scroll). Process ist alive — verifiziere via `tmux capture-pane`-Content oder Task-Status im UI, **nicht** über den visuellen Frame. Cosmetic, kein Bug.

"""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(db_agents: list[dict], sessions: set[str]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
    parts = [
        f"# Agent State Map\n\n_Generiert: {now}_\n\n"
        "_Dieses File wird von `python3 tools/generate-agent-map.py` automatisch erstellt. Nicht manuell bearbeiten._\n",
        section_binary_chain(),
        section_shared_env(),
        section_agents(db_agents, sessions),
        section_provisioning(),
        section_gotchas(),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Lese tmux-Sessions...")
    sessions = tmux_sessions()

    print("Lese Agents aus DB...")
    db_agents = fetch_db_agents()
    if not db_agents:
        print("  WARN: DB nicht erreichbar — nur Filesystem-Daten")

    print("Generiere agent-state.md...")
    content = render(db_agents, sessions)

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(content)

    cli_count = len([a for a in db_agents if a["runtime"] == "cli-bridge"])
    print(f"Geschrieben: {OUTPUT}")
    print(f"  {len(db_agents)} Agents total, {cli_count} cli-bridge, {len(sessions)} tmux-Sessions")
