#!/usr/bin/env python3
"""
Mission Control MCP Server
Gibt Claude direkten Zugriff auf MC API, Bridge und Docker-Logs.

Two kinds of users, two modes (ADR-085 §4: the admin tool stays an operator
tool):

* Operator mode — no agent identity in the environment (Claude Code sessions
  in the MC folder). Admin helper `_api()` with the operator `.env`, every
  tool is offered. Unchanged behaviour.
* Agent mode — the host runtime passed MC_AGENT_TOKEN or MC_AGENT_ENV_FILE.
  The operator `.env` is never read, no admin JWT is ever minted, every tool
  goes through the agent-scoped routes (`/agent/...`, own board only) and a
  401/403 is reported as is — never retried as the operator. Operator-only
  tools (task creation, phase completion, worker restart, logs, system
  status) are not registered at all, so they are absent from the tool list.
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


def _agent_mode_from_env() -> bool:
    return bool(
        os.environ.get("MC_AGENT_TOKEN", "").strip()
        or os.environ.get("MC_AGENT_ENV_FILE", "").strip()
    )


# Decided once at start: controls which tools are registered. A process that
# started as an agent stays an agent (no later switch to the admin path).
AGENT_MODE = _agent_mode_from_env()


def _is_agent() -> bool:
    return AGENT_MODE or _agent_mode_from_env()


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

# Agent mode never reads the operator `.env` (it holds the admin secret).
ENV: dict = {} if AGENT_MODE else _load_env()

def _get_token() -> str:
    """JWT-Token via Login generieren (operator mode only)."""
    if _is_agent():
        raise RuntimeError("no admin token in agent mode")
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

_ADMIN_BLOCKED = (
    "Operator-Weg im Agenten-Modus gesperrt — dieser Server handelt fuer "
    "Agenten nur ueber die Agenten-Wege (kein Admin-Zugriff)."
)


def _api(method: str, path: str, **kwargs) -> dict:
    """Admin call as the operator. Refuses outright in agent mode."""
    if _is_agent():
        return {"error": _ADMIN_BLOCKED}
    try:
        with httpx.Client(timeout=15) as c:
            r = c.request(method, f"{MC_BASE}{path}", headers=_headers(), **kwargs)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"error": str(e)}

# Agent PBKDF2 token, passed through from agent.env by the host runtime. Used so
# that task actions performed by the agent are attributed to the AGENT
# (author_type='agent') instead of the admin user shown as '👤 Du' — which also
# stops the self-notification echo loop (own comments are filtered out on poll).
#
# Resolved at CALL time, never snapshotted at import (live incident 2026-07-12:
# the long-lived hermes gateway spawned this server with a stale/poisoned token
# in its process env — every agent-scoped call failed for days while the admin
# JWT path kept working). Fallback: the agent.env file pointed to by
# MC_AGENT_ENV_FILE (set e.g. in the hermes gateway plist), which also picks up
# token rotations without a process restart.

def _agent_token() -> str:
    token = os.environ.get("MC_AGENT_TOKEN", "").strip()
    if token:
        return token
    env_file = os.environ.get("MC_AGENT_ENV_FILE", "").strip()
    if env_file:
        try:
            with open(env_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("MC_AGENT_TOKEN="):
                        value = line.partition("=")[2].strip()
                        # agent.env values may carry one layer of shell quoting.
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                            value = value[1:-1]
                        return value.strip()
        except OSError:
            pass
    return ""

def _api_agent(method: str, path: str, extra_headers: dict | None = None, **kwargs):
    """Call an agent-scoped endpoint AS the agent. Returns None when no agent
    token is available so callers can fall back to the admin _api()."""
    token = _agent_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
    try:
        with httpx.Client(timeout=15) as c:
            r = c.request(method, f"{MC_BASE}{path}", headers=headers, **kwargs)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"error": str(e)}

def _bridge_headers() -> dict:
    """X-MC-Bridge-Token for the cli-bridge helper — operator mode only.

    Same source as the helper (scripts/cli-bridge.py): CLI_BRIDGE_TOKEN, else
    the 0600 file CLI_BRIDGE_TOKEN_FILE / ~/.mc/secrets/cli-bridge.token.
    Agent mode never reads it (an agent gets no operator power over the
    host helper); its only bridge call is the unauthenticated /ping.
    """
    if _is_agent():
        return {}
    token = os.environ.get("CLI_BRIDGE_TOKEN", "").strip()
    if not token:
        path = Path(
            os.environ.get("CLI_BRIDGE_TOKEN_FILE", "").strip()
            or (Path.home() / ".mc" / "secrets" / "cli-bridge.token")
        )
        try:
            token = path.read_text().strip()
        except OSError:
            token = ""
    return {"X-MC-Bridge-Token": token} if token else {}


def _bridge(method: str, path: str, **kwargs) -> dict:
    try:
        with httpx.Client(timeout=10, headers=_bridge_headers()) as c:
            r = c.request(method, f"{BRIDGE_URL}{path}", **kwargs)
            return r.json()
    except Exception as e:
        return {"error": str(e)}


_NO_AGENT_TOKEN = (
    "kein Agent-Token verfuegbar (MC_AGENT_TOKEN / MC_AGENT_ENV_FILE pruefen) — "
    "Aufruf NICHT als Operator wiederholt"
)


def _agent_call(method: str, path: str, **kwargs):
    """Agent-scoped call for agent mode: a missing token is an error, never a
    reason to fall back to the admin path."""
    r = _api_agent(method, path, **kwargs)
    if r is None:
        return {"error": _NO_AGENT_TOKEN}
    return r


def _is_error(r) -> bool:
    return isinstance(r, dict) and "error" in r


def _agent_board(board_id: str = "") -> tuple[str, str]:
    """The agent's board: the given one, else its own (GET /agent/me).
    Returns (board_id, error)."""
    if board_id:
        return board_id, ""
    me = _agent_call("GET", "/agent/me")
    if _is_error(me):
        return "", f"Fehler: {me['error']}"
    bid = (me or {}).get("board_id") if isinstance(me, dict) else None
    if not bid:
        return "", "Fehler: Agent ist keinem Board zugeordnet"
    return bid, ""


def _agent_tasks(board_id: str = "", status: str = "", limit: int = 200) -> tuple[list[dict], str]:
    """Tasks of the agent's own board (newest first, max 200).
    Returns (tasks, error)."""
    bid, err = _agent_board(board_id)
    if err:
        return [], err
    params: dict = {"limit": max(1, min(int(limit or 200), 200))}
    if status:
        params["status"] = status
    r = _agent_call("GET", f"/agent/boards/{bid}/tasks", params=params)
    if _is_error(r):
        return [], f"Fehler: {r['error']}"
    tasks = r if isinstance(r, list) else (r or {}).get("tasks", [])
    for t in tasks:
        t["_board_id"] = bid
    return tasks, ""


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP("Mission Control")


def _operator_tool(fn):
    """Register a tool only in operator mode. In agent mode the function
    stays importable but is not offered in the MCP tool list."""
    if AGENT_MODE:
        return fn
    return mcp.tool()(fn)


@mcp.tool()
def mc_agents(board_id: str = "") -> str:
    """Alle Agents auflisten — optional gefiltert nach board_id.
    Als Agent: nur die Agents des eigenen Boards."""
    if _is_agent():
        bid, err = _agent_board(board_id)
        if err:
            return err
        r = _agent_call("GET", f"/agent/boards/{bid}/agents")
        if _is_error(r):
            return f"Fehler: {r['error']}"
        lines = [
            f"- {a.get('name', '?')} | {a.get('role') or '?'} | "
            f"{a.get('provision_status', '?')} | id={str(a.get('id', '?'))[:8]}"
            for a in (r or [])
        ]
        return "\n".join(lines) if lines else "Keine Agents gefunden"
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
    """Tasks auflisten — filterbar nach board_id, status (inbox/in_progress/review/done/blocked/failed), agent_name.
    Als Agent: nur das eigene Board."""
    if _is_agent():
        tasks, err = _agent_tasks(board_id=board_id, status=status, limit=limit)
        if err:
            return err
    else:
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
    if _is_agent():
        task_id, board_id, err = _resolve_task_board(task_id, board_id)
        if err:
            return err
        result = _agent_call("GET", f"/agent/boards/{board_id}/tasks/{task_id}/detail")
        if _is_error(result):
            return f"Fehler: {result['error']}"
        comments = _agent_call("GET", f"/agent/boards/{board_id}/tasks/{task_id}/comments")
        result["comments"] = comments if isinstance(comments, list) else []
    # Vollstaendige UUID + board_id bekannt → direkt
    elif len(task_id) == 36 and board_id:
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
            author = (c.get("author_name") or c.get("author_agent_name")
                      or c.get("author_type") or "?")
            body = (c.get("content") or c.get("body") or "")[:200]
            lines.append(f"\n[{author}]\n{body}")

    return "\n".join(lines)


@_operator_tool
def mc_create_task(title: str, description: str = "", board_id: str = "",
                   agent_name: str = "", priority: str = "medium") -> str:
    """Neuen Task erstellen (nur Operator — Agenten bekommen dieses Werkzeug nicht)."""
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
        "priority": priority,
    }

    # Agent-ID auflösen falls Name angegeben
    if agent_name:
        agents = _api("GET", "/agents")
        al = agents if isinstance(agents, list) else agents.get("agents", [])
        match = next((a for a in al if a["name"].lower() == agent_name.lower()), None)
        if match:
            payload["assigned_agent_id"] = match["id"]

    # The operator create route is board-scoped (routers/tasks.py); the old
    # `POST /tasks` never existed and answered 404.
    result = _api("POST", f"/boards/{board_id}/tasks", json=payload)
    if "error" in result:
        return f"Fehler: {result['error']}"
    return f"Task erstellt: {result.get('id', '?')[:8]} — {result.get('title', '?')}"


@mcp.tool()
def mc_patch_task(task_id: str, status: str = "", comment: str = "", board_id: str = "",
                  attempt_id: str = "", comment_type: str = "message") -> str:
    """Task-Status aktualisieren und/oder Kommentar hinzufügen.

    Args:
        task_id: Task-UUID (full or unique prefix >= 4 chars)
        status: One of inbox | in_progress | review | done | blocked | failed
        comment: Optional comment text. Format: "Update: ...\\nEvidence: ...\\nNext: ..."
        board_id: Optional board UUID. Auto-resolved from task lookup if empty.
        attempt_id: Dispatch attempt id from the [MC DISPATCH] header. Sent as
            X-Dispatch-Attempt-Id — status changes on a dispatched task are
            rejected (409) without it.
        comment_type: Kommentar-Typ, Standard "message". Fuer den Wechsel auf
            review/done verlangt MC eine Reflexion: comment_type="reflection"
            — sie wird dann VOR dem Statuswechsel gepostet, damit das
            Reflexions-Tor sie schon sieht.

    Returns: Human-readable status string with one entry per applied change.
    """
    task_id, board_id, err = _resolve_task_board(task_id, board_id)
    if err:
        return err

    results = []

    def _post_comment() -> str:
        # Post AS the agent (author_type='agent') via the agent-scoped endpoint.
        # NEVER fall back to the operator endpoint here: that records
        # author_type='user' → the comment shows up as '👤 Du' (the logged-in
        # operator) and echoes back to the agent as a "new user comment". A
        # missing/invalid MC_AGENT_TOKEN is a provisioning fault to surface,
        # not to paper over by impersonating the operator.
        r = _api_agent("POST", f"/agent/boards/{board_id}/tasks/{task_id}/comments",
                       json={"content": comment, "comment_type": comment_type or "message"})
        if r is None:
            return ("Kommentar-Fehler: kein MC_AGENT_TOKEN in der Umgebung — "
                    "Kommentar NICHT gepostet (würde sonst als Operator '👤 Du' "
                    "erscheinen). agent.env prüfen / Bridge neu starten.")
        if _is_error(r):
            return f"Kommentar-Fehler (Agent-Endpoint): {r['error']}"
        results.append("Kommentar hinzugefügt")
        return ""

    # A reflection is what the gate before review/done looks for — post it
    # first so the status change in the same call can pass.
    comment_first = bool(comment) and comment_type == "reflection"
    if comment_first:
        err = _post_comment()
        if err:
            return err

    if status:
        # Change status AS the agent via the agent-scoped state-machine endpoint
        # (ACK handshake, review handoff, parent reactivation) — the same path
        # Docker agents use. Falls back to the admin endpoint ONLY in operator
        # mode (no agent identity). With an agent identity, a rejected agent
        # call (e.g. the 409 dispatch-attempt guard) is surfaced, never
        # retried as the operator: that bypassed the guard and recorded the
        # change without the agent (changed_by='user'), so a later "request
        # changes" could not find the builder and went to the Board Lead.
        extra = {"X-Dispatch-Attempt-Id": attempt_id} if attempt_id else None
        r = _api_agent("PATCH", f"/agent/boards/{board_id}/tasks/{task_id}",
                       extra_headers=extra, json={"status": status})
        if r is None:
            if _is_agent():
                return f"Status-Fehler: {_NO_AGENT_TOKEN}"
            r = _api("PATCH", f"/boards/{board_id}/tasks/{task_id}", json={"status": status})
        if _is_error(r):
            return f"Status-Fehler: {r['error']}" + (
                f" (vorher: {', '.join(results)})" if results else "")
        results.append(f"Status → {status}")

    if comment and not comment_first:
        err = _post_comment()
        if err:
            return err

    return " | ".join(results) if results else "Nichts geändert"


def _resolve_task_board(task_id: str, board_id: str) -> tuple[str, str, str]:
    """Resolve a (possibly prefix) task_id + optional board_id to full IDs.

    Returns (task_id, board_id, error); error is '' on success. Used by every
    tool that takes a task id, so all accept the same short prefixes.
    Agent mode: only the agent's own board (newest 200 cards) is searched.
    """
    if _is_agent():
        board_id, err = _agent_board(board_id)
        if err:
            return "", "", err
        if len(task_id) < 36:
            tasks, err = _agent_tasks(board_id=board_id, limit=200)
            if err:
                return "", "", err
            matches = [t for t in tasks if t.get("id", "").startswith(task_id)]
            if not matches:
                return "", "", (f"Task '{task_id}' nicht gefunden "
                                "(gesucht: neueste 200 Karten des eigenen Boards)")
            task_id = matches[0]["id"]
        return task_id, board_id, ""
    if len(task_id) < 36 or not board_id:
        all_tasks = _get_tasks_from_boards(board_id=board_id, limit=200)
        matches = [t for t in all_tasks if t.get("id", "").startswith(task_id)]
        if not matches:
            return "", "", f"Task '{task_id}' nicht gefunden"
        task = matches[0]
        task_id = task["id"]
        if not board_id:
            board_id = task.get("board_id") or task.get("_board_id", "")
    if not board_id:
        return "", "", f"Task {task_id} hat keine board_id (corrupt)"
    return task_id, board_id, ""


def _fmt_checklist(items) -> str:
    rows = items if isinstance(items, list) else (items.get("items", []) if isinstance(items, dict) else [])
    if not rows:
        return "Keine Checklist-Items"
    return "\n".join(
        f"- [{it.get('status', 'pending')}] {it.get('title', '?')} (id={str(it.get('id', '?'))[:8]})"
        for it in rows
    )


@mcp.tool()
def mc_checklist_add(task_id: str, items: list[str], board_id: str = "") -> str:
    """Checklist-/Todo-Items für einen Task anlegen (Bulk).

    Die Checkliste ist die Single Source of Truth für den Fortschritt und wird
    im Task-Detail-Panel angezeigt. Lege sie zu Beginn des Tasks an (ein Item
    pro Schritt) und hake Items mit mc_checklist_done ab.

    Args:
        task_id: Task-UUID (voll oder eindeutiger Prefix >= 4 Zeichen)
        items: Liste der Item-Titel, z.B. ["Recherche", "Implementierung", "Test"]
        board_id: Optional; wird aus dem Task aufgelöst wenn leer.

    Returns: Die angelegten Items mit ihren IDs (für mc_checklist_done).
    """
    task_id, board_id, err = _resolve_task_board(task_id, board_id)
    if err:
        return err
    payload = {"items": [{"title": t, "sort_order": i} for i, t in enumerate(items) if t and t.strip()]}
    if not payload["items"]:
        return "Keine Items angegeben"
    r = _api_agent("POST", f"/agent/boards/{board_id}/tasks/{task_id}/checklist", json=payload)
    if r is None:
        return "Kein Agent-Token verfügbar — Checklist-Items brauchen Agent-Auth (MC_AGENT_TOKEN)"
    if isinstance(r, dict) and "error" in r:
        return f"Checklist-Fehler: {r['error']}"
    return "Checkliste angelegt:\n" + _fmt_checklist(r)


@mcp.tool()
def mc_checklist(task_id: str, board_id: str = "") -> str:
    """Die Checkliste eines Tasks lesen (Item-Titel, Status, IDs)."""
    task_id, board_id, err = _resolve_task_board(task_id, board_id)
    if err:
        return err
    r = _api_agent("GET", f"/agent/boards/{board_id}/tasks/{task_id}/checklist")
    if r is None:
        # Operator mode only — in agent mode _api() refuses on its own.
        r = _api("GET", f"/boards/{board_id}/tasks/{task_id}/checklist")
    if isinstance(r, dict) and "error" in r:
        return f"Fehler: {r['error']}"
    return _fmt_checklist(r)


@mcp.tool()
def mc_checklist_done(task_id: str, item_id: str, status: str = "done", board_id: str = "") -> str:
    """Status eines Checklist-Items setzen (Standard: done).

    Args:
        task_id: Task-UUID (voll oder Prefix)
        item_id: Item-UUID (voll oder Prefix — aus mc_checklist / mc_checklist_add)
        status: pending | in_progress | done | blocked | skipped
        board_id: Optional; aus dem Task aufgelöst wenn leer.
    """
    task_id, board_id, err = _resolve_task_board(task_id, board_id)
    if err:
        return err
    # Resolve a short item-id prefix against the live checklist.
    if len(item_id) < 36:
        lst = _api_agent("GET", f"/agent/boards/{board_id}/tasks/{task_id}/checklist")
        rows = lst if isinstance(lst, list) else (lst.get("items", []) if isinstance(lst, dict) else [])
        matches = [it for it in rows if str(it.get("id", "")).startswith(item_id)]
        if not matches:
            return f"Checklist-Item '{item_id}' nicht gefunden"
        item_id = matches[0]["id"]
    r = _api_agent("PATCH", f"/agent/boards/{board_id}/tasks/{task_id}/checklist/{item_id}",
                   json={"status": status})
    if r is None:
        return "Kein Agent-Token verfügbar — Checklist braucht Agent-Auth (MC_AGENT_TOKEN)"
    if isinstance(r, dict) and "error" in r:
        return f"Checklist-Fehler: {r['error']}"
    return f"Item {str(item_id)[:8]} → {status}"


@mcp.tool()
def mc_bridge_status() -> str:
    """CLI-Bridge Status + Queue-Status aller Agents."""
    status = _bridge("GET", "/ping")  # liveness only, no token needed
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


@_operator_tool
def mc_restart_worker(agent_slug: str) -> str:
    """Worker (und Shell) Session eines CLI-Agents neu starten."""
    result = _bridge("POST", f"/worker/{agent_slug}/restart")
    if "error" in result:
        return f"Fehler: {result['error']}"
    worker = result.get("worker_started", result.get("ok", "?"))
    shell = result.get("shell_started", "?")
    return f"Restart {agent_slug}: worker={worker} shell={shell}"


@_operator_tool
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


@_operator_tool
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
    bridge = _bridge("GET", "/ping")
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
    """Projekt-Details + Phasen + Briefing abrufen (fuer Agent-Kontext).
    Als Agent: nur Projekte des eigenen Boards (Recht project:read)."""
    if _is_agent():
        data = _agent_call("GET", f"/agent/projects/{project_id}")
    else:
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
def mc_get_deliverables(task_id: str, board_id: str = "") -> str:
    """Deliverables eines Tasks abrufen.

    Args:
        task_id: UUID des Tasks (voll oder Prefix)
        board_id: UUID des Boards (optional, wird aus dem Task aufgeloest)
    """
    task_id, board_id, err = _resolve_task_board(task_id, board_id)
    if err:
        return err
    if _is_agent():
        data = _agent_call("GET", f"/agent/boards/{board_id}/tasks/{task_id}/deliverables")
    else:
        data = _api("GET", f"/boards/{board_id}/tasks/{task_id}/deliverables")
    if _is_error(data):
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
    board_id: str = "",
    title: str = "",
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
        task_id: UUID des Tasks (voll oder Prefix)
        board_id: UUID des Boards (optional, wird aus dem Task aufgeloest)
        title: Titel des Deliverables (Pflicht)
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

    if not title.strip():
        return "Fehler: title fehlt"
    task_id, board_id, err = _resolve_task_board(task_id, board_id)
    if err:
        return err
    if _is_agent():
        # Agent route: records the agent as author (duplicate guard, board
        # memory, git commit, vault folder) — the admin route records none.
        data = _agent_call("POST", f"/agent/boards/{board_id}/tasks/{task_id}/deliverables",
                           json=payload)
    else:
        data = _api("POST", f"/boards/{board_id}/tasks/{task_id}/deliverables", json=payload)
    if _is_error(data):
        return f"Fehler: {data['error']}"

    result = f"Deliverable registriert: {title}"
    if data.get("git_commit_hash"):
        result += f" (commit: {data['git_commit_hash']})"
    return result


@mcp.tool()
def mc_inbox() -> str:
    """Neue Thread-Nachrichten abholen und bestaetigen (Nudge+Pull, W2.1).

    Wenn ein 📬-Weckruf ("Neue Nachrichten … lies sie jetzt") erscheint,
    dieses Tool aufrufen: es liefert alle ungelesenen Nachrichten aus den
    Task-Threads des Agenten und ackt sie danach server-seitig — erst der
    Ack konsumiert die Nachricht (at-least-once, wie `mc inbox` im Shell-CLI).
    Danach IMMER mit mc_msg in den Thread aus dem [thread ... · seq ...]-Footer
    antworten — sonst bleibt die Nachricht eine Einbahnstrasse.
    """
    resp = _api_agent("GET", "/agent/me/inbox")
    if resp is None:
        return "Fehler: kein Agent-Token verfuegbar (MC_AGENT_TOKEN fehlt)."
    if "error" in resp:
        return f"Fehler: {resp['error']}"

    messages = resp.get("messages") or []
    threads = resp.get("threads") or {}
    if not messages:
        return "Keine neuen Nachrichten."

    blocks = []
    for m in messages:
        footer = (
            f"[thread {m.get('thread_id')} · seq {m.get('seq')} · "
            f"von {m.get('sender', '?')} · typ {m.get('message_type', '?')}]"
        )
        blocks.append(
            f"# Neue Nachricht (Interaction 2.0)\n\n{m.get('body') or ''}\n\n{footer}"
        )

    # Ack pro Thread das hoechste gelieferte seq (Pull = Zustellung).
    # Fehlschlag bei einem Thread blockiert die uebrigen nicht — idempotent,
    # der naechste Abruf liefert Nicht-geacktes ohnehin erneut.
    ack_errors = []
    for tid, max_seq in threads.items():
        r = _api_agent(
            "POST", "/agent/me/inbox/ack",
            json={"thread_id": tid, "seq": max_seq},
        )
        if r is None or "error" in (r or {}):
            ack_errors.append(f"ack fuer thread {tid} fehlgeschlagen: {(r or {}).get('error', 'kein Token')}")

    result = "\n\n---\n\n".join(blocks)
    if ack_errors:
        result += "\n\n" + "\n".join(ack_errors)
    return result


@mcp.tool()
def mc_msg(body: str, thread_id: str = "", message_type: str = "message") -> str:
    """Nachricht in einen Chat-Thread posten — Rueckkanal fuer MCP-only-Agenten.

    Das Gegenstueck zu mc_inbox: nach dem Abholen neuer Nachrichten IMMER in
    den Thread aus dem [thread ... · seq ...]-Footer antworten. Ohne mc_msg
    bleibt jede Chat-Nachricht eine Einbahnstrasse — gelesen, geackt,
    verstummt (live gemessen 2026-07-29, siehe thread_scope).

    Mit `thread_id` → POST /agent/threads/{thread_id}/messages (gezielte
    Konversation, z.B. die Thread-ID aus dem mc_inbox-Footer).
    Ohne `thread_id` → POST /agent/tasks/current/messages (Thread des aktiven
    Tasks; ohne aktiven Task fallback auf den DM-Thread des Agenten).

    Erlaubte Typen: message (Default), status, decision. `question` wird
    abgelehnt — Fragen tragen Awaiting-Semantik und laufen ueber einen anderen
    Weg (`mc ask` im Shell-CLI); beide Endpoints lehnen message_type
    "question" ab.

    Args:
        body: Nachrichtentext
        thread_id: Thread-ID (aus dem mc_inbox-Footer). Leer = aktueller Task.
        message_type: message | status | decision
    """
    if message_type == "question":
        return (
            "Fehler: message_type='question' wird abgelehnt — Fragen tragen "
            "Awaiting-Semantik und laufen ueber einen anderen Weg (mc ask). "
            "Nutze message | status | decision."
        )
    if message_type not in ("message", "status", "decision"):
        return (
            f"Fehler: ungueltiger message_type '{message_type}' — erlaubt sind "
            "message, status, decision."
        )

    path = (
        f"/agent/threads/{thread_id}/messages"
        if thread_id
        else "/agent/tasks/current/messages"
    )
    resp = _api_agent("POST", path, json={"body": body, "message_type": message_type})
    if resp is None:
        return "Fehler: kein Agent-Token verfuegbar (MC_AGENT_TOKEN fehlt)."
    if "error" in resp:
        return f"Fehler: {resp['error']}"

    message_id = resp.get("message_id") or resp.get("id") or "?"
    return (
        f"Nachricht gesendet: {message_id} "
        f"(thread={resp.get('thread_id', '?')}, typ={message_type})"
    )


@mcp.tool()
def mc_box_status(box: str = "") -> str:
    """Box facts, read-only (ADR-100): is a GPU box free?

    Per box a verdict — free / busy / not_serving / unknown — with reasons,
    whether MC restarts a stopped engine there (auto-recovery), a working
    head, and per engine: model, engine state, running requests, switch lock.
    `not_serving` is never "free": during a hand test the engine is stopped
    by hand. Always the agent route with the agent's own token, never the
    operator path (same as `mc box status`).

    Args:
        box: only this box (host slug); empty = all boxes
    """
    resp = _agent_call("GET", "/agent/boxes/status")
    if _is_error(resp):
        return f"Fehler: {resp['error']}"
    boxes = (resp or {}).get("boxes") or []
    if box:
        boxes = [b for b in boxes if b.get("slug") == box]
        if not boxes:
            return f"Keine Box '{box}' (ohne Argument: alle Boxen)."
    if not boxes:
        return "Keine GPU-Box mit Engine in MC registriert."
    blocks = []
    for b in boxes:
        recovery = "auto-recovery on" if b.get("auto_recovery") else "auto-recovery off"
        lines = [
            f"{b.get('slug')} ({b.get('display_name')}): {b.get('verdict')} — "
            f"{', '.join(b.get('reasons') or []) or '-'} · {recovery}"
        ]
        head = b.get("head")
        if head:
            lines.append(f"  head working: run {head.get('run_id')} · {head.get('harness')}")
        for rt in b.get("runtimes") or []:
            running = rt.get("running_requests")
            lines.append(
                f"  - {rt.get('slug')} [{rt.get('role')}] model {rt.get('model') or '-'} · "
                f"engine {rt.get('engine')} · running {'?' if running is None else running} · "
                f"switch lock {rt.get('switch_lock')}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@_operator_tool
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
