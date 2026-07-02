"""
Work Context Service — Projekt-Auto-Detection + Config-Resolution + Shared Validators.

Analysiert das Filesystem eines Workspaces und leitet Stack/Framework/Befehle ab.
Wird von dispatch.py aufgerufen wenn ein Task eine Dispatch-Message bekommt.

Phase 4 Plan 04-04 (REF-02 step 1): Erweitert um Shared-Validatoren die
zuvor in routers/agent_scoped.py wohnten:
  - enforce_board_rules_agent  (war _enforce_board_rules_agent, 137 Zeilen)
  - enforce_reflection         (extrahiert aus Inline-Block der Board-Rules)
  - find_reviewer              (war _find_reviewer)
  - find_last_developer        (war _find_last_developer)
  - VALID_BLOCKER_TYPES        (frozenset Konstante)

agent_scoped.py haelt einen Re-Export-Shim (Pattern S1) der die unterstrich-
prefixierten Aliase bereitstellt — task_lifecycle.py:707/:878 und mehrere
Test-Dateien importieren `_find_reviewer` / `_find_last_developer` /
`_enforce_board_rules_agent` aus agent_scoped.
"""

import json
import logging
import os
import uuid

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.task import Task, TaskComment

logger = logging.getLogger("mc.work_context")


# Moved from agent_scoped.py:661 (Phase 4 REF-02 Plan 04-04).
# Endpoint-Validierung fuer blocker_type — frozenset damit der Inhalt
# nicht versehentlich mutiert wird. Aenderungen hier wirken auf die
# PATCH /agent/boards/{board_id}/tasks/{task_id} Validierung.
VALID_BLOCKER_TYPES: frozenset[str] = frozenset({
    "missing_info", "technical_problem", "decision_needed",
    "permission_needed", "dependency_blocked", "other",
})


async def detect_project_config(workspace_path: str) -> dict:
    """Leitet Projekt-Konfiguration aus dem Filesystem ab.

    Liest package.json, pyproject.toml, docker-compose.yml.
    Gibt leeres dict zurück wenn kein bekannter Stack erkannt wird.
    """
    config: dict = {}

    # Node.js / Next.js
    pkg_path = os.path.join(workspace_path, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
            config["stack"] = "node"
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                config["framework"] = "nextjs"
                config["dev_command"] = "npm run dev -- -p {port}"
                config["test_command"] = "npm run test:run"
            else:
                config["test_command"] = "npm test"
        except Exception:
            pass

    # Python
    elif os.path.exists(os.path.join(workspace_path, "pyproject.toml")):
        config["stack"] = "python"
        config["test_command"] = "pytest"
    elif os.path.exists(os.path.join(workspace_path, "requirements.txt")):
        config["stack"] = "python"
        config["test_command"] = "pytest"

    # Docker
    if os.path.exists(os.path.join(workspace_path, "docker-compose.yml")):
        config["has_docker"] = True

    # Source-Verzeichnisse ermitteln
    for d in ["frontend-v2", "frontend", "src", "app", "backend"]:
        if os.path.isdir(os.path.join(workspace_path, d)):
            config.setdefault("source_dirs", []).append(d)

    return config


def resolve_project_config(
    auto_config: dict | None,
    manual_config: dict | None,
) -> dict:
    """Merged Auto-Detection und manuelle Config.

    Cascade: Manual > Auto > System-Default.
    Manuelle Werte überschreiben Auto-Werte bei Schlüssel-Konflikten.
    """
    result: dict = {}
    if auto_config:
        result.update(auto_config)
    if manual_config:
        result.update(manual_config)  # Manual überschreibt Auto
    return result


def build_config_dispatch_section(
    project_name: str,
    config: dict,
    port: int | None = None,
) -> str:
    """Baut die Projekt-Kontext-Sektion für die Dispatch-Message."""
    lines = [f"## Projekt-Kontext ({project_name})"]

    if config.get("stack"):
        stack_label = {
            "node": "Node.js" + (" / Next.js" if config.get("framework") == "nextjs" else ""),
            "python": "Python",
        }.get(config["stack"], config["stack"])
        lines.append(f"- Stack: {stack_label}")

    if config.get("source_dir"):
        lines.append(f"- Quellverzeichnis: `{config['source_dir']}`")
    elif config.get("source_dirs"):
        lines.append(f"- Verzeichnisse: {', '.join(f'`{d}`' for d in config['source_dirs'])}")

    if config.get("dev_command"):
        cmd = config["dev_command"]
        if port and "{port}" in cmd:
            cmd = cmd.replace("{port}", str(port))
        lines.append(f"- Dev-Server: `{cmd}`")

    if config.get("test_command"):
        lines.append(f"- Tests: `{config['test_command']}`")

    if config.get("build_command"):
        lines.append(f"- Build: `{config['build_command']}`")

    if config.get("notes"):
        lines.append(f"- **WICHTIG:** {config['notes']}")

    return "\n".join(lines)


async def validate_task_completion(session, task) -> tuple[bool, list[str]]:
    """Prüft ob ein Task die Abschlussbedingungen erfüllt.

    Gibt (ok, fehler_liste) zurück.
    Checks: checklist vollständigkeit, Git-Commits (wenn Code-Task mit Repo), Deliverables (wenn visual_proof).
    """
    from sqlmodel import select
    from app.models.board import Project
    from app.models.deliverable import TaskDeliverable

    errors: list[str] = []

    # 1. Checklist-Vollständigkeit — direkt aus DB (nicht aus denormalisiertem Counter)
    from app.models.checklist import TaskChecklistItem
    checklist_result = await session.exec(
        select(TaskChecklistItem).where(TaskChecklistItem.task_id == task.id)
    )
    all_checklist_items = checklist_result.all()
    if all_checklist_items:
        pending_items = [i for i in all_checklist_items if i.status not in ("done", "skipped")]
        if pending_items:
            errors.append(f"{len(pending_items)} Checklist-Item(s) noch offen")

    # 2. Git-Verification — nur wenn:
    #    a) Code-Task mit workspace_path + Projekt-Repo
    #    b) Assigned Agent hat requires_git_workflow=True (Default)
    # Designer/Writer/Researcher/Orchestrator produzieren Files/Deliverables, kein Code
    # → Git-Commit-Pflicht wuerde sie permanent blocken.
    if task.workspace_path and task.project_id:
        # Agent-Flag pruefen (nur wenn assigned)
        _git_required = True
        if task.assigned_agent_id:
            from app.models.agent import Agent
            _assigned = await session.get(Agent, task.assigned_agent_id)
            if _assigned is not None and not getattr(_assigned, "requires_git_workflow", True):
                _git_required = False

        if _git_required:
            project = await session.get(Project, task.project_id)
            if project and project.github_repo_url:
                # Skip the check when workspace_path is not actually a git repo.
                # Happens in two observed cases:
                #   1. MC fell back to an empty placeholder dir at dispatch
                #      time (backend couldn't clone into `/workspace`, used
                #      ~/FreeCode/projects/<slug>/ instead — never populated).
                #   2. Agent worked in a different location the container
                #      could write to (e.g. container-side /workspace/...),
                #      so the host-side path the backend sees stays empty.
                # In both cases, blocking on "Keine Git-Commits" is wrong:
                # the agent's push may have succeeded to a completely
                # separate repo checkout, and the task gate has no way to
                # verify that from here.
                from pathlib import Path as _Path
                workspace_is_git_repo = (
                    _Path(task.workspace_path, ".git").exists()
                    if task.workspace_path else False
                )
                if workspace_is_git_repo:
                    from app.services.git_service import git_service
                    try:
                        has_commits = await git_service.has_task_commits(task.workspace_path)
                        if not has_commits:
                            errors.append("Keine Git-Commits im Workspace gefunden")
                    except Exception:
                        pass  # Git-Check best-effort — kein Block bei Fehler

    # 3. Deliverable-Check (wenn needs_browser oder visual_proof)
    if task.needs_browser or task.delegation_type == "visual_proof":
        result = await session.exec(
            select(TaskDeliverable).where(TaskDeliverable.task_id == task.id).limit(1)
        )
        if not result.first():
            errors.append("Kein Deliverable registriert (Screenshot erwartet)")

    return (len(errors) == 0, errors)


# ─────────────────────────────────────────────────────────────────────
# Shared Validators — Phase 4 REF-02 Plan 04-04
# Moved from backend/app/routers/agent_scoped.py.
# agent_scoped.py exposes Pattern S1 re-export shims with the original
# underscored names (`_enforce_board_rules_agent`, `_find_reviewer`,
# `_find_last_developer`) for backward compat with task_lifecycle.py
# (lines 707, 878) and several test files that import or `mock.patch`
# the underscored aliases via the agent_scoped namespace.
# ─────────────────────────────────────────────────────────────────────


async def enforce_reflection(
    session: AsyncSession,
    task: Task,
    agent: Agent,
    new_status: str,
) -> None:
    """Pflicht-Reflexion enforcen — Pattern S4 + ADR-A1.

    Raises HTTPException(400) wenn ein Reflection-Kommentar fehlt oder zu kurz
    ist und der Status-Uebergang als "Closing" gilt (in_progress/inbox →
    review/done). Board-Lead-Agents sind ausgenommen.

    Status-Code = 400 (Production-Verhalten per A1 — NICHT 422). Die deutschen
    Fehlermeldungen werden von TST-04 (Plan 04-11) verbatim assertet — bitte
    nicht stilistisch anpassen.

    Pre-conditions (skip wenn eines false):
      - settings.enforce_reflection (global toggle)
      - new_status in {"review", "done"}
      - task.status NICHT bereits "review" oder "user_test"
      - agent.is_board_lead == False

    Logik extrahiert aus dem Inline-Block in agent_scoped.py:86-122
    (Phase 4 REF-02 Plan 04-04).
    """
    # Lazy imports — Konstanten leben in app.constants, settings in app.config.
    # Das verhindert Circular-Imports beim Modul-Load.
    from app.config import settings as _cfg
    from app.constants import REFLECTION_REQUIRED_FIELDS, REFLECTION_MIN_CHARS

    _is_closing_transition = (
        new_status in ("review", "done")
        and task.status not in ("review", "user_test")
    )
    if not (_cfg.enforce_reflection and _is_closing_transition and not agent.is_board_lead):
        return

    _refl_exists = await session.exec(
        select(TaskComment)
        .where(
            TaskComment.task_id == task.id,
            TaskComment.author_agent_id == agent.id,
            TaskComment.comment_type == "reflection",
        )
        .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    reflection_comment = _refl_exists.first()
    _fields_str = " / ".join(REFLECTION_REQUIRED_FIELDS)
    _mc_hint = (
        'mc comment reflection "'
        + "\\n\\n".join(f"## {f}\\n..." for f in REFLECTION_REQUIRED_FIELDS)
        + '"'
    )
    if reflection_comment is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Pflicht-Reflexion fehlt: vor Task-Abschluss muss du einen "
                f"comment_type='reflection' posten ({len(REFLECTION_REQUIRED_FIELDS)} "
                f"Pflichtfelder: {_fields_str}). Beispiel: `{_mc_hint}`"
            ),
        )
    if len(reflection_comment.content or "") < REFLECTION_MIN_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Reflexions-Kommentar zu kurz (mind. {REFLECTION_MIN_CHARS} Zeichen mit "
                f"{len(REFLECTION_REQUIRED_FIELDS)} Pflichtfeldern). Beispiel: `{_mc_hint}`"
            ),
        )


async def enforce_board_rules_agent(
    session: AsyncSession,
    board_id: uuid.UUID,
    task: Task,
    new_status: str,
    agent: Agent,
) -> None:
    """Board Workflow Rules pruefen fuer Agent-Status-Aenderungen.

    Verbatim moved from backend/app/routers/agent_scoped.py:36-172
    (Phase 4 REF-02 Plan 04-04). Der Inline-Reflection-Block wurde in
    `enforce_reflection` (siehe oben) extrahiert; das Verhalten bleibt
    identisch (gleiche Pre-Conditions, gleiche HTTP-Codes, gleicher Wortlaut).

    agent_scoped.py haelt einen Re-Export-Shim
    `_enforce_board_rules_agent = enforce_board_rules_agent` fuer Test-Imports.
    """
    from app.routers.tasks import VALID_TRANSITIONS, STATUS_LABELS
    from app.task_status import check_children_complete

    # Rule 0: Gueltige Status-Uebergaenge pruefen
    current = task.status

    # Guard: done ist terminal fuer Agents — Re-Open nur via UI (User-Aktion)
    if current == "done" and new_status == "in_progress":
        raise HTTPException(
            status_code=400,
            detail="Ein abgeschlossener Task kann nur manuell über die UI wieder geöffnet werden",
        )

    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        from_label = STATUS_LABELS.get(current, current)
        to_label = STATUS_LABELS.get(new_status, new_status)
        raise HTTPException(
            status_code=400,
            detail=f"Ungültiger Status-Übergang: {from_label} → {to_label}",
        )

    # Rule 1: Parent/Child Integritaet — Parent darf nicht abgeschlossen werden wenn Children offen
    if new_status in ("done", "review"):
        children_ok, children_detail = await check_children_complete(task.id, session)
        if not children_ok:
            raise HTTPException(status_code=400, detail=children_detail)

    # Rule T-1: Pre-Done Validation (Checklist + Git + Deliverable)
    if new_status in ("done", "review"):
        ok, errors = await validate_task_completion(session, task)
        if not ok:
            raise HTTPException(
                status_code=422,
                detail=f"Task kann nicht abgeschlossen werden: {'; '.join(errors)}",
            )

    # ADR-023: Reflexion ist ein unabhaengiger Hebel — unabhaengig von jeder
    # Review-Policy (board-flag, project.review_policy, task.skip_review).
    # Deshalb ZUERST pruefen, BEVOR irgendein early-return den Rest ueberspringt.
    await enforce_reflection(session, task, agent, new_status)

    # T-1: Projekt-spezifische Review-Policy (überschreibt Board-Default)
    _project_review_policy = None
    if task.project_id:
        _project = await session.get(Project, task.project_id)
        if _project and _project.project_config:
            _project_review_policy = _project.project_config.get("review_policy")

    # Policy "never" → Review-Gate vollständig überspringen (Reflection lief schon oben)
    if _project_review_policy == "never" and new_status == "done":
        return

    # skip_review Flag auf dem Task (z.B. von Scheduler gesetzt) → Review-Gate überspringen
    if getattr(task, "skip_review", False) and new_status == "done":
        return  # Automation-Tasks brauchen kein Review

    board = await session.get(Board, board_id)
    if not board:
        return

    # Rule 2: Task muss durch Review bevor es auf Done gesetzt werden kann.
    # Board-flag is a HARD GATE — keep it for projects that need it. On
    # mc-dev we set it to `false` (ADR-023) so the Dev decides per task
    # whether to call `mc review` first; Rex becomes opt-in. The SOUL of
    # each developer agent has the explicit rules for WHEN to review.
    if board.require_review_before_done:
        if new_status == "done" and task.status not in ("review", "user_test"):
            # Subtasks (mit parent_task_id) duerfen direkt done — Review
            # laeuft auf Phase-Ebene (Parent wird auto auf review gesetzt
            # wenn alle Subtasks done).
            is_subtask = task.parent_task_id is not None

            # Pruefen ob Parent-Task mit allen Subtasks done
            subtask_result = await session.exec(
                select(Task).where(Task.parent_task_id == task.id)
            )
            subtasks = subtask_result.all()
            is_completed_parent = subtasks and all(s.status == "done" for s in subtasks)
            if not is_completed_parent and not is_subtask:
                raise HTTPException(
                    status_code=400,
                    detail="Task muss zuerst durch Review bevor es auf Done gesetzt werden kann",
                )

    # Rule 3: Nur der Board Lead darf den Status aendern
    if board.only_lead_can_change_status and not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur der Board Lead darf den Task-Status aendern",
        )


async def find_reviewer(
    session: AsyncSession,
    board_id: uuid.UUID,
) -> Agent | None:
    """Reviewer-Agent im Board finden — primaer nach Rolle, Legacy-Fallback nach Name.

    Verbatim moved from backend/app/routers/agent_scoped.py:3533-3560
    (Phase 4 REF-02 Plan 04-04).

    Pattern S2: `find_agent_by_role` ist ein lazy local import — work_context
    und dispatch duerfen sich nicht beim Modul-Load gegenseitig importieren.
    """
    from app.scopes import AgentRole
    # CRITICAL (Pattern S2): keep lazy local import to break the cycle
    from app.services.dispatch import find_agent_by_role

    # Primaer: Rolle-basierte Suche
    reviewer = await find_agent_by_role(session, board_id, AgentRole.REVIEWER)
    if reviewer:
        return reviewer

    # Legacy-Fallback: Name-basiert fuer Agents ohne role.
    # Phase 30: gateway_agent_id filter dropped — runtime is the new check.
    result = await session.exec(
        select(Agent).where(
            Agent.board_id == board_id,
            Agent.role.is_(None),  # type: ignore[union-attr]
        )
    )
    agents = result.all()
    for a in agents:
        name_lower = a.name.lower()
        if "rex" in name_lower or "review" in name_lower:
            return a
    # Letzter Fallback: Board Lead
    for a in agents:
        if a.is_board_lead:
            return a
    return None


async def find_last_developer(
    session: AsyncSession,
    task: Task,
) -> Agent | None:
    """Developer finden der den Task zuletzt fuer Review eingereicht hat.

    Sucht den Agent der in_progress → review gesetzt hat (= Code eingereicht)
    und NICHT der Reviewer ist. Das ist zuverlaessiger als nach in_progress-Events
    zu suchen, weil Reviewer-ACKs und mehrfache Dispatch-Zyklen ebenfalls
    in_progress-Events erzeugen.

    Fallback: Agent der progress/resolution-Kommentare geschrieben hat.

    Verbatim moved from backend/app/routers/agent_scoped.py:3563-3608
    (Phase 4 REF-02 Plan 04-04).
    """
    from app.models.activity import ActivityEvent

    # Reviewer ermitteln (damit wir ihn ausschliessen koennen).
    # Hinweis: ruft die work_context-lokale `find_reviewer` (gleicher Modul-
    # namespace). Tests die `app.routers.agent_scoped._find_reviewer` patchen
    # ueberbruecken diesen internen Aufruf NICHT — was bisher auch so war,
    # weil der Aufruf intern in agent_scoped.py via lokalem Namen lief und
    # dadurch ebenfalls nicht ueber den Modul-Lookup ging. Verhalten unveraendert.
    reviewer = await find_reviewer(session, task.board_id)
    reviewer_id = reviewer.id if reviewer else None

    # Primaer: Agent der in_progress → review gesetzt hat (nicht Reviewer)
    result = await session.exec(
        select(ActivityEvent).where(
            ActivityEvent.task_id == task.id,
            ActivityEvent.event_type == "task.status_changed",
        ).order_by(ActivityEvent.created_at.desc())
    )
    events = result.all()
    for ev in events:
        detail = ev.detail or {}
        if (detail.get("new_status") == "review"
                and detail.get("old_status") == "in_progress"
                and ev.agent_id
                and ev.agent_id != reviewer_id):
            return await session.get(Agent, ev.agent_id)

    # Fallback: Agent mit progress/resolution-Kommentaren (nicht Reviewer)
    comment_result = await session.exec(
        select(TaskComment).where(
            TaskComment.task_id == task.id,
            TaskComment.author_type == "agent",
            TaskComment.comment_type.in_(["progress", "resolution"]),  # type: ignore[union-attr]
        ).order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
    )
    for c in comment_result.all():
        if c.author_agent_id and c.author_agent_id != reviewer_id:
            return await session.get(Agent, c.author_agent_id)

    return None
