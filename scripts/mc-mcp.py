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

Worker flow (ADR-101): an agent-mode process started with MC_MCP_WORKER=1 also
gets the tools to work cards (mc_next_task, mc_progress, mc_finish,
mc_handbook) and the server instructions that explain the loop.
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────────

# MC_API_URL is the variable the `mc` CLI uses (base without /api/v1): a worker
# on another machine points both at the same MC. Default: this machine.
MC_BASE = (
    os.environ.get("MC_API_URL", "").strip().rstrip("/").removesuffix("/api/v1")
    or "http://localhost"
) + "/api/v1"
BRIDGE_URL = "http://localhost:18792"
PROJECT_DIR = Path(__file__).parent.parent


def _agent_mode_from_env() -> bool:
    return bool(
        os.environ.get("MC_AGENT_TOKEN", "").strip()
        or os.environ.get("MC_AGENT_ENV_FILE", "").strip()
        or _session_file()
    )


def _session_file() -> str:
    """ADR-100: a terminal session enrolled with a guest card (`mc hello`)
    acts with its own session token from this file — agent mode, never the
    operator path."""
    return os.environ.get("MC_COMMS_SESSION_FILE", "").strip()


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
    session_file = _session_file()
    if session_file:
        try:
            with open(session_file, encoding="utf-8") as fh:
                return str(json.load(fh).get("token") or "").strip()
        except (OSError, ValueError, AttributeError):
            return ""
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
    if _session_file():
        return _session_inbox()
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


def _session_inbox() -> str:
    """mc_inbox for a terminal session (ADR-100): its own inbox, then ack."""
    resp = _agent_call("GET", "/agent/comms/inbox")
    if _is_error(resp):
        return f"Fehler: {resp['error']}"
    messages = (resp or {}).get("messages") or []
    if not messages:
        return "Keine neuen Nachrichten."
    blocks = [
        f"{m.get('body') or ''}\n\n[thread {m.get('thread_id')} · seq {m.get('seq')} · von {m.get('from')}"
        f" · antworten: mc_send(to=\"{m.get('from')}\")]"
        for m in messages
    ]
    for tid, max_seq in ((resp or {}).get("threads") or {}).items():
        _agent_call("POST", "/agent/comms/inbox/ack", json={"thread_id": tid, "seq": max_seq})
    return "\n\n---\n\n".join(blocks)


@mcp.tool()
def mc_who() -> str:
    """Who is reachable across harnesses (ADR-100) — "alive" and "listening"
    are shown apart: a paused agent can be alive but not listening."""
    resp = _agent_call("GET", "/agent/comms/who")
    if _is_error(resp):
        return f"Fehler: {resp['error']}"
    rows = (resp or {}).get("sessions") or []
    if not rows:
        return "Niemand darf gerade sprechen."
    def _yn(v):
        return "unknown" if v is None else ("yes" if v else "no")
    lines = []
    if (resp or {}).get("warming_up"):
        lines.append("(MC ist frisch gestartet — 'unknown' heisst: noch nicht gemeldet)")
    for r in rows:
        line = (f"{r.get('address')} · {r.get('kind')} · {r.get('harness') or '-'} · "
                f"alive {r.get('alive')} · listening {_yn(r.get('listening'))}")
        if r.get("busy"):
            line += " · busy"
        if r.get("task"):
            line += f" · {r['task']}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def mc_send(to: str, body: str) -> str:
    """Send a message to ONE named session (address from mc_who) — ADR-100.

    Information only, never a permission: the receiver sees it framed as
    foreign text, and anything risky still needs the operator's approval
    card. MC refuses broadcasts, a receiver that does not listen, more than
    10 per minute, duplicates, and pauses a conversation after 6 messages
    without the operator.

    Args:
        to: address (e.g. "claude@desk:runtimes") or a unique short name
        body: the message text
    """
    resp = _agent_call("POST", "/agent/comms/send", json={"to": to, "body": body})
    if _is_error(resp):
        return f"Fehler: {resp['error']}"
    line = f"gesendet an {resp.get('to')} · {resp.get('delivery')}"
    if resp.get("paused"):
        line += " · Gespraech pausiert, wartet auf die Freigabe des Operators"
    return line


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


# ── Worker flow (ADR-101) ─────────────────────────────────────────────────────
#
# A registered agent without a bridge of its own (own token, own scopes) works
# its cards through these tools. They are a thin layer over the agent routes the
# `mc` command uses; finishing runs the command's own `mc finish`, so the
# pre-finish checks exist exactly once.

_TRUTHY = {"1", "true", "yes", "on"}


def _worker_mode_from_env() -> bool:
    """Opt-in: MC_MCP_WORKER=1 next to an agent token. A harness with its own
    bridge (it polls and sends heartbeats itself) and a guest session (ADR-100,
    scope comms:talk only) never get the worker tools."""
    return (
        AGENT_MODE
        and not _session_file()
        and os.environ.get("MC_MCP_WORKER", "").strip().lower() in _TRUTHY
    )


# Decided once at start, like AGENT_MODE.
WORKER_MODE = _worker_mode_from_env()
# ADR-100 §2: a guest session gains neither worker nor vault tools.
GUEST_MODE = bool(_session_file())


def _worker_tool(fn):
    """Register a tool only for a worker (MC_MCP_WORKER=1 in agent mode)."""
    return mcp.tool()(fn) if WORKER_MODE else fn


def _vault_tool(agent_only: bool = False):
    """Register a vault tool: never for a guest session; `agent_only` tools
    (writing) not for the operator either."""
    def register(fn):
        if GUEST_MODE or (agent_only and not AGENT_MODE):
            return fn
        return mcp.tool()(fn)
    return register


_NO_CARD = "no active card. Call mc_next_task first."
_NOTE_CAP = 20000
_CLI_DIR = Path(__file__).parent / "mc-cli"

# The card this process works on: task_id, board_id, attempt_id, title, status.
_card_lock = threading.RLock()
_card: dict = {}
# Highest message seq shown per thread — sent as acked_seq on the next poll.
_shown_seq: dict[str, int] = {}
# (state, task_id) of run-end answers already reported; MC repeats them.
_reported_ends: set[tuple[str, str]] = set()
_last_call = time.monotonic()
_hb_thread: threading.Thread | None = None
_hb_stop = threading.Event()
_beat_lock = threading.Lock()
# mc_finish redirects stdout/stderr and may change the folder: one at a time.
_finish_lock = threading.Lock()


def _held_card() -> dict | None:
    with _card_lock:
        return dict(_card) if _card.get("task_id") else None


def _remember_card(card: dict) -> None:
    with _card_lock:
        _card.clear()
        _card.update(card)


def _forget_card() -> None:
    with _card_lock:
        _card.clear()


def _label(card: dict) -> str:
    short = str(card.get("task_id") or "")[:8]
    title = str(card.get("title") or "").strip()
    return f"{title} ({short})" if title else short


def _touch() -> None:
    global _last_call
    _last_call = time.monotonic()


def _en(err) -> str:
    """Error text of a shared helper without its German 'Fehler: ' prefix."""
    return str(err).removeprefix("Fehler: ")


def _attempt_header(attempt_id: str) -> dict:
    return {"X-Dispatch-Attempt-Id": attempt_id} if attempt_id else {}


# ── Heartbeat ─────────────────────────────────────────────────────────────────
#
# The beat tells MC that this process is alive and was used recently. It does
# NOT prove that the model behind it is producing work — hence the quiet window.

def _env_number(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _heartbeat_interval() -> float:
    """Seconds between beats; 0 switches the beat off. Capped at 60 so a beat
    is always younger than the 90 s the turn signal accepts."""
    return min(_env_number("MC_MCP_HEARTBEAT_SECONDS", 30), 60)


def _quiet() -> bool:
    """True when no MC tool was called for longer than the quiet window."""
    minutes = _env_number("MC_MCP_HEARTBEAT_QUIET_MINUTES", 20)
    return minutes > 0 and time.monotonic() - _last_call > minutes * 60


def _beat() -> None:
    # The card check and the send happen under one lock, and the idle beat
    # takes the same lock: a `working` beat can never land after `idle`.
    with _beat_lock:
        card = _held_card()
        if not card:
            return
        body = {"status": "working", "task_id": card["task_id"]}
        if card.get("attempt_id"):
            body["attempt_id"] = card["attempt_id"]
        _api_agent("POST", "/agent/me/heartbeat", json=body)


def _heartbeat_tick() -> None:
    if _heartbeat_interval() > 0 and _held_card() and not _quiet():
        _beat()


def _heartbeat_loop(stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        _heartbeat_tick()


def _start_heartbeat() -> None:
    global _hb_thread, _hb_stop
    interval = _heartbeat_interval()
    if interval <= 0:
        return
    _beat()
    with _card_lock:
        if _hb_thread is not None and _hb_thread.is_alive():
            return
        _hb_stop = threading.Event()
        _hb_thread = threading.Thread(
            target=_heartbeat_loop, args=(_hb_stop, interval),
            name="mc-mcp-heartbeat", daemon=True,
        )
        _hb_thread.start()


def _stop_heartbeat(send_idle: bool = True) -> None:
    """Stop the loop and send one `idle` beat. Callers drop the card first, so
    a beat that starts after this point finds no card and sends nothing."""
    global _hb_thread
    with _card_lock:
        thread = _hb_thread
        _hb_stop.set()
        _hb_thread = None
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=15)
    if send_idle and thread is not None and _heartbeat_interval() > 0:
        with _beat_lock:
            _api_agent("POST", "/agent/me/heartbeat", json={"status": "idle"})


# ── Cards ─────────────────────────────────────────────────────────────────────

def _load_card(task_id: str, board_id: str = "") -> tuple[dict | None, str]:
    """Read a card of the own board as MC has it now -> (card, error)."""
    if not str(task_id or "").strip():
        return None, "no card id given"
    task_id, board_id, err = _resolve_task_board(str(task_id).strip(), board_id)
    if err:
        return None, _en(err)
    detail = _agent_call("GET", f"/agent/boards/{board_id}/tasks/{task_id}/detail")
    if _is_error(detail):
        return None, str(detail["error"])
    return {
        "task_id": str(detail.get("id") or task_id),
        "board_id": str(detail.get("board_id") or board_id),
        "attempt_id": detail.get("dispatch_attempt_id") or "",
        "title": detail.get("title") or "",
        "status": detail.get("status") or "",
    }, ""


def _active_card(task_id: str = "") -> tuple[dict | None, str]:
    """The card a tool call is about -> (card, error). Order: the card this
    process holds, a card named by id, then the card MC lists as the agent's
    current one (so a restarted server finds its card again)."""
    task_id = str(task_id or "").strip()
    held = _held_card()
    if held and (not task_id or held["task_id"].startswith(task_id)):
        return held, ""
    if task_id:
        return _load_card(task_id, held["board_id"] if held else "")
    me = _agent_call("GET", "/agent/me")
    if _is_error(me):
        return None, str(me["error"])
    current = me.get("current_task") or {}
    if not current.get("id"):
        return None, _NO_CARD
    card, err = _load_card(
        str(current["id"]), str(current.get("board_id") or me.get("board_id") or "")
    )
    if card and card["status"] == "in_progress":
        _remember_card(card)
        _start_heartbeat()
    return card, err


def _poll_notes(resp: dict) -> list[str]:
    """Comments and messages a poll delivered. MC shows comments only once, so
    every answer of mc_next_task passes them on."""
    notes = []
    comments = resp.get("new_comments") or []
    if comments:
        lines = ["New comments on your cards (MC shows them only once):"]
        for c in comments:
            lines.append(
                f"- [{c.get('comment_type') or 'message'}] on {c.get('task_title') or 'card'}"
                f" ({str(c.get('task_id') or '')[:8]}): {c.get('content') or ''}"
            )
        notes.append("\n".join(lines))
    messages = resp.get("new_messages") or []
    if messages:
        lines = ["New messages:"]
        for m in messages:
            lines.append(
                f"- {m.get('sender') or 'unknown'} ({m.get('message_type') or 'message'}):"
                f" {m.get('body') or ''}"
            )
            thread, seq = str(m.get("thread_id") or ""), m.get("seq")
            if thread and isinstance(seq, int):
                _shown_seq[thread] = max(_shown_seq.get(thread, 0), seq)
        notes.append("\n".join(lines))
    return notes


_IDLE = (
    "No card for you right now.\n"
    "Next step: call mc_next_task again in about a minute (once a minute is enough)."
)

_HOLD_FLAGS = (
    ("system_halted", "the system is halted"),
    ("system_draining", "the system is draining"),
    ("update_drain", "an MC update is running"),
    ("runtime_not_ready", "your model runtime is not ready"),
)

_VERB_MAP = """The brief was written for the `mc` command line. Over MCP use these tools instead:
- mc ack -> already done, the card is accepted; skip it
- mc comment -> mc_progress
- mc checklist add / list / done -> mc_checklist_add / mc_checklist / mc_checklist_done
- mc finish, mc done, mc review -> mc_finish
- mc vault-search / mc vault-write -> mc_vault_search / mc_vault_write (read a note: mc_vault_read)
- mc deliverable -> mc_register_deliverable
- mc task-get -> mc_task_detail
- mc inbox -> mc_inbox, mc msg -> mc_msg
Any other mc command has no tool here: do not guess, say what you needed in an mc_progress note.
Paths in the brief are paths on the MC host."""


# The poll also hands out an assigned card in review or blocked (once — it
# stamps ack_at itself). Accepting such a card would pull it out of review or
# past its blocker, so only these are accepted; a missing status is the old
# poll answer and is accepted as before.
_ACCEPT_FROM = {"", "inbox", "in_progress"}

_WAITS_FOR = {
    "review": "It waits for the review decision.",
    "blocked": "It waits for the operator or the board lead to clear the blocker.",
}


def _shown_not_accepted(task: dict, card: dict, status: str) -> str:
    task_id = card["task_id"]
    return "\n".join([
        f"Card {_label(card)} is in status '{status}'. MC shows it to you once; it is "
        "not yours to work on now.",
        _WAITS_FOR.get(status, "It waits for someone else."),
        "It was NOT accepted and its status was NOT changed.",
        f"Card id: {task_id}  ·  board: {card['board_id']}",
        "",
        "--- BRIEF (from Mission Control) ---",
        str(task.get("prompt") or "").strip(),
        "--- END OF BRIEF ---",
        "",
        "Next step: do not change its status and do not call mc_finish for it. To add "
        f'something, post an mc_progress note with task_id="{task_id}". Then call '
        "mc_next_task again in a few minutes.",
    ])


def _on_new_task(resp: dict) -> str:
    task = resp.get("task") or {}
    task_id, board_id = str(task.get("id") or ""), str(task.get("board_id") or "")
    if not task_id or not board_id:
        return (
            "Error: MC answered new_task without a card id.\n"
            "Next step: call mc_next_task again."
        )
    attempt = str(task.get("dispatch_attempt_id") or "")
    card = {
        "task_id": task_id, "board_id": board_id, "attempt_id": attempt,
        "title": str(task.get("title") or ""), "status": "in_progress",
    }
    held = _held_card()
    status = str(task.get("status") or "").strip().lower()
    if status not in _ACCEPT_FROM:
        if held and held["task_id"] == task_id:
            _forget_card()
            _stop_heartbeat()
        return _shown_not_accepted(task, card, status)
    again = bool(resp.get("orphaned_run_redispatched")) or bool(
        held and held["task_id"] == task_id
    )
    # Accept the card (ACK). MC delivers a card it gave out before with a fresh
    # attempt id; the same call accepts it again.
    ack = _agent_call(
        "PATCH", f"/agent/boards/{board_id}/tasks/{task_id}",
        json={"status": "in_progress"},
        **({"extra_headers": _attempt_header(attempt)} if attempt else {}),
    )
    if _is_error(ack) and str(ack["error"]).count("In Progress") < 2:
        return (
            f"Error: MC delivered card {_label(card)} but accepting it failed: {ack['error']}\n"
            "Next step: call mc_next_task again."
        )
    _remember_card(card)
    for key in [k for k in _reported_ends if k[1] == task_id]:
        _reported_ends.discard(key)
    _start_heartbeat()

    if again:
        lines = [
            f"This is your card again (MC re-delivered it): {card['title']}",
            "Continue where you stopped, do not start over. Do not create the "
            "checklist again — check it with mc_checklist.",
        ]
    else:
        lines = [f"New card accepted: {card['title']}"]
    lines.append(f"Card id: {task_id}  ·  board: {board_id}")
    if task.get("workspace_path"):
        lines.append(f"Workspace on the MC host: {task['workspace_path']}")
    lines += [
        "",
        "--- BRIEF (from Mission Control) ---",
        str(task.get("prompt") or "").strip(),
        "--- END OF BRIEF ---",
        "",
        _VERB_MAP,
        "",
        "Next step: do the work. If the brief asks for a checklist, create it once "
        "with mc_checklist_add. Send an mc_progress note at least every 15 minutes. "
        "When the work is done call mc_finish with your reflection.",
    ]
    return "\n".join(lines)


def _on_working(resp: dict) -> str:
    task_id = str(resp.get("task_id") or "")
    if not task_id:
        return (
            "MC says you are working on a card but did not name it.\n"
            "Next step: call mc_next_task again in about a minute."
        )
    held = _held_card()
    mine = bool(held and held["task_id"] == task_id)
    card, err = _load_card(task_id, held["board_id"] if mine else "")
    if not card:
        return (
            f"MC lists card {task_id[:8]} as your active card, but reading it failed: {err}\n"
            "Next step: call mc_next_task again in about a minute."
        )
    if card["status"] == "in_progress":
        _remember_card(card)
        _start_heartbeat()
        return (
            f"You are already working on card {_label(card)}.\n"
            "Next step: continue it; send mc_progress notes at least every 15 minutes "
            "and call mc_finish when the work is done."
        )
    if mine:
        _forget_card()
        _stop_heartbeat()
    if card["status"] == "review":
        return (
            f"Card {_label(card)} is waiting for the review decision. "
            "Nothing to do for you now.\n"
            "Next step: call mc_next_task again in a few minutes."
        )
    return (
        f"Card {_label(card)} is in status '{card['status']}'. Nothing to do for you now.\n"
        "Next step: call mc_next_task again in a few minutes."
    )


def _on_run_end(state: str, resp: dict) -> str:
    task_id = str(resp.get("task_id") or "")
    key = (state, task_id)
    held = _held_card()
    if held and held["task_id"] == task_id:
        label = _label(held)
        _forget_card()
        _stop_heartbeat()
    elif key in _reported_ends:
        # MC repeats an end answer while nothing else is to deliver.
        return _IDLE
    else:
        label = task_id[:8]
    _reported_ends.add(key)
    if state == "cancelled":
        return (
            f"Card {label} was cancelled. Stop working on it; do not finish it.\n"
            "Next step: call mc_next_task for your next card."
        )
    return (
        f"Card {label} was stopped by the operator. Stop working on it now; "
        "MC delivers it again if the operator resumes it.\n"
        "Next step: call mc_next_task for your next card."
    )


def _on_idle(resp: dict) -> str:
    held = _held_card()
    for flag, reason in _HOLD_FLAGS:
        if resp.get(flag):
            detail = str(resp.get("detail") or "").strip().rstrip(".")
            lines = [f"MC is holding new work ({reason})" + (f": {detail}." if detail else ".")]
            if held:
                lines.append(f"Card {_label(held)} is still yours.")
                lines.append(
                    "Next step: continue your card; call mc_next_task again in a few minutes."
                )
            else:
                lines.append("Next step: call mc_next_task again in a few minutes.")
            return "\n".join(lines)
    if held:
        _forget_card()
        _stop_heartbeat()
        return (
            f"MC no longer lists card {_label(held)} as your active card — stop working on it.\n"
            + _IDLE
        )
    return _IDLE


@_worker_tool
def mc_next_task() -> str:
    """Fetch your next card from Mission Control and accept it.

    Call it when you start and after every finished card. The answer holds the
    brief of the card and names the next step. No card: call it again in about a
    minute. It also passes on new comments and messages for you.
    """
    kwargs = {"params": {"acked_seq": json.dumps(_shown_seq)}} if _shown_seq else {}
    resp = _agent_call("GET", "/agent/me/poll", **kwargs)
    if not isinstance(resp, dict) or _is_error(resp):
        error = resp["error"] if isinstance(resp, dict) else f"unexpected answer: {resp!r}"
        return (
            f"Error: {error}\n"
            "Next step: fix the connection or token, then call mc_next_task again."
        )
    notes = _poll_notes(resp)
    state = resp.get("state")
    if state == "new_task":
        body = _on_new_task(resp)
    elif state == "working":
        body = _on_working(resp)
    elif state in ("cancelled", "stopped"):
        body = _on_run_end(state, resp)
    elif state == "idle":
        body = _on_idle(resp)
    else:
        body = (
            f"MC answered with a state this server does not know: {state!r}.\n"
            "Next step: call mc_next_task again in about a minute."
        )
    return "\n\n".join([*notes, body])


@_worker_tool
def mc_progress(note: str, task_id: str = "") -> str:
    """Save a short progress note on the card you are working on.

    Send one at least every 15 minutes and whenever something is worth knowing
    (a decision, a problem, a part that is done). The operator reads these notes.

    Args:
        note: What you did or found, in plain words.
        task_id: Only for a card other than the one you hold (id or 8-char prefix).
    """
    note = (note or "").strip()
    if not note:
        return "Error: the note is empty. Say in one or two sentences what you did or found."
    card, err = _active_card(task_id)
    if not card:
        return f"Error: {err}"
    result = _agent_call(
        "POST", f"/agent/boards/{card['board_id']}/tasks/{card['task_id']}/comments",
        json={"content": note, "comment_type": "progress"},
        **({"extra_headers": _attempt_header(card["attempt_id"])} if card.get("attempt_id") else {}),
    )
    if _is_error(result):
        return (
            f"Error: {result['error']}\n"
            "Hint: if MC no longer knows the card as yours, call mc_next_task."
        )
    return (
        f"Progress note saved on card {_label(card)}.\n"
        "Next step: continue the work; call mc_finish when it is done."
    )


# ── Finishing: the CLI's own `mc finish` ──────────────────────────────────────

def _mc_cli():
    """The `mc` command's modules (commands, config, errors), or None when the
    CLI is not next to this script. Lazy and stdlib-only: nothing is imported in
    operator or plain agent mode."""
    if not (_CLI_DIR / "mc_cli" / "commands.py").is_file():
        return None
    if str(_CLI_DIR) not in sys.path:
        sys.path.insert(0, str(_CLI_DIR))
    try:
        from mc_cli import commands, config, errors
    except Exception:
        return None
    return commands, config, errors


class _CliClient:
    """What `mc finish` needs from the CLI's client (`request` and `cfg`), sent
    through this server's HTTP path and as the agent."""

    def __init__(self, cfg, errors):
        self.cfg = cfg
        self._errors = errors

    def request(self, method, path, body=None, query=None, allow_empty=False):
        headers = {
            "Authorization": f"Bearer {self.cfg.require_token()}",
            **_attempt_header(self.cfg.dispatch_attempt_id or ""),
        }
        params = {k: v for k, v in (query or {}).items() if v is not None} or None
        url = f"{MC_BASE}{path.removeprefix('/api/v1')}"
        try:
            with httpx.Client(timeout=30) as c:
                r = c.request(method, url, headers=headers, params=params, json=body)
        except Exception as e:
            raise self._errors.ServerError(f"{method} {path}: {e}") from e
        if r.status_code >= 500:
            raise self._errors.ServerError(f"HTTP {r.status_code} {method} {path}: {r.text[:400]}")
        if r.status_code >= 400:
            raise self._errors.ClientError(f"HTTP {r.status_code} {method} {path}: {r.text[:400]}")
        if not (r.text or "").strip():
            if allow_empty:
                return None
            raise self._errors.ServerError(f"HTTP {r.status_code} {method} {path}: empty answer")
        return r.json()


_REFLECTION_HINT = (
    "Hint: the reflection needs these four sections, each with a real line break "
    "after the heading, at least 80 characters in total:\n"
    "## What was done\n## What worked\n## What was unclear\n## Lesson for agent memory"
)


def _finish_error(text: str, reflection: bool = False, saved: bool = False) -> str:
    """An error of `mc finish` plus what to do about it over MCP. The CLI's own
    retry advice names shell commands, so it is not passed on."""
    hints = []
    if saved:
        hints.append(
            "Hint: the reflection was saved, but the status change failed. Fix what MC "
            "names and call mc_finish again with the same text (no second reflection is "
            "posted within 5 minutes)."
        )
    if "Checklist-Item" in text:
        hints.append(
            "Hint: list the open items with mc_checklist, close each with "
            "mc_checklist_done, then call mc_finish again."
        )
    if reflection or "Reflexion" in text or "Pflichtfelder" in text:
        hints.append(_REFLECTION_HINT)
    if "Stale dispatch_attempt_id" in text or "Fehlender X-Dispatch-Attempt-Id" in text:
        hints.append(
            "Hint: MC gave this card out again in the meantime. Call mc_next_task, "
            "then mc_finish again."
        )
    elif "HTTP 422" in text:
        hints.append(
            "Hint: MC refused the status change. Fix what it names, then call "
            "mc_finish again."
        )
    if "run_control=" in text or "Updates nicht erlaubt" in text:
        hints.append(
            "Hint: MC stopped or cancelled this card. Stop working on it and call "
            "mc_next_task."
        )
    if "Task-Status ist" in text:
        hints.append(
            "Hint: the card is not in progress or in review any more. Call mc_next_task."
        )
    if not hints:
        hints.append("Hint: fix what the message names, then call mc_finish again.")
    return "\n".join([f"Error: {text}", *hints])


def _cli_notes(out: str, err: str) -> list[str]:
    """What `mc finish` told the agent on the side: '#' lines on stdout and
    everything on stderr (the JSON receipt on stdout is not a note)."""
    lines = [ln for ln in out.splitlines() if ln.startswith("#")] + err.splitlines()
    return [ln.lstrip("#").strip() for ln in lines if ln.lstrip("#").strip()]


@_worker_tool
def mc_finish(message: str, review: bool = False, task_id: str = "", workdir: str = "") -> str:
    """Finish the card you are working on. This is the only way to finish a card.

    MC checks first: every checklist item must be closed and the reflection must
    be complete. MC may set 'review' instead of 'done' (a human or a reviewer
    decides then) and it can refuse — the answer names the reason.

    Args:
        message: Your reflection with the four sections '## What was done',
            '## What worked', '## What was unclear', '## Lesson for agent memory'
            (at least 80 characters, real line breaks).
        review: True hands the card to review instead of marking it done.
        task_id: Only for a card other than the one you hold (id or 8-char prefix).
        workdir: Folder of your git checkout, if the work is code — MC records
            the commit and branch from there.
    """
    cli = _mc_cli()
    if cli is None:
        return (
            "Error: the mc command line (scripts/mc-cli) is not next to this server, "
            "so the pre-finish checks cannot run. Nothing was finished.\n"
            "Hint: start mc-mcp.py from a complete Mission Control checkout."
        )
    commands, config, errors = cli
    try:
        commands._validate_reflection(message)
    except errors.CLIError as e:
        return _finish_error(str(e), reflection=True)
    workdir = (workdir or "").strip()
    if workdir and not Path(workdir).expanduser().is_dir():
        return f"Error: workdir '{workdir}' is not a folder.\nHint: pass the folder of your checkout, or leave it empty."
    card, err = _active_card(task_id)
    if not card:
        return f"Error: {err}"
    # `mc finish` also allows review -> done. A worker must never approve its
    # own review card, and a held card may have been moved meanwhile: read the
    # status MC has now.
    now, err = _load_card(card["task_id"], card["board_id"])
    if not now:
        return (
            f"Error: reading card {_label(card)} failed: {err}\n"
            "Hint: nothing was finished. Call mc_finish again in a minute."
        )
    if now["status"] != "in_progress":
        held = _held_card()
        if held and held["task_id"] == card["task_id"]:
            _forget_card()
            _stop_heartbeat()
        why = (
            "It waits for the review decision; a worker never finishes a card in review."
            if now["status"] == "review" else "Only a card in progress can be finished."
        )
        return (
            f"Error: card {_label(card)} is in status '{now['status']}', not in progress. "
            f"Nothing was finished. {why}\n"
            "Next step: call mc_next_task."
        )

    cfg = config.Config(
        api_url=MC_BASE.removesuffix("/api/v1"),
        agent_token=_agent_token(),
        task_id=card["task_id"],
        board_id=card["board_id"],
        dispatch_attempt_id=card.get("attempt_id") or None,
    )
    args = argparse.Namespace(message=message, review=bool(review))
    out, errs = io.StringIO(), io.StringIO()
    failure = ""
    with _finish_lock:
        before = os.getcwd()
        try:
            if workdir:
                os.chdir(Path(workdir).expanduser())
            # The CLI prints its receipt and notes; printed straight to stdout
            # they would corrupt the stdio protocol stream.
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
                commands._cmd_finish(args, _CliClient(cfg, errors), cfg)
        except errors.CLIError as e:
            failure = _finish_error(
                str(e), saved="Reflexion wurde gepostet" in errs.getvalue()
            )
        except (Exception, SystemExit) as e:
            failure = _finish_error(f"{type(e).__name__}: {e}")
        finally:
            os.chdir(before)
    if failure:
        return failure

    detail = _agent_call(
        "GET", f"/agent/boards/{card['board_id']}/tasks/{card['task_id']}/detail"
    )
    notes = _cli_notes(out.getvalue(), errs.getvalue())
    tail = ["MC notes (from the mc command line):", *[f"- {n}" for n in notes]] if notes else []
    if _is_error(detail):
        # "Finished" is never said without MC's word; the card stays held.
        return "\n".join([
            f"mc finish ran for card {_label(card)}, but MC could not read the card "
            f"afterwards: {detail['error']}",
            *tail,
            "Next step: check its status with mc_task_detail. Still in progress: call "
            "mc_finish again with the same text (no second reflection is posted within "
            "5 minutes). Done or review: call mc_next_task.",
        ])
    status = str(detail.get("status") or "unknown")
    if status == "in_progress":
        return "\n".join([
            f"Card {_label(card)} is still in progress — MC did not change its status.",
            *tail,
            "Next step: read the notes, fix what they name, then call mc_finish again.",
        ])
    held = _held_card()
    if held and held["task_id"] == card["task_id"]:
        _forget_card()
        _stop_heartbeat()
    return "\n".join([
        f"Card finished: {_label(card)} — status is now {status}.",
        *tail,
        "Next step: call mc_next_task for your next card.",
    ])


# ── Vault ─────────────────────────────────────────────────────────────────────

def _vault_call(method: str, path: str, **kwargs):
    """Agent: the agent's vault routes (scopes vault:read / vault:write).
    Operator: the admin routes."""
    if _is_agent():
        return _agent_call(method, f"/agent{path}", **kwargs)
    return _api(method, path, **kwargs)


@_vault_tool()
def mc_vault_search(q: str, limit: int = 10, type: str = "", agent: str = "") -> str:
    """Search the vault (Mission Control's shared knowledge: notes, lessons, decisions).

    Search before you start a card — someone may have solved it before.

    Args:
        q: Search text.
        limit: Number of hits, 1-50.
        type: Only this note type (note | knowledge | lesson | reference | journal).
        agent: Only notes written by this agent.
    """
    q = (q or "").strip()
    if not q:
        return "Error: the search text is empty."
    params = {"q": q, "limit": max(1, min(int(limit or 10), 50))}
    if type.strip():
        params["type"] = type.strip()
    if agent.strip():
        params["agent"] = agent.strip()
    result = _vault_call("GET", "/vault/search", params=params)
    if _is_error(result):
        return f"Error: {result['error']}"
    hits = result.get("hits") or []
    if not hits:
        return f"No notes for '{q}'.\nNext step: try other words, or go on without a note."
    lines = [f"{len(hits)} notes for '{q}':"]
    for h in hits:
        meta = " · ".join(str(h[k]) for k in ("type", "agent", "date") if h.get(k))
        lines.append(f"- {h.get('title') or '(no title)'} — {h.get('path')}" + (f" [{meta}]" if meta else ""))
        excerpt = " ".join(str(h.get("content") or "").split())
        if excerpt:
            lines.append(f"  {excerpt[:200]}{'…' if len(excerpt) > 200 else ''}")
    lines.append("Next step: read a note in full with mc_vault_read(path).")
    return "\n".join(lines)


@_vault_tool()
def mc_vault_read(path: str) -> str:
    """Read one vault note in full.

    Args:
        path: Path of the note as mc_vault_search shows it, e.g. 'knowledge/x.md'.
    """
    path = (path or "").strip().lstrip("/")
    if not path:
        return "Error: the path is empty."
    if ".." in path.split("/"):
        return "Error: the path must not contain '..'."
    result = _vault_call("GET", f"/vault/note/{quote(path, safe='/')}")
    if _is_error(result):
        return f"Error: {result['error']}"
    lines = [f"{k}: {v}" for k, v in (result.get("frontmatter") or {}).items()]
    content = str(result.get("content") or "")
    if len(content) > _NOTE_CAP:
        content = content[:_NOTE_CAP] + f"\n\n[cut after {_NOTE_CAP} characters — the note is longer]"
    return "\n".join([*lines, "", content]) if lines else content


@_vault_tool(agent_only=True)
def mc_vault_write(
    title: str, content: str, type: str = "note", tags: list[str] | None = None,
    task_id: str = "", related_notes: list[str] | None = None,
) -> str:
    """Write a note into the vault so the next agent can find it.

    Write what is worth keeping beyond this card: a lesson, a fact about a
    system, a decision with its reason. MC files the note in the background.

    Args:
        title: 3-80 characters.
        content: The note in Markdown, at least 10 characters.
        type: note | knowledge | lesson | reference | journal.
        tags: Up to 8 tags.
        task_id: Card the note belongs to (default: the card you hold).
        related_notes: Paths of up to 8 related notes.
    """
    body: dict = {"title": (title or "").strip(), "content": content, "type": (type or "note").strip()}
    if tags:
        body["tags"] = list(tags)
    held = _held_card()
    task_id = (task_id or "").strip() or (held["task_id"] if held else "")
    if task_id:
        body["task_id"] = task_id
    if related_notes:
        body["related_notes"] = list(related_notes)
    result = _vault_call("POST", "/vault/note", json=body)
    if _is_error(result):
        return f"Error: {result['error']}"
    target = result.get("expected_target") or "the vault"
    return (
        f"Note accepted. MC files it in the background as {target}; "
        "it can take a moment until mc_vault_search finds it."
    )


# ── Handbook and connect instructions ─────────────────────────────────────────

_HANDBOOK = """# Mission Control — handbook for workers

Mission Control (MC) is a task board for AI agents. The operator (a human) and a
lead put work on the board as cards. You are a registered worker: MC gives you one
card at a time, you do the work and report back through these tools.

## The loop
1. mc_next_task — fetches your next card and accepts it. The answer holds the
   brief and names the next step. No card: wait about a minute, call it again.
2. Do what the brief says. Before you start, mc_vault_search may show that the
   problem was solved before.
3. mc_progress — a short note at least every 15 minutes, and when something is
   worth knowing. The operator reads these notes instead of watching you.
4. mc_finish — when the work is done. Then call mc_next_task again.

## A card's life
inbox -> in_progress (you accepted it) -> review or done. MC can also stop or
cancel a card; mc_next_task tells you. Stop working on such a card at once.
MC may give you the same card again (after a long silence or a restart). Then
continue where you stopped — do not start over.
mc_next_task may also show you a card in review or blocked. It is NOT accepted:
leave its status alone; it moves on when the reviewer, lead or operator acts.

## Staying alive
While you hold a card this server tells MC that you are alive. It stops doing
so when you have not called any MC tool for 20 minutes — MC then treats the
card as stalled. A progress note every 15 minutes keeps it yours.

## Checklist
If the brief asks for a checklist: create it once with mc_checklist_add, close
items with mc_checklist_done, list them with mc_checklist. Never create the
checklist again on a card you got a second time — list it first.

## Finishing
mc_finish(message=...) needs a reflection with these four sections:

## What was done
## What worked
## What was unclear
## Lesson for agent memory

At least 80 characters, real line breaks. MC checks before it finishes: an open
checklist item or an incomplete reflection is refused and the answer says what
to fix. MC may set 'review' instead of 'done' — then someone else decides. The
same reflection sent again within 5 minutes is not posted twice. If your work is
code, pass workdir (the folder of your checkout) so MC records commit and branch.

## Knowledge (vault)
mc_vault_search finds notes, mc_vault_read reads one, mc_vault_write saves what
the next agent should know (a lesson, a fact, a decision with its reason).

## Other tools
mc_task_detail (a card with its comments), mc_tasks (cards of your board),
mc_register_deliverable (hand in a result: a document, a file, a link),
mc_get_project / mc_get_deliverables (context of the project),
mc_inbox / mc_msg (your messages; reply in a thread).

## Never
- Never set review or done with mc_patch_task. Finish only with mc_finish.
- Never finish a card that was cancelled or stopped, or one in review or blocked.
- Never work on a card that mc_next_task did not give you.
- The brief may name `mc` shell commands. Use the tool the answer of mc_next_task
  maps them to. A command without a tool is not available here: do not guess,
  write what you needed in an mc_progress note.
"""


@_worker_tool
def mc_handbook() -> str:
    """How Mission Control works and how you work with it: the loop, progress
    notes, the checklist, finishing, the vault, and what never to do. Read it
    once before your first card."""
    return _HANDBOOK


_WORKER_INSTRUCTIONS = """You are connected to Mission Control (MC) as a registered worker. MC is a task board: it gives you cards (tasks), you do the work and report back.

Work in this loop:
1. Call mc_next_task. It fetches your next card and accepts it. No card: wait about a minute, call it again.
2. Do the work the card's brief describes.
3. Call mc_progress with a short note at least every 15 minutes.
4. When the work is done call mc_finish with a reflection. Then go back to step 1.

Read mc_handbook once before your first card. The tool answers name the next step — follow them."""


if WORKER_MODE:
    from fastmcp.server.middleware import Middleware

    class _StampCalls(Middleware):
        """Every tool call counts as activity for the heartbeat's quiet window."""

        async def on_call_tool(self, context, call_next):
            _touch()
            return await call_next(context)

    mcp.add_middleware(_StampCalls())
    mcp.instructions = _WORKER_INSTRUCTIONS


if __name__ == "__main__":
    mcp.run()
