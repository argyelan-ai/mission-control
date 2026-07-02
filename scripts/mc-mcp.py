#!/usr/bin/env python3
"""
Mission Control MCP Server
Gibt Claude direkten Zugriff auf MC API, Bridge und Docker-Logs.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────────

MC_BASE = "http://localhost/api/v1"
BRIDGE_URL = "http://localhost:18792"
PROJECT_DIR = Path(__file__).parent.parent

def _load_env() -> dict:
    env = {}
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

ENV = _load_env()

def _get_token() -> str:
    """JWT-Token via Login generieren."""
    import jose.jwt as jwt
    secret = ENV.get("JWT_SECRET_KEY", "")
    payload = {
        "sub": "mcp-server",
        "role": "admin",
        "exp": int(datetime.now(timezone.utc).timestamp()) + 86400,
    }
    return jwt.encode(payload, secret, algorithm="HS256")

def _headers() -> dict:
    try:
        return {"Authorization": f"Bearer {_get_token()}"}
    except Exception:
        return {}

def _api(method: str, path: str, **kwargs) -> dict:
    try:
        with httpx.Client(timeout=15) as c:
            r = c.request(method, f"{MC_BASE}{path}", headers=_headers(), **kwargs)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"error": str(e)}

def _bridge(method: str, path: str, **kwargs) -> dict:
    try:
        with httpx.Client(timeout=10) as c:
            r = c.request(method, f"{BRIDGE_URL}{path}", **kwargs)
            return r.json()
    except Exception as e:
        return {"error": str(e)}


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP("Mission Control")


@mcp.tool()
def mc_agents(board_id: str = "") -> str:
    """Alle Agents auflisten — optional gefiltert nach board_id."""
    params = {}
    if board_id:
        params["board_id"] = board_id
    result = _api("GET", "/agents", params=params)
    if "error" in result:
        return f"Fehler: {result['error']}"
    agents = result if isinstance(result, list) else result.get("agents", result)
    lines = []
    for a in agents:
        name = a.get("name", "?")
        status = a.get("provision_status", "?")
        runtime = a.get("agent_runtime", "openclaw")
        model = a.get("model", "?")
        lines.append(f"- {name} | {runtime} | {status} | {model} | id={a.get('id', '?')[:8]}")
    return "\n".join(lines) if lines else "Keine Agents gefunden"


def _get_boards() -> list:
    """Alle Boards laden."""
    result = _api("GET", "/boards")
    if "error" in result:
        return []
    return result if isinstance(result, list) else result.get("boards", [])


def _get_tasks_from_boards(board_id: str = "", status: str = "", limit: int = 200) -> list[dict]:
    """Tasks aus einem oder allen Boards laden. Gibt Tasks mit _board_id-Feld zurueck."""
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    if board_id:
        result = _api("GET", f"/boards/{board_id}/tasks", params=params)
        if "error" in result:
            return []
        tasks = result if isinstance(result, list) else result.get("tasks", [])
        for t in tasks:
            t["_board_id"] = board_id
        return tasks
    # Kein board_id → alle Boards abfragen
    all_tasks: list[dict] = []
    for board in _get_boards():
        bid = board.get("id", "")
        if not bid:
            continue
        result = _api("GET", f"/boards/{bid}/tasks", params=params)
        if "error" in result:
            continue
        tasks = result if isinstance(result, list) else result.get("tasks", [])
        for t in tasks:
            t["_board_id"] = bid
        all_tasks.extend(tasks)
    return all_tasks


@mcp.tool()
def mc_tasks(board_id: str = "", status: str = "", agent_name: str = "", limit: int = 20) -> str:
    """Tasks auflisten — filterbar nach board_id, status (inbox/in_progress/review/done/blocked/failed), agent_name."""
    tasks = _get_tasks_from_boards(board_id=board_id, status=status, limit=limit)
    if agent_name:
        tasks = [t for t in tasks if agent_name.lower() in (t.get("assigned_agent_name") or "").lower()]
    lines = []
    for t in tasks:
        title = t.get("title", "?")[:50]
        st = t.get("status", "?")
        agent = t.get("assigned_agent_name") or "—"
        tid = t.get("id", "?")[:8]
        lines.append(f"[{st}] {title} | {agent} | id={tid}")
    return "\n".join(lines) if lines else "Keine Tasks gefunden"


@mcp.tool()
def mc_task_detail(task_id: str, board_id: str = "") -> str:
    """Details eines Tasks inkl. Kommentare — task_id kann auch ein 8-Zeichen Prefix sein."""
    # Vollstaendige UUID + board_id bekannt → direkt
    if len(task_id) == 36 and board_id:
        result = _api("GET", f"/boards/{board_id}/tasks/{task_id}")
        if "error" in result:
            return f"Fehler: {result['error']}"
    else:
        # Prefix oder kein board_id → alle Tasks durchsuchen
        all_tasks = _get_tasks_from_boards(board_id=board_id, limit=200)
        matches = [t for t in all_tasks if t.get("id", "").startswith(task_id)]
        if not matches:
            return f"Task mit Prefix '{task_id}' nicht gefunden"
        task_id = matches[0]["id"]
        board_id = matches[0].get("_board_id", "")
        result = _api("GET", f"/boards/{board_id}/tasks/{task_id}")
        if "error" in result:
            return f"Fehler: {result['error']}"

    lines = [
        f"Titel: {result.get('title', '?')}",
        f"Status: {result.get('status', '?')}",
        f"Agent: {result.get('assigned_agent_name') or '—'}",
        f"Priorität: {result.get('priority', '?')}",
        f"Erstellt: {result.get('created_at', '?')}",
        "",
        f"Beschreibung:\n{result.get('description') or '(keine)'}",
    ]

    comments = result.get("comments", [])
    if comments:
        lines.append(f"\n--- {len(comments)} Kommentar(e) ---")
        for c in comments[-5:]:
            author = c.get("author_name") or c.get("author_agent_name") or "?"
            body = c.get("body", "")[:200]
            lines.append(f"\n[{author}]\n{body}")

    return "\n".join(lines)


@mcp.tool()
def mc_create_task(title: str, description: str = "", board_id: str = "",
                   agent_name: str = "", priority: str = "medium") -> str:
    """Neuen Task erstellen."""
    # Board-ID auflösen falls nicht angegeben
    if not board_id:
        boards = _api("GET", "/boards")
        bl = boards if isinstance(boards, list) else boards.get("boards", [])
        if bl:
            board_id = bl[0]["id"]
        else:
            return "Fehler: Kein Board gefunden"

    payload: dict = {
        "title": title,
        "description": description,
        "board_id": board_id,
        "priority": priority,
    }

    # Agent-ID auflösen falls Name angegeben
    if agent_name:
        agents = _api("GET", "/agents")
        al = agents if isinstance(agents, list) else agents.get("agents", [])
        match = next((a for a in al if a["name"].lower() == agent_name.lower()), None)
        if match:
            payload["assigned_agent_id"] = match["id"]

    result = _api("POST", "/tasks", json=payload)
    if "error" in result:
        return f"Fehler: {result['error']}"
    return f"Task erstellt: {result.get('id', '?')[:8]} — {result.get('title', '?')}"


@mcp.tool()
def mc_patch_task(task_id: str, status: str = "", comment: str = "", board_id: str = "") -> str:
    """Task-Status aktualisieren und/oder Kommentar hinzufügen.

    Args:
        task_id: Task-UUID (full or unique prefix >= 4 chars)
        status: One of inbox | in_progress | review | done | blocked | failed
        comment: Optional comment text. Format: "Update: ...\\nEvidence: ...\\nNext: ..."
        board_id: Optional board UUID. Auto-resolved from task lookup if empty.

    Returns: Human-readable status string with one entry per applied change.
    """
    # Prefix-Auflösung über board-scoped task list (admin endpoint)
    if len(task_id) < 36 or not board_id:
        all_tasks = _get_tasks_from_boards(board_id=board_id, limit=200)
        matches = [t for t in all_tasks if t.get("id", "").startswith(task_id)]
        if not matches:
            return f"Task '{task_id}' nicht gefunden"
        task = matches[0]
        task_id = task["id"]
        if not board_id:
            board_id = task.get("board_id") or task.get("_board_id", "")
    if not board_id:
        return f"Task {task_id} hat keine board_id (corrupt) — kann PATCH nicht ausführen"

    results = []
    if status:
        r = _api("PATCH", f"/boards/{board_id}/tasks/{task_id}", json={"status": status})
        if "error" in r:
            return f"Status-Fehler: {r['error']}"
        results.append(f"Status → {status}")

    if comment:
        # Backend schema CommentCreate uses key `content` (verified 2026-05-01, tasks.py:188)
        r = _api("POST", f"/boards/{board_id}/tasks/{task_id}/comments", json={"content": comment})
        if "error" in r:
            return f"Kommentar-Fehler: {r['error']}"
        results.append("Kommentar hinzugefügt")

    return " | ".join(results) if results else "Nichts geändert"


@mcp.tool()
def mc_bridge_status() -> str:
    """CLI-Bridge Status + Queue-Status aller Agents."""
    status = _bridge("GET", "/health")
    if "error" in status:
        return f"Bridge nicht erreichbar: {status['error']}"

    agents_dir = Path.home() / ".mc" / "agents"
    lines = ["## Bridge Status", f"Erreichbar: ja", ""]

    if agents_dir.exists():
        lines.append("## Agent Queues")
        for agent_dir in sorted(agents_dir.iterdir()):
            if agent_dir.name.startswith("_") or not agent_dir.is_dir():
                continue
            queue = agent_dir / "queue"
            if not queue.exists():
                continue
            pending = len(list((queue / "pending").glob("*.json"))) if (queue / "pending").exists() else 0
            running = len(list((queue / "running").glob("*.json"))) if (queue / "running").exists() else 0
            done = len(list((queue / "done").glob("*.json"))) if (queue / "done").exists() else 0
            failed = len(list((queue / "failed").glob("*.json"))) if (queue / "failed").exists() else 0
            lines.append(f"- {agent_dir.name}: pending={pending} running={running} done={done} failed={failed}")

    return "\n".join(lines)


@mcp.tool()
def mc_restart_worker(agent_slug: str) -> str:
    """Worker (und Shell) Session eines CLI-Agents neu starten."""
    result = _bridge("POST", f"/worker/{agent_slug}/restart")
    if "error" in result:
        return f"Fehler: {result['error']}"
    worker = result.get("worker_started", result.get("ok", "?"))
    shell = result.get("shell_started", "?")
    return f"Restart {agent_slug}: worker={worker} shell={shell}"


@mcp.tool()
def mc_logs(service: str = "backend", lines: int = 30) -> str:
    """Docker Compose Logs abrufen.
    service: backend | frontend | db | redis | caddy
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", service, f"--tail={lines}", "--no-color"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_DIR),
        )
        output = result.stdout or result.stderr
        return output[-4000:] if len(output) > 4000 else output
    except Exception as e:
        return f"Fehler: {e}"


@mcp.tool()
def mc_system_status() -> str:
    """Überblick: Docker Services, Backend Health, Bridge, aktive tmux Sessions."""
    lines = []

    # Docker
    try:
        r = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, timeout=10, cwd=str(PROJECT_DIR),
        )
        services = []
        for line in r.stdout.strip().splitlines():
            try:
                s = json.loads(line)
                services.append(f"  {s.get('Service', '?')}: {s.get('Status', '?')}")
            except Exception:
                pass
        lines.append("## Docker Services")
        lines.extend(services or ["  (keine Info)"])
    except Exception as e:
        lines.append(f"Docker Fehler: {e}")

    # Backend Health
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get("http://localhost/health")
            lines.append(f"\n## Backend: HTTP {r.status_code}")
    except Exception:
        lines.append("\n## Backend: nicht erreichbar")

    # Bridge
    bridge = _bridge("GET", "/health")
    lines.append(f"\n## Bridge (18792): {'OK' if 'error' not in bridge else 'nicht erreichbar'}")

    # tmux Sessions
    try:
        r = subprocess.run(["tmux", "ls"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            lines.append(f"\n## tmux Sessions\n{r.stdout.strip()}")
        else:
            lines.append("\n## tmux: keine Sessions")
    except Exception:
        lines.append("\n## tmux: Fehler")

    return "\n".join(lines)


@mcp.tool()
def mc_get_project(project_id: str) -> str:
    """Projekt-Details + Phasen + Briefing abrufen (fuer Agent-Kontext)."""
    data = _api("GET", f"/projects/{project_id}")
    if "error" in data:
        return f"Fehler: {data['error']}"
    project = data
    phases = project.get("phases", [])
    briefing = project.get("briefing_doc") or "_Kein Briefing vorhanden._"

    lines = [
        f"# Projekt: {project.get('name', 'Unbekannt')}",
        f"Status: {project.get('status', '?')}",
        f"\n## Briefing\n{briefing}",
        f"\n## Phasen ({len(phases)})",
    ]
    for p in phases:
        lines.append(f"- [{p['status']}] {p['title']} (order={p['order']})")
    return "\n".join(lines)


@mcp.tool()
def mc_get_deliverables(task_id: str, board_id: str) -> str:
    """Deliverables eines Tasks abrufen.

    Args:
        task_id: UUID des Tasks
        board_id: UUID des Boards
    """
    data = _api("GET", f"/boards/{board_id}/tasks/{task_id}/deliverables")
    if "error" in data:
        return f"Fehler: {data['error']}"
    items = data if isinstance(data, list) else []
    if not items:
        return "Keine Deliverables fuer diesen Task."
    lines = [f"## Deliverables ({len(items)})"]
    for d in items:
        pinned = " [pinned]" if d.get("is_pinned") else ""
        lines.append(f"- [{d.get('scope', 'task')}]{pinned} {d['title']} ({d['deliverable_type']})")
        if d.get("path"):
            lines.append(f"  Pfad: {d['path']}")
    return "\n".join(lines)


@mcp.tool()
def mc_register_deliverable(
    task_id: str,
    board_id: str,
    title: str,
    deliverable_type: str = "document",
    content: str = "",
    path: str = "",
    description: str = "",
    scope: str = "task",
    tags: str = "",
    is_pinned: bool = False,
    git_commit: bool = False,
) -> str:
    """Deliverable fuer einen Task registrieren.

    Args:
        task_id: UUID des Tasks
        board_id: UUID des Boards
        title: Titel des Deliverables
        deliverable_type: screenshot | file | url | artifact | document | data
        content: Text-Inhalt (fuer Markdown-Deliverables)
        path: Datei-Pfad (fuer file/screenshot/artifact). Akzeptiert Docker-Form
              (/deliverables/<task_id>/foo.pdf) ODER Host-Form
              (~/.mc/deliverables/<task_id>/foo.pdf, /Users/YOUR_USER/.mc/deliverables/...).
        description: Optionale Beschreibung (z.B. wer, wofuer, warum).
        scope: task | phase | project
        tags: kommagetrennte Tags (z.B. 'research,fonts,competitor')
        is_pinned: True = immer in Agent-Kontext injiziert
        git_commit: True = Deliverable als Datei in Git committen
    """
    payload: dict = {
        "title": title,
        "deliverable_type": deliverable_type,
        "scope": scope,
        "is_pinned": is_pinned,
        "git_commit": git_commit,
    }
    if content:
        payload["content"] = content
    if path:
        payload["path"] = path
    if description:
        payload["description"] = description
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

    data = _api("POST", f"/boards/{board_id}/tasks/{task_id}/deliverables", json=payload)
    if "error" in data:
        return f"Fehler: {data['error']}"

    result = f"Deliverable registriert: {title}"
    if data.get("git_commit_hash"):
        result += f" (commit: {data['git_commit_hash']})"
    return result


@mcp.tool()
def mc_complete_phase(project_id: str, phase_id: str) -> str:
    """Phase als abgeschlossen markieren.

    Oeffnet automatisch einen GitHub-PR (phase -> main) und aktiviert
    alle Phasen deren Dependencies jetzt erfuellt sind.

    Args:
        project_id: UUID des Projekts
        phase_id: UUID der Phase
    """
    data = _api("POST", f"/projects/{project_id}/phases/{phase_id}/complete")
    if "error" in data:
        return f"Fehler: {data['error']}"

    lines = [f"Phase abgeschlossen: {phase_id}"]
    if data.get("pr_url"):
        lines.append(f"PR erstellt: {data['pr_url']}")
    activated = data.get("activated_phases", [])
    if activated:
        lines.append(f"Neue aktive Phasen: {len(activated)}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
