"""
Agent-Diagnose-Tool fuer Mission Control.

Laeuft im Backend-Container und gibt einen strukturierten Bericht
ueber den aktuellen Zustand eines Agents aus.

Usage:
  docker compose exec -T backend python3 tools/agent_check.py <agent_name>
  docker compose exec -T backend python3 tools/agent_check.py --all
"""

import asyncio
import json
import sys
from datetime import datetime, timezone


async def check_agent(agent_name: str) -> dict:
    """Vollstaendige Diagnose eines Agents."""
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.database import engine
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from app.services.openclaw_rpc import rpc

    report: dict = {
        "agent": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "unknown",
        "problems": [],
    }

    # ── 1. Agent aus DB laden ──
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(
            select(Agent).where(Agent.name.ilike(f"%{agent_name}%"))  # type: ignore
        )
        agent = result.first()
        if not agent:
            report["status"] = "NOT_FOUND"
            report["problems"].append(f"Agent '{agent_name}' nicht in DB gefunden")
            return report

        report["agent_info"] = {
            "id": str(agent.id),
            "name": agent.name,
            "role": agent.role,
            "gateway_id": agent.gateway_agent_id,
            "status": agent.status,
            "model": agent.model,
            "workspace": agent.workspace_path,
            "provision_status": agent.provision_status,
        }

        # ── 2. Aktive Tasks laden ──
        task_result = await session.exec(
            select(Task).where(
                Task.assigned_agent_id == agent.id,
                Task.status.in_(["in_progress", "inbox", "review", "blocked"]),  # type: ignore
            )
        )
        tasks = task_result.all()

        report["tasks"] = []
        for t in tasks:
            task_info: dict = {
                "id": str(t.id),
                "title": t.title,
                "status": t.status,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "dispatched_at": t.dispatched_at.isoformat() if t.dispatched_at else None,
                "ack_at": t.ack_at.isoformat() if t.ack_at else None,
            }

            # Projekt-Info laden
            if t.project_id:
                from app.models.board import Project
                proj = await session.get(Project, t.project_id)
                if proj:
                    task_info["project"] = proj.name
                    task_info["project_workspace"] = proj.workspace_path

            # Letzter Kommentar
            comment_result = await session.exec(
                select(TaskComment)
                .where(TaskComment.task_id == t.id)
                .order_by(TaskComment.created_at.desc())  # type: ignore
                .limit(1)
            )
            last_comment = comment_result.first()
            if last_comment:
                task_info["last_comment"] = {
                    "type": last_comment.comment_type,
                    "time": last_comment.created_at.isoformat(),
                    "preview": last_comment.content[:200],
                }

            report["tasks"].append(task_info)

        # ── 3. Gateway-Session pruefen ──
        if not agent.gateway_agent_id:
            report["session"] = {"status": "NO_GATEWAY_ID"}
            report["problems"].append("Agent hat keine gateway_agent_id")
            return report

        try:
            await rpc.connect()
            raw_sessions = await rpc.sessions_list(limit=100)
            sessions = raw_sessions if isinstance(raw_sessions, list) else raw_sessions.get("sessions", [])

            # Sessions fuer diesen Agent finden
            gw_id = agent.gateway_agent_id
            agent_sessions = [s for s in sessions if s.get("key", "").startswith(f"agent:{gw_id}")]

            if not agent_sessions:
                report["session"] = {"status": "NO_SESSION"}
                report["problems"].append("Keine aktive Gateway-Session gefunden")
            else:
                main_session = None
                for s in agent_sessions:
                    if s.get("key", "").endswith(":main"):
                        main_session = s
                        break
                if not main_session:
                    main_session = agent_sessions[0]

                session_key = main_session.get("key", "")
                report["session"] = {
                    "key": session_key,
                    "sessions_count": len(agent_sessions),
                    "all_keys": [s.get("key", "") for s in agent_sessions],
                }

                # ── 4. Chat-History pruefen ──
                try:
                    history_result = await rpc.request(
                        "chat.history", {"sessionKey": session_key, "limit": 50}
                    )
                    messages = history_result.get("messages", []) if isinstance(history_result, dict) else []
                    report["session"]["message_count"] = len(messages)

                    if not messages:
                        report["session"]["last_activity"] = "EMPTY"
                        if report.get("tasks"):
                            report["problems"].append("Session leer aber hat aktive Tasks!")
                    else:
                        # Letzte 3 Messages zusammenfassen
                        last_messages = []
                        for msg in messages[-3:]:
                            role = msg.get("role", "?")
                            content = msg.get("content", "")

                            if isinstance(content, list):
                                texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                                tool_uses = [b for b in content if b.get("type") == "tool_use"]
                                tool_results = [b for b in content if b.get("type") == "tool_result"]
                                if texts:
                                    content = "\n".join(texts)
                                elif tool_uses:
                                    content = ", ".join(
                                        f"[tool: {t.get('name', '?')}]" for t in tool_uses
                                    )
                                elif tool_results:
                                    raw = tool_results[0].get("content", "")
                                    content = f"[result] {str(raw)[:200]}"
                                else:
                                    # Gemischte Bloecke (z.B. tool_use + text + thinking)
                                    parts = []
                                    for b in content:
                                        btype = b.get("type", "?")
                                        if btype == "text" and b.get("text"):
                                            parts.append(b["text"][:150])
                                        elif btype == "tool_use":
                                            parts.append(f"[tool: {b.get('name', '?')}]")
                                        elif btype == "tool_result":
                                            parts.append(f"[result: {str(b.get('content',''))[:100]}]")
                                        elif btype == "thinking":
                                            parts.append("[thinking...]")
                                        else:
                                            parts.append(f"[{btype}]")
                                    content = " | ".join(parts) if parts else "[empty]"

                            if len(content) > 300:
                                content = content[:300] + "..."

                            last_messages.append({"role": role, "content": content})

                        report["session"]["last_messages"] = last_messages

                        # Leere Assistant-Antwort erkennen
                        last_msg = messages[-1]
                        if last_msg.get("role") == "assistant":
                            last_content = last_msg.get("content", "")
                            if isinstance(last_content, str) and not last_content.strip():
                                report["problems"].append(
                                    "Letzte Antwort ist LEER (Thinking-Only Bug?)"
                                )
                            elif isinstance(last_content, list):
                                has_text = any(
                                    b.get("type") == "text" and b.get("text", "").strip()
                                    for b in last_content
                                )
                                has_tool_use = any(
                                    b.get("type") == "tool_use" for b in last_content
                                )
                                has_thinking = any(
                                    b.get("type") == "thinking" for b in last_content
                                )
                                if not has_text and not has_tool_use:
                                    if has_thinking:
                                        report["problems"].append(
                                            "Letzte Antwort nur Thinking (kein Text, kein Tool-Use)"
                                        )
                                    else:
                                        report["problems"].append(
                                        "Letzte Antwort hat keinen Text-Content"
                                    )

                        # Pruefen ob letzte Message ein tool_result ist (Agent wartet auf nichts)
                        if messages[-1].get("role") == "user":
                            # Letzte Message ist von User/System — Agent sollte antworten
                            pass

                except Exception as e:
                    report["session"]["history_error"] = str(e)
                    report["problems"].append(f"Chat-History Fehler: {e}")

        except Exception as e:
            report["session"] = {"status": "RPC_ERROR", "error": str(e)}
            report["problems"].append(f"RPC-Verbindung fehlgeschlagen: {e}")

    # ── 5. Problem-Zusammenfassung ──
    if not report["problems"]:
        if report.get("tasks"):
            in_progress = [t for t in report["tasks"] if t["status"] == "in_progress"]
            if in_progress and report.get("session", {}).get("message_count", 0) > 0:
                report["status"] = "WORKING"
            elif in_progress:
                report["status"] = "IDLE"
                report["problems"].append("Hat in_progress Task aber keine Session-Aktivitaet")
            else:
                report["status"] = "IDLE"
        else:
            report["status"] = "IDLE"
    else:
        report["status"] = "PROBLEM"

    return report


def format_report(report: dict) -> str:
    """Menschenlesbarer Bericht."""
    lines = []
    status_emoji = {
        "WORKING": "[OK]",
        "IDLE": "[IDLE]",
        "PROBLEM": "[!!!]",
        "NOT_FOUND": "[ERR]",
    }
    emoji = status_emoji.get(report["status"], "[?]")
    lines.append(f"{emoji} Agent: {report['agent']} — Status: {report['status']}")
    lines.append("")

    # Agent-Info
    info = report.get("agent_info", {})
    if info:
        lines.append(f"  Rolle: {info.get('role', '?')} | Modell: {info.get('model', '?')}")
        lines.append(f"  Gateway: {info.get('gateway_id', '?')} | MC-Status: {info.get('status', '?')}")
        if info.get("workspace"):
            lines.append(f"  Workspace: {info['workspace']}")
        lines.append("")

    # Tasks
    tasks = report.get("tasks", [])
    if tasks:
        lines.append(f"  Tasks ({len(tasks)}):")
        for t in tasks:
            status_mark = {"in_progress": ">>", "inbox": "..", "review": "??", "blocked": "!!"}
            mark = status_mark.get(t["status"], "  ")
            lines.append(f"    {mark} [{t['status']}] {t['title']}")
            if t.get("project"):
                proj_ws = t.get("project_workspace", "")
                ws_info = f" @ {proj_ws}" if proj_ws else ""
                lines.append(f"       Projekt: {t['project']}{ws_info}")
            if t.get("last_comment"):
                c = t["last_comment"]
                lines.append(f"       Letzter Kommentar ({c['type']}): {c['preview'][:100]}")
        lines.append("")

    # Session
    sess = report.get("session", {})
    if sess:
        if sess.get("status") in ("NO_SESSION", "NO_GATEWAY_ID", "RPC_ERROR"):
            lines.append(f"  Session: {sess.get('status')} {sess.get('error', '')}")
        else:
            lines.append(f"  Session: {sess.get('key', '?')} ({sess.get('message_count', '?')} messages)")
            if sess.get("last_messages"):
                lines.append("  Letzte Messages:")
                for msg in sess["last_messages"]:
                    role_label = "  AGENT" if msg["role"] == "assistant" else "  USER "
                    content_preview = msg["content"].replace("\n", " ")[:150]
                    lines.append(f"    {role_label}: {content_preview}")
        lines.append("")

    # Probleme
    problems = report.get("problems", [])
    if problems:
        lines.append("  PROBLEME:")
        for p in problems:
            lines.append(f"    - {p}")
    else:
        lines.append("  Keine Probleme erkannt.")

    return "\n".join(lines)


async def check_all() -> list[dict]:
    """Alle Agents mit Gateway-ID pruefen."""
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.database import engine
    from app.models.agent import Agent

    async with AsyncSession(engine) as session:
        result = await session.exec(
            select(Agent).where(Agent.gateway_agent_id.isnot(None))  # type: ignore
        )
        agents = result.all()

    reports = []
    for agent in agents:
        report = await check_agent(agent.name)
        reports.append(report)
    return reports


async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 tools/agent_check.py <agent_name|--all> [--json]")
        sys.exit(1)

    use_json = "--json" in sys.argv
    target = sys.argv[1]

    if target == "--all":
        reports = await check_all()
        if use_json:
            print(json.dumps(reports, indent=2, default=str))
        else:
            for report in reports:
                print(format_report(report))
                print("=" * 60)
    else:
        report = await check_agent(target)
        if use_json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(format_report(report))


if __name__ == "__main__":
    asyncio.run(main())
