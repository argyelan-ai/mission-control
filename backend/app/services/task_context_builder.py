"""
Task Context Builder — Dispatch-Context Assembly extracted from dispatch.py (REF-01).

All functions are side-effect free reads against AsyncSession.
asyncio.gather() pattern preserved verbatim from the original location.
Phase-4 Boundary: keine Routes, keine RPC, keine writes — pure read service.

Quelle: backend/app/services/dispatch.py (Phase 4 REF-01 Bottom-Up Extraction).
Re-Exports in dispatch.py erhalten alle bestehenden Importer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Project
from app.models.meeting import AgentMeeting
from app.models.memory import BoardMemory
from app.models.deliverable import TaskDeliverable
from app.models.tag import Tag, TagAssignment
from app.models.task import Task, TaskComment, TaskDependency

logger = logging.getLogger(__name__)


# ── REF-01 Step 3: Git Workspace Setup (extracted from dispatch.auto_dispatch_task) ──
# Verbatim relocation of the git-workspace + worktree provisioning block that lived
# inline in dispatch.auto_dispatch_task (lines 419-569 before extraction).
#
# Behavior contract preserved:
#   - Project with github_repo_url + agent.workspace_path:
#       * Pre-check workspace_path against backend mount-roots (fail-fast on
#         non-mounted paths to avoid cryptic PermissionErrors). Failure path
#         posts a TaskComment(comment_type="blocker"), sets task.status="blocked",
#         applies terminal-unassign, commits, and returns False (caller MUST
#         abort dispatch — same as original `return` in auto_dispatch_task).
#       * Success path tries worktree-isolation; on worktree failure falls back
#         to a branch checkout in the main repo. Sets task.workspace_path.
#   - Project absent + agent.workspace_path set: ad-hoc git workspace path.
#       * Worktree → branch fallback identical to project path.
#       * On any exception logs WARNING but does NOT block dispatch (returns True).
#   - Otherwise: no-op (returns True — caller continues with non-code workspace).
#
# Pattern S2 (lazy local imports) preserved for git_service + apply_terminal_unassign
# to avoid module-load cycles with dispatch.py / task_lifecycle.py.
async def setup_git_workspace_for_dispatch(
    task: "Task",
    agent: "Agent",
    session: AsyncSession,
) -> bool:
    """Provision the git workspace (worktree or branch) for a task before dispatch.

    Returns True if dispatch should continue, False if the task was blocked
    (TaskComment + terminal-unassign already committed; caller MUST `return`).
    """
    # Lazy import: dispatch.py contains is_backend_writable_path + _BACKEND_MOUNTED_ROOTS
    # (Pitfall D inseparable triple stays in dispatch.py per CONTEXT D-07 + ADR-025).
    from app.services.dispatch import is_backend_writable_path, _BACKEND_MOUNTED_ROOTS

    git_branch = None
    git_project_dir = None
    if task.project_id:
        try:
            from app.services.git_service import git_service, slugify_project
            project = await session.get(Project, task.project_id)
            if project and project.github_repo_url and agent.workspace_path:
                # Pre-Check: workspace_path muss in einem Backend-gemounteten
                # Volume liegen. Ohne Check wirft mkdir/clone einen kryptischen
                # PermissionError und der Operator bekommt eine vage Blocker-Nachricht.
                # Siehe Incident 2026-04-23 (Boss DNA-Task).
                if not is_backend_writable_path(agent.workspace_path):
                    raise RuntimeError(
                        f"Agent '{agent.name}' hat workspace_path="
                        f"'{agent.workspace_path}' — dieser Pfad ist NICHT "
                        f"in den Backend-Container-Mounts verfuegbar. "
                        f"Gueltige Prefixe: {', '.join(_BACKEND_MOUNTED_ROOTS)}. "
                        f"Fix: `UPDATE agents SET workspace_path="
                        f"'~/.mc/workspaces/{agent.name.lower()}' "
                        f"WHERE name='{agent.name}'` (Standard-Pattern)."
                    )
                project_slug = slugify_project(project.name)
                main_repo = await git_service.ensure_workspace(
                    agent.workspace_path,
                    project.github_repo_url,
                    project_slug,
                )
                task_slug = slugify_project(task.title)
                # Worktree-Versuch: isolierter Pfad pro Task
                try:
                    worktree_path = await git_service.create_task_worktree(
                        main_repo, task_slug,
                        branch_name=f"task/{task_slug}",
                    )
                    git_project_dir = worktree_path
                    task.workspace_path = worktree_path
                    logger.info("Task %s: Worktree erstellt: %s", task.id, worktree_path)
                except Exception as wt_err:
                    # Fallback: Branch im Hauptrepo (wie bisher)
                    logger.warning("Worktree fehlgeschlagen, Fallback auf Branch: %s", wt_err)
                    git_project_dir = main_repo
                    git_branch = await git_service.create_task_branch(
                        main_repo, task_slug,
                    )
                    task.workspace_path = main_repo
                await git_service.setup_git_identity(
                    git_project_dir, agent.name,
                )
                session.add(task)
                await session.commit()
        except Exception as e:
            # Bei Project mit github_repo_url: Git-Setup MUSS erfolgreich sein.
            # Silent-Fallback (dispatch ohne workspace) hat am 2026-04-19
            # dazu gefuehrt, dass FreeCode auf einem fremden Repo committet
            # hat. Jetzt: Task auf blocked, kein Dispatch.
            logger.error(
                "Git workspace setup failed for task %s: %s",
                task.id, e,
            )
            # Konkrete Error-Klassifikation fuer saubere Operator-Eskalation.
            # Unterscheiden: workspace_path mount-Problem vs. echte Git-Errors.
            _err_name = type(e).__name__
            _err_msg = str(e)
            if _err_name == "RuntimeError" and "Backend-Container-Mounts" in _err_msg:
                blocker_text = (
                    "**Workspace-Setup fehlgeschlagen** — Dispatch abgebrochen.\n\n"
                    f"**Root-Cause:** Agent-Workspace-Pfad nicht vom Backend-Container erreichbar.\n\n"
                    f"**Detail:** {_err_msg}\n\n"
                    "**Question for @Operator** — Soll ich den workspace_path auf das Standard-Pattern "
                    "setzen? Der `UPDATE`-Befehl oben ist der empfohlene Fix."
                )
            elif "Permission denied" in _err_msg:
                blocker_text = (
                    "**Workspace-Setup fehlgeschlagen** — Dispatch abgebrochen.\n\n"
                    f"**Fehler:** `{_err_name}: {_err_msg}`\n\n"
                    f"Agent-Workspace: `{agent.workspace_path}`\n"
                    f"Gueltige Backend-Mount-Prefixe: {', '.join(_BACKEND_MOUNTED_ROOTS)}\n\n"
                    "Der workspace_path liegt moeglicherweise ausserhalb der gemounteten "
                    "Volumes — Backend-Container kann dort nicht schreiben. Pruefen via "
                    "`docker exec mission-control-backend-1 ls -la <path>`.\n\n"
                    "**Question for @Operator** — Workspace umziehen oder Mount nachziehen?"
                )
            else:
                blocker_text = (
                    "**Workspace-Setup fehlgeschlagen** — Dispatch abgebrochen.\n\n"
                    f"**Fehler:** `{_err_name}: {_err_msg}`\n\n"
                    "Project hat github_repo_url gesetzt, aber clone/worktree "
                    "schlug fehl. Moegliche Ursachen:\n"
                    "- Workspace-Dir existiert mit fremdem Inhalt (Cleanup noetig)\n"
                    "- Project.github_repo_url falsch konfiguriert\n"
                    "- GH_TOKEN fehlt Rechte\n\n"
                    "**Question for @Operator** — Cleanup oder Repo-URL korrigieren?"
                )
            from app.models.task import TaskComment
            blocker = TaskComment(
                task_id=task.id,
                author_type="system",
                comment_type="blocker",
                content=blocker_text,
            )
            task.status = "blocked"
            # Auto-Unassign: Workspace-Setup-Fehler ist Operator-Approval-Wait,
            # kein Callback-Wait. Verhindert Cancel-Schleife.
            from app.services.task_lifecycle import apply_terminal_unassign
            await apply_terminal_unassign(session, task, "blocked")
            session.add(task)
            session.add(blocker)
            await session.commit()
            return False
    elif agent.workspace_path:
        # Ad-hoc Task ohne Projekt → eigenes Repo oder mc-workspace
        try:
            from app.services.git_service import (
                ADHOC_REPO,
                GITHUB_OWNER,
                git_service,
                require_github_owner,
                slugify_project,
            )

            if task.use_separate_repo:
                # Dediziertes Repo für diesen Task
                repo_url = await git_service.ensure_task_repo(task.title, str(task.id))
                repo_slug = repo_url.rstrip(".git").split("/")[-1]
            else:
                # Geteiltes mc-workspace Repo (bisheriges Verhalten).
                # Fail loud statt stiller Warning-Fallback bei fehlendem Owner.
                require_github_owner()
                repo_url = f"https://github.com/{GITHUB_OWNER}/{ADHOC_REPO}.git"
                repo_slug = ADHOC_REPO
                await git_service.ensure_adhoc_repo()

            main_repo = await git_service.ensure_workspace(
                agent.workspace_path, repo_url, repo_slug,
            )
            task_slug = slugify_project(task.title)
            try:
                worktree_path = await git_service.create_task_worktree(
                    main_repo, task_slug,
                    branch_name=f"task/{task_slug}",
                )
                git_project_dir = worktree_path
                task.workspace_path = worktree_path
            except Exception:
                git_project_dir = main_repo
                git_branch = await git_service.create_task_branch(
                    main_repo, task_slug,
                )
                task.workspace_path = main_repo
            await git_service.setup_git_identity(
                git_project_dir, agent.name,
            )
            session.add(task)
            await session.commit()
        except Exception as e:
            logger.warning("Ad-hoc git workspace setup fehlgeschlagen: %s", e)

    return True


# ── T-1 Phase C: Non-Code-Task Workspace ────────────────────────────────
async def _ensure_task_workspace(
    task_id: uuid.UUID,
    project: "Project | None",
    agent_workspace: str | None,
) -> str | None:
    """Stellt sicher dass ein Task ein eigenes Arbeitsverzeichnis hat.

    Wird fuer Non-Code-Tasks aufgerufen (kein GitHub-Repo).
    Git-Worktrees werden vom GitService separat behandelt.

    Returns:
        Pfad zum Workspace-Verzeichnis, oder None wenn kein agent_workspace bekannt
        oder das Verzeichnis nicht erstellt werden konnte (PermissionError, OSError).
        Im Fehlerfall darf Auto-Dispatch nicht crashen — Workspace ist Nice-to-Have.
    """
    # Kein extra Workspace noetig wenn Git-Repo vorhanden (GitService erstellt Worktree)
    if project and project.github_repo_url:
        return None

    # Basis-Verzeichnis bestimmen
    if agent_workspace:
        base = os.path.join(agent_workspace, "_tasks", str(task_id))
    else:
        base = os.path.join("/tmp", "mc_tasks", str(task_id))

    try:
        os.makedirs(base, exist_ok=True)
        output_dir = os.path.join(base, "output")
        os.makedirs(output_dir, exist_ok=True)
    except (PermissionError, OSError) as exc:
        logger.warning(
            "Workspace-Erstellung fehlgeschlagen für Task %s (%s) — Dispatch läuft ohne lokales Workspace weiter: %s",
            task_id, base, exc,
        )
        return None
    return base


# ── DispatchContext ─────────────────────────────────────────────────────
@dataclass
class DispatchContext:
    """Buendelt alle vorgeladenen Daten fuer eine Dispatch-Message.

    Wird von _load_dispatch_context() via asyncio.gather() befuellt,
    sodass die 6+ DB-Queries parallel laufen statt sequentiell (N+1 Fix).
    """
    memory_context: str = ""
    lessons_context: str = ""
    agent_lessons_context: str = ""
    relevant_lessons_context: str = ""
    semantic_memory_context: str = ""  # Phase A (2026-04-11): Qdrant Vektor-Treffer
    intelligence_context: str = ""
    feedback_context: str = ""
    meeting_context: str = ""
    credentials_text: str = ""
    dependency_context: str = ""
    project: Project | None = None
    project_tags: list[str] = field(default_factory=list)
    team_agents: list[Agent] = field(default_factory=list)
    child_tasks: list[Task] = field(default_factory=list)


async def _load_dispatch_context(
    task: Task,
    agent: Agent,
    session: AsyncSession,
) -> DispatchContext:
    """Laedt alle Kontext-Daten parallel via asyncio.gather().

    Jede Query ist best-effort — bei Fehler wird der Wert leer gelassen.
    """
    ctx = DispatchContext()

    async def _load_memory() -> str:
        try:
            mem_result = await session.exec(
                select(BoardMemory)
                .where(
                    BoardMemory.board_id == task.board_id,
                    BoardMemory.is_pinned == True,  # noqa: E712
                )
                .limit(3)
            )
            memories = mem_result.all()
            if not memories:
                mem_result2 = await session.exec(
                    select(BoardMemory)
                    .where(BoardMemory.board_id == task.board_id)
                    .order_by(BoardMemory.created_at.desc())  # type: ignore[union-attr]
                    .limit(3)
                )
                memories = mem_result2.all()
            if memories:
                lines = []
                for m in memories:
                    title = m.title or m.memory_type
                    preview = m.content[:150].replace("\n", " ")
                    lines.append(f"- [{title}] {preview}")
                return "\n".join(lines)
        except Exception:
            pass
        return ""

    async def _load_lessons() -> str:
        try:
            from app.services.auto_memory import fetch_recent_lessons
            recent = await fetch_recent_lessons(session, task.board_id, limit=3)
            if recent:
                lines = []
                for les in recent:
                    title = les.title or "Lesson"
                    preview = les.content[:120].replace("\n", " ")
                    lines.append(f"- [{title}] {preview}")
                return "\n".join(lines)
        except Exception:
            pass
        return ""

    async def _load_agent_lessons() -> str:
        try:
            from app.services.auto_memory import fetch_agent_lessons
            lessons = await fetch_agent_lessons(session, agent.id, limit=3)
            if lessons:
                lines = []
                for al in lessons:
                    title = al.title or "Lesson"
                    preview = al.content[:120].replace("\n", " ")
                    lines.append(f"- [{title}] {preview}")
                return "\n".join(lines)
        except Exception:
            pass
        return ""

    async def _load_relevant_lessons() -> str:
        try:
            from app.services.auto_memory import fetch_relevant_lessons
            relevant = await fetch_relevant_lessons(
                session, task.title, task.description, task.board_id, limit=3
            )
            if relevant:
                lines = []
                for rl in relevant:
                    title = rl.title or "Lesson"
                    preview = rl.content[:120].replace("\n", " ")
                    lines.append(f"- [{title}] {preview}")
                return "\n".join(lines)
        except Exception:
            pass
        return ""

    async def _load_semantic_memory() -> str:
        """Laedt via Qdrant-Vektorsuche die relevantesten Semantic + Agent Memories
        fuer den aktuellen Task (Phase A, 2026-04-11).

        Kombiniert Task-Titel + Description als Query, holt Top-3 aus semantic
        (board-scoped) und Top-3 aus agent (privat fuer den Ziel-Agent).
        Fail-soft: leere Rueckgabe bei Spark/Qdrant-Problemen.
        """
        try:
            query_text = (task.title or "") + "\n" + (task.description or "")
            query_text = query_text.strip()
            if len(query_text) < 5:
                return ""
            from app.services.memory_query import run_memory_query

            result = await run_memory_query(
                session=session,
                query=query_text[:1500],  # Top-1500 Chars reichen fuer Query
                layers=["semantic", "agent"],
                top_k=3,
                agent_id=str(agent.id) if agent else None,
                board_id=str(task.board_id) if task.board_id else None,
            )
            lines: list[str] = []
            sem = result.get("results", {}).get("semantic", [])
            if sem:
                lines.append("**Semantic (wiederverwendbares Wissen):**")
                for hit in sem[:3]:
                    title = hit.get("title", "").strip() or hit.get("memory_type", "memory")
                    preview = (hit.get("content_preview", "") or "")[:200].replace("\n", " ")
                    score = hit.get("score", 0)
                    lines.append(f"- [{title}] (~{score:.2f}) {preview}")
            ag = result.get("results", {}).get("agent", [])
            if ag:
                lines.append("**Deine Agent-Lessons:**")
                for hit in ag[:3]:
                    title = hit.get("title", "").strip() or "lesson"
                    preview = (hit.get("content_preview", "") or "")[:200].replace("\n", " ")
                    score = hit.get("score", 0)
                    lines.append(f"- [{title}] (~{score:.2f}) {preview}")
            if result.get("fallback"):
                lines.insert(0, "_(Keyword-Fallback — Spark/Qdrant nicht erreichbar)_")
            return "\n".join(lines)
        except Exception:
            return ""

    async def _load_intelligence() -> str:
        try:
            from app.services.intelligence import fetch_recent_insights
            insights = await fetch_recent_insights(session, limit=2)
            if insights:
                lines = []
                for ins in insights:
                    title = ins.title or "Insight"
                    preview = ins.content[:150].replace("\n", " ")
                    lines.append(f"- [{title}] {preview}")
                return "\n".join(lines)
        except Exception:
            pass
        return ""

    async def _load_feedback() -> str:
        try:
            feedback_result = await session.exec(
                select(TaskComment)
                .where(
                    TaskComment.task_id == task.id,
                    TaskComment.comment_type == "feedback",
                    TaskComment.author_type == "agent",
                )
                .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
                .limit(3)
            )
            feedbacks = feedback_result.all()
            if feedbacks:
                fb_lines = []
                for fb in feedbacks:
                    author_name = "Reviewer"
                    if fb.author_agent_id:
                        author_agent = await session.get(Agent, fb.author_agent_id)
                        if author_agent:
                            author_name = author_agent.name
                    content = fb.content[:600]
                    if len(fb.content) > 600:
                        content += "\n[...gekuerzt]"
                    fb_lines.append(f"**{author_name}** ({fb.created_at.strftime('%H:%M')}):\n{content}")
                return "\n\n".join(fb_lines)
        except Exception:
            pass
        return ""

    async def _load_project() -> tuple[Project | None, list[str]]:
        if not task.project_id:
            return None, []
        try:
            project = await session.get(Project, task.project_id)
            if not project:
                return None, []
            tag_result = await session.exec(
                select(Tag)
                .join(TagAssignment, TagAssignment.tag_id == Tag.id)
                .where(TagAssignment.project_id == task.project_id)
            )
            tags = [t.name for t in tag_result.all()]
            return project, tags
        except Exception:
            return None, []

    async def _load_team() -> list[Agent]:
        if not agent.is_board_lead:
            return []
        try:
            result = await session.exec(
                select(Agent).where(
                    Agent.board_id == task.board_id,
                    Agent.id != agent.id,
                )
            )
            return list(result.all())
        except Exception:
            return []

    async def _load_child_tasks() -> list[Task]:
        """Aktive Child-Tasks dieses Tasks laden (fuer Board Lead Subtask-Uebersicht)."""
        if not agent.is_board_lead:
            return []
        try:
            result = await session.exec(
                select(Task).where(Task.parent_task_id == task.id)
            )
            return list(result.all())
        except Exception:
            return []

    async def _load_meeting_insights() -> str:
        """Letzte 2 Meeting-Summaries als Kontext laden."""
        try:
            result = await session.exec(
                select(AgentMeeting)
                .where(
                    AgentMeeting.board_id == task.board_id,
                    AgentMeeting.status == "completed",
                )
                .order_by(AgentMeeting.completed_at.desc())
                .limit(2)
            )
            meetings = result.all()
            if not meetings:
                return ""
            parts = []
            for m in meetings:
                date_str = m.completed_at.strftime("%d.%m.%Y") if m.completed_at else "?"
                parts.append(f"**{m.title}** ({date_str})")
                if m.summary:
                    # Maximal 300 Zeichen pro Meeting-Summary
                    parts.append(m.summary[:300])
                if m.decisions:
                    for d in m.decisions[:3]:
                        text = d.get("text", str(d)) if isinstance(d, dict) else str(d)
                        parts.append(f"  - {text}")
                parts.append("")
            return "\n".join(parts).strip()
        except Exception:
            return ""

    async def _load_dependencies() -> str:
        """Workspace-Pfade und Outputs der Vorgaenger-Tasks laden."""
        try:
            dep_result = await session.exec(
                select(TaskDependency).where(TaskDependency.task_id == task.id)
            )
            deps = dep_result.all()
            if not deps:
                return ""
            parts = []
            for dep in deps:
                dep_task = await session.get(Task, dep.depends_on_task_id)
                if not dep_task:
                    continue
                section = [f"**{dep_task.title}** (status: {dep_task.status})"]
                if dep_task.workspace_path:
                    section.append(f"- Workspace: `{dep_task.workspace_path}`")
                # Deliverables des Vorgaengers
                deliv_result = await session.exec(
                    select(TaskDeliverable).where(TaskDeliverable.task_id == dep_task.id)
                )
                deliverables = deliv_result.all()
                for d in deliverables:
                    path_hint = f" → `{d.path}`" if d.path else ""
                    section.append(f"- Deliverable: {d.title}{path_hint}")
                # Letzter Progress-Kommentar als Evidence
                last_comment_result = await session.exec(
                    select(TaskComment)
                    .where(
                        TaskComment.task_id == dep_task.id,
                        TaskComment.comment_type.in_(["progress", "checkpoint"]),
                    )
                    .order_by(TaskComment.created_at.desc())
                    .limit(1)
                )
                last_comment = last_comment_result.first()
                if last_comment:
                    section.append(f"- Letzter Stand: {last_comment.content[:300]}")
                parts.append("\n".join(section))
            return "\n\n".join(parts) if parts else ""
        except Exception:
            return ""

    # Alle Queries parallel ausfuehren
    results = await asyncio.gather(
        _load_memory(),
        _load_lessons(),
        _load_agent_lessons(),
        _load_relevant_lessons(),
        _load_semantic_memory(),  # Phase A
        _load_intelligence(),
        _load_feedback(),
        _load_project(),
        _load_team(),
        _load_meeting_insights(),
        _load_child_tasks(),
        _load_dependencies(),
        return_exceptions=True,
    )

    # Ergebnisse zuweisen (Fehler werden als leere Werte behandelt)
    ctx.memory_context = results[0] if isinstance(results[0], str) else ""
    ctx.lessons_context = results[1] if isinstance(results[1], str) else ""
    ctx.agent_lessons_context = results[2] if isinstance(results[2], str) else ""
    ctx.relevant_lessons_context = results[3] if isinstance(results[3], str) else ""
    ctx.semantic_memory_context = results[4] if isinstance(results[4], str) else ""
    ctx.intelligence_context = results[5] if isinstance(results[5], str) else ""
    ctx.feedback_context = results[6] if isinstance(results[6], str) else ""

    if isinstance(results[7], tuple):
        ctx.project, ctx.project_tags = results[7]
    if isinstance(results[8], list):
        ctx.team_agents = results[8]
    ctx.meeting_context = results[9] if isinstance(results[9], str) else ""
    if isinstance(results[10], list):
        ctx.child_tasks = results[10]
    ctx.dependency_context = results[11] if isinstance(results[11], str) else ""

    # Vault-Credential aufloesen (sequentiell, da credential_id selten gesetzt ist).
    # _inherited_credential_id wird vom _build_dispatch_message Inheritance-Block
    # gesetzt, falls Parent ein credential_id hat und der Subtask keins.
    effective_credential_id = task.credential_id or getattr(task, "_inherited_credential_id", None)
    if effective_credential_id:
        try:
            from app.models.credential import Credential
            from app.services.encryption import safe_decrypt
            import json as _json
            credential = await session.get(Credential, effective_credential_id)
            if credential:
                decrypted = safe_decrypt(credential.encrypted_data)
                if decrypted:
                    data = _json.loads(decrypted)
                    parts = []
                    if credential.url:
                        parts.append(f"URL: {credential.url}")
                    if credential.credential_type == "login":
                        parts.append(f"Username: {data.get('username', '')}")
                        parts.append(f"Password: {data.get('password', '')}")
                    elif credential.credential_type == "token":
                        parts.append(f"Token: {data.get('token', '')}")
                    else:
                        parts.append(data.get("content", ""))
                    ctx.credentials_text = "\n".join(parts)
        except Exception:
            pass

    return ctx


async def get_last_checkpoint(session: AsyncSession, task_id: uuid.UUID) -> str | None:
    """Letzten Checkpoint-Kommentar eines Tasks laden (fuer Recovery-Kontext)."""
    result = await session.exec(
        select(TaskComment)
        .where(
            TaskComment.task_id == task_id,
            TaskComment.comment_type == "checkpoint",
        )
        .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    checkpoint = result.first()
    return checkpoint.content if checkpoint else None


async def build_recovery_context(session: AsyncSession, task: Task) -> str | None:
    """Compact recovery snippet — Workstream A4.

    Source of truth is TaskChecklistItem (progress) plus the last few
    lifecycle comments (progress / blocker / feedback / resolution). The
    old TaskCheckpoint table is no longer read — migration 0082 moved
    checkpoint-typed comments into `progress`, and POST /checkpoint is 410.
    """
    from app.models.agent import Agent
    from app.models.checklist import TaskChecklistItem

    # Comments — last 5 relevant lifecycle entries, chronological.
    relevant_types = ("progress", "blocker", "feedback", "resolution")
    result = await session.exec(
        select(TaskComment)
        .where(
            TaskComment.task_id == task.id,
            TaskComment.comment_type.in_(relevant_types),  # type: ignore[union-attr]
        )
        .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
        .limit(5)
    )
    comments = list(result.all())
    comments.sort(key=lambda c: c.created_at)

    # Checklist items — ordered, flagged for first-pending.
    items_result = await session.exec(
        select(TaskChecklistItem)
        .where(TaskChecklistItem.task_id == task.id)
        .order_by(TaskChecklistItem.sort_order)  # type: ignore[union-attr]
    )
    items = list(items_result.all())

    if not comments and not items:
        return None

    parts: list[str] = [
        "## Recovery — Du hast hier aufgehoert",
        "",
        "**WICHTIG:** Faengst NICHT neu an. Setze bei `← HIER WEITERMACHEN` "
        "fort oder beim letzten `progress`-Eintrag. Kein Re-Doing.",
    ]

    if items:
        parts.append("\n### Deine Checkliste")
        _found_first_pending = False
        for item in items:
            mark = "[x]" if item.status == "done" else "[ ]"
            hint = ""
            if item.status in ("pending", "in_progress") and not _found_first_pending:
                hint = " ← **HIER WEITERMACHEN**"
                _found_first_pending = True
            parts.append(f"- {mark} {item.title}{hint}")

    if comments:
        parts.append("\n### Letzter Fortschritt")
        for c in comments:
            ts = c.created_at.strftime("%H:%M") if c.created_at else "?"
            label = {
                "feedback": "REVIEWER-FEEDBACK",
                "blocker": "BLOCKER",
                "resolution": "resolution",
                "progress": "progress",
            }.get(c.comment_type, c.comment_type)
            # Truncate long comments in the recap — agent can fetch full via
            # `mc comment list` if needed.
            snippet = c.content.strip().splitlines()[0][:180]
            parts.append(f"[{label} @ {ts}] {snippet}")

    # Workspace hint — Task.workspace_path is authoritative (Bundle 4),
    # agent workspace is fallback for tasks without their own worktree.
    _ws = getattr(task, "workspace_path", None)
    if not _ws and task.assigned_agent_id:
        agent_obj = await session.get(Agent, task.assigned_agent_id)
        if agent_obj:
            _ws = agent_obj.workspace_path
    if _ws:
        port = f" (Port: {task.workspace_port})" if getattr(task, "workspace_port", None) else ""
        parts.append(f"\n### Workspace\n`{_ws}`{port}")

    # Operator decision (from an approved blocker) — surface inline.
    from app.models.approval import Approval
    approval_result = await session.exec(
        select(Approval)
        .where(
            Approval.task_id == task.id,
            Approval.action_type == "blocker_decision",
            Approval.status == "approved",
        )
        .order_by(Approval.resolved_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    last_approval = approval_result.first()
    if last_approval and last_approval.resolver_note:
        parts.append(
            f"\n### Operator-Entscheidung\n{last_approval.resolver_note}"
        )

    parts.append(
        "\n### Naechster Schritt\n"
        "Pruefe den Workspace (Dateien, Git). Mach beim ersten offenen "
        "Checklist-Item weiter. Nutze `mc checklist done <id>` + "
        "`mc comment progress \"...\"` beim Fortschritt."
    )

    return "\n".join(parts)
