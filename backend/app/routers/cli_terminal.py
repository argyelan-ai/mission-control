# backend/app/routers/cli_terminal.py
"""CLI Terminal Sessions — REST + WebSocket Proxy zur Bridge."""
import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodel import select

from pydantic import BaseModel

from app.auth import require_user, generate_agent_token
from app.config import effective_host_ssh_user, settings
from app.database import get_session
from app.models.agent import Agent


class CliProvisionPayload(BaseModel):
    model: str = "nvidia/nemotron-3-super"  # Default — beim Provisioning explizit setzen!
    system_prompt: str = ""   # Identity only (Name, Rolle, Faehigkeiten) — Protokoll wird auto-angehaengt
    role: str = ""            # Convenience: Rolle fuer Default-Identity wenn system_prompt leer
    skills: list[str] = []    # Convenience: Faehigkeiten fuer Default-Identity
    extra_plugins: list[str] = []


# ── CLI-Bridge Protokoll — Single Source of Truth ────────────────────────────
# Dieses Protokoll wird IMMER an jeden CLI-Bridge Agent System-Prompt angehaengt.
# Nicht konfigurierbar — gilt fuer alle CLI-Bridge Agents gleichermassen.
# Anpassbar: nur die Identity (Name, Rolle, Faehigkeiten) via system_prompt-Payload.
_CLI_BRIDGE_PROTOCOL = """
## Pflicht-Verhalten (gilt fuer jeden Task)
1. **Sofort ACK**: PATCH status: in_progress — bevor du irgendwas anderes machst
2. **Checkliste anlegen**: POST /checklist direkt nach ACK — konkrete Arbeitsschritte definieren
3. **Regelmaessige Updates**: Progress-Kommentar + Checkpoint nach jedem groesseren Schritt
4. **Artefakte registrieren**: Jede produzierte Datei, URL, Screenshot als Deliverable (POST /deliverables)
5. **Fertig**: Subtasks → PATCH status: done | Root-Tasks → PATCH status: review — jeweils mit Resolution-Kommentar
6. **Blockiert**: PATCH status: blocked + Blocker-Kommentar mit genauen Fehlerdetails

## Kein Task gilt als fertig ohne
- Status korrekt gesetzt (done fuer Subtasks, review fuer Root-Tasks)
- Alle Checklisten-Items abgehakt
- Deliverables registriert (fuer jede produzierte Datei/URL)
- Resolution-Kommentar gepostet

## 5-Minuten-Blocker-Regel
Nach 2-3 Versuchen (max 5 Min) ohne Fortschritt → SOFORT blocked. Nie still aufgeben.

## Hilfe holen (Help Request)
Wenn du fuer deine Aufgabe Unterstuetzung brauchst die ausserhalb deiner Kompetenz liegt
(z.B. Recherche, Design, andere Fachgebiete), nutze den Help Request Endpoint.
Dein Task wird automatisch pausiert und du bekommst das Ergebnis als Nachricht.

## Klaerungsfrage stellen
Bei Unklarheiten zur Aufgabe: stelle dem Operator eine strukturierte Frage via Clarification Endpoint.
Dein Task wird pausiert bis der Operator antwortet. Lieber fragen als raten.

## Strukturierte Blocker
Wenn du blockiert bist, melde den Blocker mit strukturierten Feldern:
- blocker_type: missing_info | technical_problem | decision_needed | permission_needed | dependency_blocked | other
- blocker_description: Was genau das Problem ist
- blocker_question: Konkrete Frage an den Operator

## Progress-Kommentar Format
**Update** — Was konkret getan
**Evidence** — Dateipfade, Outputs, Links
**Next** — Naechste 1-2 Schritte

## Jeder Task-Prompt ist self-contained
Jeder Task enthaelt alle curl-Befehle fuer ACK, Checkliste, Progress, Deliverables, Checkpoint und Status-Updates.
Folge diesen Anweisungen genau."""


def _default_identity(agent_name: str, role: str = "", skills: list | None = None) -> str:
    """Minimale Identity fuer einen neuen CLI-Bridge Agent."""
    role_text = role.strip() or "Developer Agent (Coding, Frontend, Backend, Scripts, Prototypen)"
    if skills:
        skills_text = "\n".join(f"- {s}" for s in skills)
    else:
        skills_text = (
            "- Frontend: React, Next.js, TypeScript, Tailwind\n"
            "- Backend: Python, FastAPI, Node.js\n"
            "- Tools: git, gh CLI, curl, npm, pip"
        )
    return (
        f"Du bist {agent_name}, ein autonomer Agent in Mission Control (MC).\n\n"
        f"## Identitaet\n"
        f"- Name: {agent_name}\n"
        f"- Rolle: {role_text}\n"
        f"- Auth: API-Token in $MC_AGENT_TOKEN (Shell-Umgebungsvariable)\n"
        f"- MC API: $MC_API_URL/api/v1/agent/\n\n"
        f"## Faehigkeiten\n{skills_text}"
    )


def _build_cli_system_prompt(agent_name: str, identity: str) -> str:
    """Kombiniert Agent-Identity mit festem Protokoll.

    identity: Wer der Agent ist — anpassbar pro Agent (Name, Rolle, Faehigkeiten)
    Protokoll: Wie der Agent arbeitet — immer gleich, nicht konfigurierbar
    """
    return identity.strip() + "\n\n" + _CLI_BRIDGE_PROTOCOL.strip()


logger = logging.getLogger("mc.cli_terminal")
router = APIRouter(prefix="/api/v1", tags=["cli-terminal"])


# ── Bridge HTTP Helpers ───────────────────────────────────────────────────────

def _bridge_get(path: str):
    """Synchroner GET zur Bridge. Gibt None bei Fehler."""
    url = f"{settings.free_code_bridge_url}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Bridge GET %s failed: %s", path, e)
        return None


def _bridge_post(path: str, body: dict, timeout: int = 5) -> dict:
    """Synchroner POST zur Bridge."""
    url = f"{settings.free_code_bridge_url}{path}"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Bridge POST %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


def _bridge_delete(path: str) -> dict:
    """Synchroner DELETE zur Bridge."""
    url = f"{settings.free_code_bridge_url}{path}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Bridge DELETE %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


async def _get_cli_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> Agent:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if getattr(agent, "agent_runtime", "openclaw") != "cli-bridge":
        raise HTTPException(400, "Agent ist kein CLI-Bridge-Agent")
    return agent


# ── REST Endpoints ────────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/cli-sessions")
async def list_cli_sessions(
    agent: Agent = Depends(_get_cli_agent),
):
    """Alle aktiven CLI tmux-Sessions von der Bridge abrufen."""
    sessions = _bridge_get("/sessions")
    return sessions or []


@router.get("/cli-sessions")
async def list_all_cli_sessions(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Alle aktiven CLI tmux-Sessions über alle Agents (global)."""
    # Bridge abfragen
    sessions_raw = _bridge_get("/sessions") or []
    if not sessions_raw:
        return []

    # Alle cli-bridge Agents laden (auch free-code-bridge für Transition)
    result = await session.exec(
        select(Agent).where(
            Agent.agent_runtime.in_(["cli-bridge", "free-code-bridge"])
        )
    )
    agents = result.all()
    # Slug → Agent Mapping aufbauen
    slug_map = {a.name.lower().replace(" ", "-"): a for a in agents}

    enriched = []
    for s in sessions_raw:
        sname = s.get("session", "")
        is_permanent = s.get("permanent", False)
        is_shell = s.get("shell", False)
        if is_shell:
            # Shell session: {agent_slug}-shell
            agent_slug = sname[:-6]  # strip "-shell"
        elif is_permanent:
            # Permanent session: session name == agent_slug (z.B. "freecode")
            agent_slug = sname
        else:
            # Per-task session: {agent_slug}-{8chars}
            segments = sname.rsplit("-", 1)
            agent_slug = segments[0] if len(segments) == 2 else sname
        agent = slug_map.get(agent_slug)
        enriched.append({
            **s,
            "agent_slug": agent_slug,
            "agent_id": str(agent.id) if agent else None,
            "agent_name": agent.name if agent else agent_slug,
            "shell": is_shell,
        })
    return enriched


@router.post("/agents/{agent_id}/terminal/{task_id}/input")
async def send_terminal_input(
    agent_id: uuid.UUID,
    task_id: str,
    body: dict,
    agent: Agent = Depends(_get_cli_agent),
):
    """Text in eine laufende CLI-Session schicken."""
    text = body.get("text", "")
    if not text:
        raise HTTPException(400, "text darf nicht leer sein")
    result = _bridge_post(f"/input/{task_id}", {"text": text})
    return result


@router.delete("/agents/{agent_id}/terminal/{task_id}")
async def kill_terminal_session(
    agent_id: uuid.UUID,
    task_id: str,
    agent: Agent = Depends(_get_cli_agent),
):
    """CLI tmux-Session beenden."""
    result = _bridge_delete(f"/sessions/{task_id}")
    return result


# ── WebSocket Terminal Stream ─────────────────────────────────────────────────

async def _proxy_terminal_websocket(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    session_key: str,
    token: Optional[str],
    session: AsyncSession,
):
    """Gemeinsame Logik für Terminal-WebSocket-Proxy.

    session_key: entweder agent_slug (permanent session) oder task_id (per-task)
    """
    # Auth check
    if not token:
        await websocket.close(code=4001)
        return
    try:
        from jose import jwt as _jwt
        payload = _jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        if not payload.get("sub"):
            await websocket.close(code=4001)
            return
    except Exception:
        await websocket.close(code=4001)
        return

    # Agent check
    agent = await session.get(Agent, agent_id)
    if not agent or getattr(agent, "agent_runtime", "openclaw") != "cli-bridge":
        await websocket.close(code=4004)
        return

    await websocket.accept()

    # Bridge WebSocket URL (host.docker.internal because backend is in Docker)
    bridge_ws_url = settings.free_code_bridge_url.replace("http://", "ws://").replace("https://", "wss://")
    # free_code_bridge_url is http://host.docker.internal:18792 → ws://host.docker.internal:18793
    bridge_ws_url = bridge_ws_url.replace(":18792", ":18793")
    ws_url = f"{bridge_ws_url}/{session_key}"

    logger.info("WS proxy: agent=%s session=%s → %s", agent_id, session_key, ws_url)

    try:
        import websockets as _ws
        async with _ws.connect(ws_url) as bridge_ws:

            async def browser_to_bridge():
                while True:
                    try:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes"):
                            await bridge_ws.send(msg["bytes"])
                        elif msg.get("text"):
                            await bridge_ws.send(msg["text"])
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break

            async def bridge_to_browser():
                async for msg in bridge_ws:
                    try:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                    except Exception:
                        break

            await asyncio.gather(browser_to_bridge(), bridge_to_browser())

    except Exception as e:
        logger.error("WS proxy error: %s", e)
        try:
            await websocket.send_text(f"\r\n\x1b[31m[Bridge nicht erreichbar: {e}]\x1b[0m\r\n")
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass



@router.post("/agents/{agent_id}/provision")
async def provision_cli_agent(
    agent_id: uuid.UUID,
    payload: CliProvisionPayload | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """CLI Bridge Agent provisionieren — Filesystem-Setup via Bridge."""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if getattr(agent, "agent_runtime", "openclaw") != "cli-bridge":
        raise HTTPException(400, "Agent ist kein CLI-Bridge-Agent")

    # 1. Neuen Token generieren (format: salt_hex:dk_hex — kompatibel mit verify_agent_token)
    raw_token, token_hash = generate_agent_token()

    # Token-Hash in DB speichern
    agent.agent_token_hash = token_hash
    agent.provision_status = "provisioning"
    session.add(agent)
    await session.commit()

    # Vault-Rotation mc_token_{slug}: /internal/bootstrap muss den NEUEN Token
    # liefern — sonst startet der Container mit dem alten (Fresh-Install-Fix).
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, agent.name, raw_token)

    agent_slug = agent.name.lower().replace(" ", "-")

    # 2. System-Prompt aufbauen: Identity (anpassbar) + Protokoll (fix)
    # Prio 1: explizites system_prompt aus API-Payload
    # Prio 2: soul_md aus DB (gerendert via setup-coordination / template instantiation)
    # Prio 3: generischer Fallback
    identity = (
        (payload and payload.system_prompt.strip())
        or (agent.soul_md and agent.soul_md.strip())
        or _default_identity(
            agent_name=agent.name,
            role=(payload and payload.role) or "",
            skills=(payload and payload.skills) or [],
        )
    )
    full_system_prompt = _build_cli_system_prompt(agent.name, identity)

    # 3. Bridge provisionieren
    effective_model = (payload and payload.model) or getattr(agent, "model", None) or "nvidia/nemotron-3-super"
    is_claude_model = effective_model.startswith("claude-")

    bridge_payload: dict = {
        "mc_agent_token": raw_token,
        "model": effective_model,
        "system_prompt": full_system_prompt,
        "extra_plugins": (payload and payload.extra_plugins) or [],
        "cli_plugins": agent.cli_plugins,  # None = alle, [] = keine, ["x"] = nur diese
    }
    if is_claude_model:
        bridge_payload["cli_bin"] = str(Path(settings.home_host) / ".local" / "bin" / "claude")

    result = _bridge_post(f"/provision/{agent_slug}", bridge_payload, timeout=30)

    if result.get("ok"):
        agent.provision_status = "provisioned"
        if not agent.workspace_path or agent.workspace_path == "/home/mcuser/free-code-projects":
            # Host-side path of the free-code projects mount (see
            # FREE_CODE_PATH_MAPPINGS) — derived from the host home, not hardcoded.
            agent.workspace_path = str(Path(settings.home_host) / "FreeCode" / "projects")
    else:
        agent.provision_status = "error"

    session.add(agent)
    await session.commit()

    # 4. Docker-Agent File-Sync: SOUL.md/HEARTBEAT.md/TOOLS.md/USER.md/MEMORY.md
    # ins claude-config Bind-Mount schreiben (ADR-006: DB -> Templates -> Files).
    # entrypoint.sh des Containers liest SOUL.md und gibt sie via
    # --append-system-prompt an openclaude weiter.
    file_sync_results: dict[str, str] = {}
    if result.get("ok"):
        try:
            from app.services.docker_agent_sync import sync_docker_agent_files
            file_sync_results = await sync_docker_agent_files(session, agent)
        except Exception as e:
            logger.warning("provision_cli_agent: docker-agent file-sync failed for %s: %s", agent.name, e)
            file_sync_results = {"_error": str(e)}

    return {
        "agent_id": str(agent_id),
        "agent_name": agent.name,
        "provision_status": agent.provision_status,
        "bridge_result": result,
        "file_sync": file_sync_results,
        "token": raw_token if result.get("ok") else None,
    }


@router.get("/agents/{agent_id}/provision")
async def get_cli_agent_provision_status(
    agent_id: uuid.UUID,
    agent: Agent = Depends(_get_cli_agent),
):
    """Provisioning-Status eines CLI-Bridge Agents von der Bridge abrufen."""
    agent_slug = agent.name.lower().replace(" ", "-")
    result = _bridge_get(f"/provision/{agent_slug}")
    if result is None:
        return {"error": "Bridge nicht erreichbar", "provision_status": agent.provision_status}
    return {**result, "provision_status": agent.provision_status}


@router.post("/agents/{agent_id}/restart-worker")
async def restart_worker_session(
    agent_id: uuid.UUID,
    agent: Agent = Depends(_get_cli_agent),
):
    """Worker-Session des CLI-Bridge Agents neu starten (kill + start)."""
    agent_slug = agent.name.lower().replace(" ", "-")
    result = _bridge_post(f"/worker/{agent_slug}/restart", {})
    if result is None:
        raise HTTPException(status_code=503, detail="Bridge nicht erreichbar")
    return result


@router.post("/cli-sessions/restart")
async def restart_bridge(
    current_user=Depends(require_user),
):
    """CLI Bridge neu starten (z.B. nach Config-Änderungen)."""
    result = _bridge_post("/restart", {})
    return result


@router.websocket("/agents/{agent_id}/terminal/ws")
async def terminal_websocket_permanent(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    token: Optional[str] = None,
    shell: Optional[bool] = False,
    session: AsyncSession = Depends(get_session),
):
    """WebSocket: verbindet mit permanenter Worker-Session oder Shell-Session.

    Auth via ?token=<jwt> Query-Param.
    Shell-Session via ?shell=1.
    Proxies bidirectionally: browser ↔ backend ↔ bridge WS (PTY ↔ tmux attach).
    """
    agent = await session.get(Agent, agent_id)
    agent_slug = agent.name.lower().replace(" ", "-") if agent else str(agent_id)
    session_key = f"{agent_slug}-shell" if shell else agent_slug
    await _proxy_terminal_websocket(websocket, agent_id, session_key, token, session)


@router.websocket("/agents/{agent_id}/terminal/{task_id}/ws")
async def terminal_websocket(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    task_id: str,
    token: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """WebSocket: proxied PTY terminal via Bridge WebSocket Server (per-task, legacy).

    Auth via ?token=<jwt> Query-Param.
    Proxies bidirectionally: browser ↔ backend ↔ bridge WS (PTY ↔ tmux attach).
    """
    await _proxy_terminal_websocket(websocket, agent_id, task_id, token, session)


# ── Direct PTY Terminal (docker exec → tmux) ─────────────────────────────────

import os
import pty
import struct
import fcntl
import termios


@router.websocket("/agents/{agent_id}/terminal")
async def agent_terminal_ws(
    websocket: WebSocket,
    agent_id: str,
    token: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """WebSocket PTY-Bridge: Browser xterm.js <-> Backend <-> docker exec <-> Container tmux.

    Direkte Verbindung via PTY (ohne Bridge). Nutzt 'docker exec -it mc-agent-{name} tmux attach'.
    Auth: JWT via ?token=<jwt> Query-Param (WebSocket kann keine Auth-Header senden).
    Resize: JSON {type: "resize", cols: N, rows: N} als Text-Message.
    Input:  Bytes direkt, oder JSON {type: "input", data: "..."} als Text-Message.
    """
    # 1. Auth: JWT aus Query-Param verifizieren
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    try:
        from jose import jwt as _jwt
        payload = _jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        if not payload.get("sub"):
            await websocket.close(code=4001, reason="Invalid token")
            return
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # 2. Agent aus DB laden
    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        await websocket.close(code=4004, reason="Invalid agent ID")
        return

    agent = await session.get(Agent, agent_uuid)
    if agent is None:
        await websocket.close(code=4004, reason="Agent not found")
        return

    container_name = f"mc-agent-{agent.name.lower().replace(' ', '-')}"
    tmux_session = agent.name.lower().replace(" ", "-")

    await websocket.accept()

    # 3. PTY öffnen
    master_fd, slave_fd = pty.openpty()

    # 4. docker exec starten — tmux attach-session auf Container-Session
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-itu", "agent", container_name,
        "tmux", "attach-session", "-dt", tmux_session,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
    )
    os.close(slave_fd)

    logger.info(
        "PTY terminal: agent=%s container=%s tmux=%s pid=%s",
        agent_id, container_name, tmux_session, proc.pid,
    )

    # 5. Bidirektionale Bridge
    async def read_from_pty():
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, lambda: os.read(master_fd, 4096))
                if not data:
                    break
                await websocket.send_bytes(data)
        except (OSError, WebSocketDisconnect, RuntimeError):
            pass

    async def write_to_pty():
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    os.write(master_fd, msg["bytes"])
                elif msg.get("text"):
                    text = msg["text"]
                    handled = False
                    # JSON-Message? (resize / input)
                    try:
                        data = json.loads(text)
                        if isinstance(data, dict) and data.get("type") == "resize":
                            cols = data.get("cols", 80)
                            rows = data.get("rows", 24)
                            fcntl.ioctl(
                                master_fd,
                                termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0),
                            )
                            handled = True
                        elif isinstance(data, dict) and data.get("type") == "input":
                            os.write(master_fd, data["data"].encode())
                            handled = True
                    except (json.JSONDecodeError, ValueError):
                        pass
                    # Kein bekanntes JSON → rohe Tastatur-Eingabe von xterm.js
                    if not handled:
                        os.write(master_fd, text.encode())
        except (WebSocketDisconnect, OSError, RuntimeError):
            pass

    try:
        await asyncio.gather(read_from_pty(), write_to_pty())
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


# ── Docker Session Agents ─────────────────────────────────────────────────────

@router.get("/docker-sessions/agents")
async def list_docker_session_agents(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Alle Agents mit existierendem Docker-Container (laufend ODER gestoppt).

    Filtert:
    - cli-bridge runtime (keine openclaw/host agents)
    - Nur solche für die ein mc-agent-{slug} Container existiert

    Jeder zurückgegebene Agent bekommt container_state (running|exited|...).
    """
    result = await session.exec(
        select(Agent)
        .where(Agent.agent_runtime == "cli-bridge")  # type: ignore[union-attr]
        .order_by(Agent.name)
    )
    agents = list(result.all())

    # Alle Container (auch gestoppte) mit State abfragen
    container_state: dict[str, str] = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        for line in stdout.decode().strip().split("\n"):
            if "\t" in line:
                name, state = line.split("\t", 1)
                container_state[name] = state
    except Exception as e:
        logger.warning("docker ps failed: %s", e)

    filtered = []
    for agent in agents:
        container_name = f"mc-agent-{agent.name.lower().replace(' ', '-')}"
        if container_name in container_state:
            agent_dict = agent.model_dump()
            agent_dict["container_state"] = container_state[container_name]
            filtered.append(agent_dict)

    return filtered


# ── Host Session Agents ───────────────────────────────────────────────────────

@router.get("/host-sessions/agents")
async def list_host_session_agents(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Alle Agents mit agent_runtime='host'.

    Pendant zu /docker-sessions/agents, aber für host-side Agents (z.B. Boss
    nach Migration). Liveness via agents.last_seen_at (wird vom heartbeat-
    Endpoint alle 30s aktualisiert) — nicht via Datei-mtime, weil macOS Docker
    Desktop bind mounts den mtime cachen und stale Werte liefern.
    """
    from datetime import datetime, timezone

    result = await session.exec(
        select(Agent)
        .where(Agent.agent_runtime == "host")
        .order_by(Agent.name)
    )
    agents = list(result.all())

    now = datetime.now(timezone.utc)
    out = []
    for agent in agents:
        slug = agent.name.lower().replace(" ", "-")
        agent_dict = agent.model_dump()
        agent_dict["session_name"] = f"{slug}-host"
        if agent.last_seen_at is not None:
            age = (now - agent.last_seen_at).total_seconds()
            agent_dict["session_running"] = age < 90
        else:
            agent_dict["session_running"] = False
        out.append(agent_dict)
    return out


# ── Host-Agent tmux Targets ─────────────────────────────────────────────────
# Pro host-runtime Slug: an welche tmux-Session + Socket soll die Bridge
# attachen? Boss fehlt absichtlich -> Bridge nutzt ihren eingebauten Default
# ("boss-host:0" auf Boss-Custom-Socket), sodass das Boss-Streaming exakt
# wie vor Phase 24 funktioniert (Backwards-Compat).
#
# Hermes (Phase 24, HERM-01): user-default tmux Socket + hermes-worker Session.
# Der Socket wird bei jedem Request anhand $TMPDIR / UID aufgeloest, damit das
# Backend Container-Process die Host-Datei trifft (volume-mount per
# ${TMPDIR_HOST} oder Bridge laeuft host-seitig — siehe Plan 04 + 08).
def _user_default_tmux_socket() -> str:
    """Default tmux Socket-Pfad fuer den User (typischerweise /tmp/tmux-<uid>/default)."""
    uid = os.environ.get("HOST_UID") or str(os.getuid())
    tmpdir = os.environ.get("TMUX_TMPDIR") or os.environ.get("TMPDIR") or "/tmp"
    tmpdir = tmpdir.rstrip("/")
    # tmux schreibt per Default in /tmp/tmux-<uid>/default
    if tmpdir == "/tmp":
        return f"/tmp/tmux-{uid}/default"
    return f"{tmpdir}/tmux-{uid}/default"


_HOST_AGENT_TMUX_TARGETS: dict[str, dict[str, str]] = {
    # Hermes: user-default tmux, Session 'hermes-worker' (siehe ADR-029)
    "hermes": {
        "session": "hermes-worker",
        # Socket wird bei Bedarf rendered — Default-Wert hier reicht fuer Tests.
        "socket": _user_default_tmux_socket(),
    },
}


def _hermes_ws_send_keys(message: str) -> dict:
    """Forward a WS write-message to the hermes-worker tmux session via send-keys.

    HERM-15 (Plan 27-06): direct tmux send-keys path so the operator can type
    in the MC Sessions-UI and keystrokes reach Hermes' tmux window without
    going through the PTY-bridge.

    Args:
        message: text to inject into the tmux session.

    Returns:
        {"ok": True} on success, {"ok": False, "error": "<reason>"} on failure.

    Design decisions:
    - Empty / whitespace-only messages are dropped silently (no subprocess call).
    - `tmux has-session -t hermes-worker` is checked first; if absent the WS
      stays open but returns an error dict so the caller can relay it to the
      client — no crash, no silent drop.
    - Session name is hardcoded to "hermes-worker" (T-27-10 mitigation: no
      user-controllable part in the tmux target).
    """
    if not message or not message.strip():
        return {"ok": False, "error": "empty message dropped"}

    session_name = "hermes-worker"

    # 1. Verify session exists before sending (T-27-10)
    check = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if check.returncode != 0:
        return {"ok": False, "error": f"tmux session '{session_name}' not found"}

    # 2. Send keystrokes — trailing "" means "no Enter" (caller decides)
    result = subprocess.run(
        ["tmux", "send-keys", "-t", session_name, message, ""],
        capture_output=True,
    )
    if result.returncode != 0:
        return {"ok": False, "error": f"tmux send-keys failed: {result.stderr}"}

    return {"ok": True}


def _build_host_upstream_url(slug: str) -> Optional[str]:
    """Berechnet die Upstream-WebSocket-URL fuer den host-pty-bridge.

    Returns:
        - Boss (Default-Slug): URL ohne query-params -> Bridge nimmt Default
        - Hermes / weitere registrierte Slugs: URL mit ?session=&socket=
        - Unbekannter Slug der nicht 'boss' ist: None (-> Caller schliesst WS)
    """
    base = "ws://host.docker.internal:7682/"
    if slug == "boss" or slug == "boss-host":
        # Backwards-Compat: keine query-params -> Bridge attached an boss-host:0
        return base
    target = _HOST_AGENT_TMUX_TARGETS.get(slug)
    if target is None:
        return None
    from urllib.parse import urlencode
    params = urlencode(
        {"session": target["session"], "socket": target["socket"]}
    )
    return f"{base}?{params}"


@router.websocket("/host-agents/{agent_id}/terminal")
async def host_agent_terminal_ws(
    websocket: WebSocket,
    agent_id: str,
    token: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """WebSocket-Bridge: Browser xterm.js <-> Backend <-> Host-PTY-Bridge <-> tmux Boss-Host.

    Pendant zu agent_terminal_ws (docker exec Variante), aber:
      - Kein PTY im Backend, kein docker exec
      - Proxy zu Custom WS-PTY-Bridge auf Host (host.docker.internal:7682)
      - Bridge attached selbst per pty an tmux-Session via ${HOME}/.mc/agents/boss-host/.tmux.sock
      - Wire-Format: raw bytes — kein ttyd Frame-Protokoll, kein Subprotocol

    Voraussetzung: host-pty-bridge launchd-Job laeuft (com.openclaw.host-pty-bridge).
    """
    # 1. Auth: JWT via ?token=
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    try:
        from jose import jwt as _jwt
        payload = _jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        if not payload.get("sub"):
            await websocket.close(code=4001, reason="Invalid token")
            return
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # 2. Agent + Host-Runtime ACL
    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        await websocket.close(code=4004, reason="Invalid agent ID")
        return
    agent = await session.get(Agent, agent_uuid)
    if agent is None or agent.agent_runtime != "host":
        await websocket.close(code=4004, reason="Host agent not found")
        return

    # 3. Upstream: Custom Host-PTY-Bridge (siehe docker/host-pty-bridge/) — raw bytes,
    # kein ttyd Frame-Protokoll. Identisches Pattern zu docker-exec-PTY.
    # Per Slug entscheiden wir, ob die Bridge ihren Default (Boss) nutzt
    # oder eine spezifische tmux-Session ueber query-params bekommt (z.B. Hermes).
    slug = agent.name.lower().replace(" ", "-")
    upstream_url = _build_host_upstream_url(slug)
    if upstream_url is None:
        await websocket.close(
            code=4004,
            reason=f"No host tmux mapping for slug '{slug}'",
        )
        return

    # Accept browser connection — kein subprotocol gegenueber Frontend
    # (Frontend nutzt unsere WS, nicht ttyd direkt)
    await websocket.accept()

    import websockets as ws_client

    try:
        async with ws_client.connect(
            upstream_url,
            open_timeout=5,
            ping_interval=None,
        ) as upstream:
            logger.info(
                "Host-terminal proxy connected: agent=%s upstream=%s",
                agent_id, upstream_url,
            )

            # Phase 26 / HERM-13 (F7): byte-counter diagnostics for the
            # write-channel. Without this we cannot tell from logs whether
            # keystrokes reached the upstream bridge — silent drops were the
            # core symptom of F7. Counter is logged every 32 frames + on
            # close to keep log volume bounded.
            sent_bytes_total = 0
            sent_frames = 0
            recv_bytes_total = 0
            recv_frames = 0

            async def client_to_upstream():
                nonlocal sent_bytes_total, sent_frames
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        payload = None
                        if "bytes" in msg and msg["bytes"] is not None:
                            payload = msg["bytes"]
                            await upstream.send(payload)
                        elif "text" in msg and msg["text"] is not None:
                            payload = msg["text"]
                            await upstream.send(payload)
                        if payload is not None:
                            n = len(payload) if isinstance(payload, (bytes, bytearray, str)) else 0
                            sent_bytes_total += n
                            sent_frames += 1
                            # Per-frame info-log so even a single keystroke is
                            # traceable in the backend log; cheap because xterm
                            # write-traffic is low-bandwidth.
                            logger.info(
                                "ws proxy: forwarded %d bytes client->upstream "
                                "(agent=%s slug=%s frame=%d total_bytes=%d)",
                                n, agent_id, slug, sent_frames, sent_bytes_total,
                            )
                except (WebSocketDisconnect, RuntimeError):
                    return
                except Exception as e:
                    logger.warning(
                        "client_to_upstream stopped after %d frames / %d bytes: %s",
                        sent_frames, sent_bytes_total, e,
                    )
                    return

            async def upstream_to_client():
                nonlocal recv_bytes_total, recv_frames
                try:
                    async for frame in upstream:
                        n = len(frame) if isinstance(frame, (bytes, bytearray, str)) else 0
                        recv_bytes_total += n
                        recv_frames += 1
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                        else:
                            await websocket.send_text(frame)
                        # Throttled log: every 64 frames to avoid flooding when
                        # tmux output is verbose.
                        if recv_frames % 64 == 0:
                            logger.info(
                                "ws proxy: forwarded %d bytes upstream->client "
                                "(agent=%s slug=%s frames=%d total_bytes=%d)",
                                n, agent_id, slug, recv_frames, recv_bytes_total,
                            )
                except Exception as e:
                    logger.warning(
                        "upstream_to_client stopped after %d frames / %d bytes: %s",
                        recv_frames, recv_bytes_total, e,
                    )
                    return

            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_upstream()),
                 asyncio.create_task(upstream_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except (OSError, ConnectionRefusedError) as e:
        logger.warning("host-pty-bridge unreachable: %s", e)
        try:
            await websocket.close(code=4503, reason=f"host-pty-bridge unreachable: {e}")
        except Exception:
            pass
    except Exception as e:
        logger.exception("Host-terminal proxy error: %s", e)
        try:
            await websocket.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── Host Agent Lifecycle (Restart/Start/Stop via SSH) ────────────────────────

# Plists die für einen Host-Agent verwaltet werden. Aktuell nur Boss; später
# generisch über agent_runtime + Konvention. Beide werden parallel (un)loaded.
# Pfade leiten sich vom Host-HOME ab (HOME_HOST in Docker) — nie hardcoden.
_HOST_LAUNCH_AGENTS = Path(settings.home_host) / "Library" / "LaunchAgents"
_HOST_AGENT_PLISTS = {
    "boss": [
        str(_HOST_LAUNCH_AGENTS / "com.openclaw.boss.plist"),
        str(_HOST_LAUNCH_AGENTS / "com.openclaw.boss-ttyd.plist"),
    ],
    # Phase 24 / HERM-01: Hermes host-side bridge (siehe ADR-029, Plan 24-04).
    # plist startet scripts/hermes-bridge.py, das wiederum die hermes-worker
    # tmux-Session managed.
    "hermes": [
        str(_HOST_LAUNCH_AGENTS / "com.mc.hermes-bridge.plist"),
    ],
}


async def _ssh_host(command: str, timeout: int = 30) -> str:
    """Führt einen Befehl auf dem Mac-Host via SSH aus.

    Voraussetzung: Backend-Image hat openssh-client; ~/.ssh ist gemounted;
    Mac hat sshd aktiviert (System Settings → Sharing → Remote Login) und
    der id_rsa.pub aus dem Backend liegt in Mac's ~/.ssh/authorized_keys.
    """
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-i", "/home/mcuser/.ssh/id_rsa",
        f"{effective_host_ssh_user()}@host.docker.internal",
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"ssh timeout: {command[:60]}")
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"ssh failed ({proc.returncode}): {stderr.decode()[:200]}",
        )
    return stdout.decode().strip()


async def _host_agent_lifecycle(agent: Agent, action: str) -> dict:
    """Generic restart|start|stop für Host-Agents via launchctl auf dem Mac.

    Hermes-Sonderfall (Phase 26 UAT-Fix): die plist managed nur die hermes-bridge
    HTTP-Server, NICHT die hermes-worker tmux-Session in der Hermes läuft.
    Reines `launchctl kickstart` der plist restartet daher nur die Bridge —
    die tmux-Session (und damit der laufende Hermes-Prozess) bleibt unverändert,
    der Operator sieht „nichts passiert". Für Hermes die bridge HTTP-Endpoints nutzen
    (`POST /restart` killt tmux + spawnt neu, `/stop` killt tmux only).
    """
    slug = agent.name.lower().replace(" ", "-")

    if slug == "hermes":
        bridge_action = {"restart": "/restart", "start": "/start", "stop": "/stop"}.get(action)
        if not bridge_action:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
        # Bridge bindet nur auf 127.0.0.1 (L-C security decision Phase 24).
        # Backend SSH'd zum Host und ruft dort localhost-Bridge.
        out = await _ssh_host(
            f"curl -sS -X POST --max-time 20 http://127.0.0.1:18794{bridge_action} "
            f"-H 'Content-Type: application/json' -d '{{}}' || echo BRIDGE_UNREACHABLE"
        )
        if "BRIDGE_UNREACHABLE" in out or not out.strip():
            raise HTTPException(status_code=503, detail="hermes-bridge unreachable on host")
        return {"ok": True, "action": action, "agent": slug, "bridge_result": out.strip()}

    plists = _HOST_AGENT_PLISTS.get(slug)
    if not plists:
        raise HTTPException(status_code=404, detail=f"No host plists configured for {slug}")

    if action == "stop":
        # Beide unload (parallel ist nicht nötig, sequenziell ist robuster)
        for p in plists:
            await _ssh_host(f"launchctl unload {p} 2>&1 || true")
        return {"ok": True, "action": "stop", "agent": slug}

    if action == "start":
        for p in plists:
            await _ssh_host(f"launchctl load -w {p} 2>&1")
        return {"ok": True, "action": "start", "agent": slug}

    if action == "restart":
        # kickstart -k ist atomar (kill + relaunch); sauberer als unload+load
        labels = [
            f"gui/$(id -u {effective_host_ssh_user()})/{p.split('/')[-1].replace('.plist','')}"
            for p in plists
        ]
        for label in labels:
            await _ssh_host(f"launchctl kickstart -k {label} 2>&1 || true")
        return {"ok": True, "action": "restart", "agent": slug}

    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


async def _resolve_host_agent(agent_id: str, session: AsyncSession) -> Agent:
    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID")
    agent = await session.get(Agent, agent_uuid)
    if agent is None or agent.agent_runtime != "host":
        raise HTTPException(status_code=404, detail="Host agent not found")
    return agent


@router.post("/host-agents/{agent_id}/restart")
async def restart_host_agent(
    agent_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    agent = await _resolve_host_agent(agent_id, session)
    result = await _host_agent_lifecycle(agent, "restart")
    logger.info("Host-agent restart: %s", agent.name)
    return result


@router.post("/host-agents/{agent_id}/start")
async def start_host_agent(
    agent_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    agent = await _resolve_host_agent(agent_id, session)
    result = await _host_agent_lifecycle(agent, "start")
    logger.info("Host-agent start: %s", agent.name)
    return result


@router.post("/host-agents/{agent_id}/stop")
async def stop_host_agent(
    agent_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    agent = await _resolve_host_agent(agent_id, session)
    result = await _host_agent_lifecycle(agent, "stop")
    logger.info("Host-agent stop: %s", agent.name)
    return result


# ── Container Lifecycle (Start/Stop/Restart) ─────────────────────────────────

async def _docker_action(action: str, container_name: str, timeout: int = 30):
    """Führt docker start|stop|restart|inspect auf einem Container aus."""
    proc = await asyncio.create_subprocess_exec(
        "docker", action, container_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"docker {action} timed out")
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"docker {action} fehlgeschlagen: {stderr.decode()}",
        )
    return stdout.decode().strip()


def _container_name_for(agent: Agent) -> str:
    return f"mc-agent-{agent.name.lower().replace(' ', '-')}"


async def _get_container_state(container_name: str) -> str:
    """Gibt den State eines Containers zurück: running, exited, not-found."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "-f", "{{.State.Status}}", container_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        return "unknown"
    if proc.returncode != 0:
        return "not-found"
    return stdout.decode().strip() or "unknown"


@router.get("/docker-sessions/{agent_id}/state")
async def get_container_state(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    state = await _get_container_state(_container_name_for(agent))
    return {"state": state, "container": _container_name_for(agent)}


@router.post("/agents/{agent_id}/restart")
async def restart_agent_container(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Docker-Container des Agents neu starten."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    container_name = _container_name_for(agent)
    await _docker_action("restart", container_name, timeout=30)
    logger.info("Container restarted: %s (agent=%s)", container_name, agent_id)
    return {"ok": True, "container": container_name, "state": "running"}


@router.post("/agents/{agent_id}/start")
async def start_agent_container(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Gestoppten Docker-Container des Agents starten."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    container_name = _container_name_for(agent)
    await _docker_action("start", container_name, timeout=30)
    logger.info("Container started: %s (agent=%s)", container_name, agent_id)
    return {"ok": True, "container": container_name, "state": "running"}


@router.post("/agents/{agent_id}/stop")
async def stop_agent_container(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Laufenden Docker-Container des Agents stoppen."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    container_name = _container_name_for(agent)
    await _docker_action("stop", container_name, timeout=30)
    logger.info("Container stopped: %s (agent=%s)", container_name, agent_id)
    return {"ok": True, "container": container_name, "state": "exited"}


# ── Container Force-Recreate (Image-Update ohne kompletten Stack-Restart) ─────
#
# Use-Case (2026-05-12 Sparky-Session): der Operator hat `scripts/build-agent-images.sh`
# laufen lassen, aber laufende Container nutzen weiter die alte Image-Version
# (mit live-kopiertem mc CLI). Damit das neue Image gezogen wird, muss der
# Container recreated werden — was sonst nur via Shell ging.
#
# Unterschied zu /agents/{id}/restart:
#   restart  = `docker restart` (~5s)  — gleicher Container, alter Code/Image
#   recreate = `docker compose up -d --force-recreate <svc>` (~30-90s)
#              — neuer Container vom AKTUELLEN Image, Volumes/Env neu gemountet
#
# Guard: blockiert, wenn der Agent gerade einen Task bearbeitet (current_task_id).
# Mit ?force=true bypassen — z.B. wenn ein Task hängt und der Container das Heil
# bringt.

@router.post("/agents/{agent_id}/force-recreate")
async def force_recreate_agent_container(
    agent_id: uuid.UUID,
    force: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Docker Container des Agents komplett neu erstellen (zieht neues Image).

    Args:
        force: True überspringt den Busy-Check (Agent bearbeitet Task).
               Default False → 409 wenn busy.

    Returns: {"ok", "container", "state", "duration_seconds"}
    Raises: 404 (agent), 409 (busy), 504 (timeout), 500 (compose error)
    """
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.current_task_id and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent bearbeitet gerade Task {agent.current_task_id}. "
                "Force-Recreate wuerde den Run abbrechen. Mit ?force=true bypassen "
                "oder warten bis der Task done/failed ist."
            ),
        )

    container_name = _container_name_for(agent)
    service_name = container_name  # docker-compose.agents.yml: service_name == container_name

    # HOME_HOST = host-side $HOME (z.B. /Users/<login>), gesetzt in
    # docker-compose.yml backend.environment. docker compose substituiert
    # ${HOME} aus dem Aufrufer-Env in den Volume-Pfaden. Im Backend-Container
    # ist HOME=/home/mcuser → falscher Mount-Pfad. Wir muessen HOME explizit
    # auf den Host-HOME zwingen, damit Volume-Mounts den gleichen Pfad
    # treffen wie start-all.sh.
    # Bug 2026-05-12: ohne diese Korrektur landete Sparky auf
    # ${HOME_HOST}/Workspace/.mc/... statt ${HOME_HOST}/.mc/...
    # Mit Pattern aus docker_agent_sync.py:522-529.
    host_home = os.environ.get("HOME_HOST", os.path.expanduser("~"))
    # Repo root from MC_REPO_PATH (settings) — checkout may have any name.
    repo_root = Path(settings.mc_repo_path)
    compose_main = repo_root / "docker-compose.yml"
    compose_agents = repo_root / "docker" / "docker-compose.agents.yml"
    env_main = repo_root / ".env"
    env_agents = repo_root / "docker" / ".env.agents"
    env_shared = repo_root / "docker" / ".env.shared"

    # Multiple --env-file flags: agents-compose verweist auf ${MC_TOKEN_*},
    # ${OPENAI_API_KEY_*} etc. — ohne diese .env.agents werden alle leer und
    # Agent kommt ohne Token hoch (mc CLI: 'MC_AGENT_TOKEN fehlt').
    compose_args: list[str] = ["compose"]
    for env_file in (env_main, env_agents, env_shared):
        if env_file.is_file():
            compose_args.extend(["--env-file", str(env_file)])
    compose_args.extend([
        "-f", str(compose_main),
        "-f", str(compose_agents),
        "up", "-d", "--force-recreate", "--no-deps",
        service_name,
    ])

    run_env = dict(os.environ)
    run_env["HOME"] = host_home

    started_at = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "docker", *compose_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=run_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="docker compose up timed out after 180s")
    duration = round(time.monotonic() - started_at, 1)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[:500]
        logger.error("force-recreate failed: agent=%s container=%s err=%s", agent_id, container_name, err)
        raise HTTPException(status_code=500, detail=f"docker compose up fehlgeschlagen: {err}")

    state = await _get_container_state(container_name)
    logger.info(
        "Container force-recreated: %s (agent=%s, duration=%.1fs, state=%s)",
        container_name, agent_id, duration, state,
    )
    return {
        "ok": True,
        "container": container_name,
        "state": state,
        "duration_seconds": duration,
    }


# ── Local Memory Files (Claude-Local-Memory inside Agent-Container) ───────────
#
# Use-Case (2026-05-12): Sparky hatte Toxic-Memories in
# /home/agent/.claude/projects/-home-agent/memory/team/*.md die ihn zu
# python-urllib statt mc CLI gepusht haben. MC kannte die nicht (sie sind im
# Container, nicht in der DB), also musste der Operator sie per `docker exec rm`
# loeschen. Dieser Endpoint macht das aus dem UI heraus moeglich.

_LOCAL_MEMORY_DIR = "/home/agent/.claude/projects/-home-agent/memory/team"


def _validate_local_memory_filename(filename: str) -> None:
    """Path-Traversal-Schutz: nur .md-Files, kein Slash, kein Punkt-Prefix."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")
    if not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Nur .md-Dateien erlaubt")
    if filename.startswith("."):
        raise HTTPException(status_code=400, detail="Hidden Files nicht erlaubt")


async def _container_exec(container_name: str, *cmd: str, timeout: int = 10) -> tuple[int, str, str]:
    """docker exec <container> <cmd...>. Returns (rc, stdout, stderr).

    Hinweis: KEIN -T flag — das ist ein docker-compose Flag, nicht docker-cli.
    Mit -T schlaegt docker-cli mit 'unknown shorthand flag T' fehl (verifiziert
    2026-05-12 im Backend-Container).
    """
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container_name, *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"docker exec {' '.join(cmd)} timed out")
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


@router.get("/agents/{agent_id}/local-memory")
async def list_local_memory(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Listet die .md-Memory-Files im Agent-Container.

    Returns: {"directory", "files": [{"name", "size", "content"}]}
    Files: nur *.md im _LOCAL_MEMORY_DIR. Hidden Files + Subdirs ignoriert.
    Inhalt wird bis 50KB pro File mitgegeben (groessere truncated mit Hinweis).
    """
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    container_name = _container_name_for(agent)
    state = await _get_container_state(container_name)
    if state != "running":
        return {"directory": _LOCAL_MEMORY_DIR, "files": [], "container_state": state}

    rc, stdout, _ = await _container_exec(
        container_name, "sh", "-c",
        f"ls -1 {_LOCAL_MEMORY_DIR}/*.md 2>/dev/null || true",
    )
    if rc != 0:
        return {"directory": _LOCAL_MEMORY_DIR, "files": [], "container_state": state}

    files = []
    for path in stdout.strip().splitlines():
        if not path.strip():
            continue
        name = path.rsplit("/", 1)[-1]
        if name.startswith(".") or not name.endswith(".md"):
            continue
        # Read content (max 50KB)
        rc2, content, _ = await _container_exec(
            container_name, "sh", "-c",
            f"head -c 51200 {path}",
        )
        truncated = False
        if rc2 == 0:
            rc3, size_str, _ = await _container_exec(
                container_name, "sh", "-c", f"wc -c < {path}",
            )
            size = int(size_str.strip()) if rc3 == 0 and size_str.strip().isdigit() else len(content)
            if size > 51200:
                truncated = True
        else:
            content = ""
            size = 0
        files.append({
            "name": name,
            "size": size,
            "content": content,
            "truncated": truncated,
        })

    return {"directory": _LOCAL_MEMORY_DIR, "files": files, "container_state": state}


@router.delete("/agents/{agent_id}/local-memory/{filename}")
async def delete_local_memory(
    agent_id: uuid.UUID,
    filename: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Loescht eine einzelne .md-Memory-Datei im Agent-Container.

    Auch MEMORY.md (Index) wird aktualisiert: Zeilen die auf die geloeschte Datei
    verweisen werden via sed entfernt. Falls MEMORY.md selbst geloescht wird,
    bleibt sie unangetastet (Agent wuerde dann eine neue erzeugen).
    """
    _validate_local_memory_filename(filename)
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    container_name = _container_name_for(agent)
    state = await _get_container_state(container_name)
    if state != "running":
        raise HTTPException(
            status_code=409,
            detail=f"Container {container_name} ist nicht running (state: {state})",
        )

    file_path = f"{_LOCAL_MEMORY_DIR}/{filename}"
    rc, _, stderr = await _container_exec(
        container_name, "sh", "-c",
        f"test -f {file_path} && rm -v {file_path}",
    )
    if rc != 0:
        raise HTTPException(
            status_code=404,
            detail=f"Datei {filename} nicht gefunden in {_LOCAL_MEMORY_DIR}",
        )

    # MEMORY.md Index aktualisieren — Zeilen mit dem geloeschten Filename raus.
    # Einfaches grep -v statt sed, weil filename Sonderzeichen enthalten koennte.
    # Wir loeschen MEMORY.md NICHT selbst, falls filename == MEMORY.md.
    if filename != "MEMORY.md":
        # grep -v exit code: 0 = matches found and excluded (output >0 lines)
        #                    1 = ALL lines matched (output 0 lines — also valid!)
        #                    2 = real error
        # We accept 0+1 as success → || [ "$?" = "1" ]. Then mv if tmp exists.
        await _container_exec(
            container_name, "sh", "-c",
            f"if [ -f {_LOCAL_MEMORY_DIR}/MEMORY.md ]; then "
            f"  grep -v '{filename}' {_LOCAL_MEMORY_DIR}/MEMORY.md > {_LOCAL_MEMORY_DIR}/MEMORY.md.tmp; "
            f"  rc=$?; "
            f"  if [ $rc -le 1 ] && [ -f {_LOCAL_MEMORY_DIR}/MEMORY.md.tmp ]; then "
            f"    mv {_LOCAL_MEMORY_DIR}/MEMORY.md.tmp {_LOCAL_MEMORY_DIR}/MEMORY.md; "
            f"  else "
            f"    rm -f {_LOCAL_MEMORY_DIR}/MEMORY.md.tmp; "
            f"  fi; "
            f"fi",
        )

    logger.info("Local memory deleted: %s (agent=%s)", filename, agent_id)
    return {"ok": True, "deleted": filename}
