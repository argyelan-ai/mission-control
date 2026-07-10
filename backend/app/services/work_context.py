"""
Work Context Service — project auto-detection + config resolution + shared validators.

Analyzes a workspace's filesystem and infers stack/framework/commands.
Called by dispatch.py when a task receives a dispatch message.

Phase 4 Plan 04-04 (REF-02 step 1): Extended with shared validators that
previously lived in routers/agent_scoped.py:
  - enforce_board_rules_agent  (was _enforce_board_rules_agent, 137 lines)
  - enforce_reflection         (extracted from the board-rules inline block)
  - find_reviewer              (was _find_reviewer)
  - find_last_developer        (was _find_last_developer)
  - VALID_BLOCKER_TYPES        (frozenset constant)

agent_scoped.py holds a re-export shim (Pattern S1) that provides the
underscore-prefixed aliases — task_lifecycle.py:707/:878 and several
test files import `_find_reviewer` / `_find_last_developer` /
`_enforce_board_rules_agent` from agent_scoped.
"""

import json
import logging
import os
import re
import uuid

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.task import Task, TaskComment

logger = logging.getLogger("mc.work_context")


# Reflection header matcher — 1-3 leading '#', canonical GERMAN field label
# (any case, optional trailing colon). Level > 3 is not treated as a header.
# Mirrors scripts/mc-cli/mc_cli/reflection.py's _HEADER_RE, but the backend
# gate only needs the canonical German form: clients (mc-cli / omp bridge)
# already normalize English -> German before POSTing (Wave 1). No English
# aliases here on purpose — this is the hard structural gate, not a tolerant
# client-side parser.
_REFLECTION_HEADER_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*$", re.MULTILINE)


def _fold_reflection_label(s: str) -> str:
    """Normalize a header label for tolerant (non-byte-brittle) matching:
    lowercase, ü/ö/ä/ß folded to ue/oe/ae/ss, trailing colon dropped,
    whitespace collapsed."""
    s = s.strip().rstrip(":").strip().lower()
    for a, b in (("ü", "ue"), ("ö", "oe"), ("ä", "ae"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _missing_reflection_headers(content: str, required_fields: list[str]) -> list[str]:
    """Return the canonical required fields whose header is NOT present in
    `content`, tolerant of heading level (#/##/###), case, trailing colon,
    and ü/ue-style umlaut spelling. Order preserved from `required_fields`."""
    present_folded = {
        _fold_reflection_label(m.group(1))
        for m in _REFLECTION_HEADER_RE.finditer(content or "")
    }
    return [
        field for field in required_fields
        if _fold_reflection_label(field) not in present_folded
    ]


# Moved from agent_scoped.py:661 (Phase 4 REF-02 Plan 04-04).
# Endpoint validation for blocker_type — frozenset so the content
# isn't accidentally mutated. Changes here affect the
# PATCH /agent/boards/{board_id}/tasks/{task_id} validation.
VALID_BLOCKER_TYPES: frozenset[str] = frozenset({
    "missing_info", "technical_problem", "decision_needed",
    "permission_needed", "dependency_blocked", "other",
})


async def detect_project_config(workspace_path: str) -> dict:
    """Infers project configuration from the filesystem.

    Reads package.json, pyproject.toml, docker-compose.yml.
    Returns an empty dict if no known stack is detected.
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

    # Determine source directories
    for d in ["frontend-v2", "frontend", "src", "app", "backend"]:
        if os.path.isdir(os.path.join(workspace_path, d)):
            config.setdefault("source_dirs", []).append(d)

    return config


def resolve_project_config(
    auto_config: dict | None,
    manual_config: dict | None,
) -> dict:
    """Merges auto-detection and manual config.

    Cascade: Manual > Auto > System default.
    Manual values override auto values on key conflicts.
    """
    result: dict = {}
    if auto_config:
        result.update(auto_config)
    if manual_config:
        result.update(manual_config)  # Manual overrides auto
    return result


def build_config_dispatch_section(
    project_name: str,
    config: dict,
    port: int | None = None,
) -> str:
    """Builds the project-context section for the dispatch message."""
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
    """Checks whether a task meets its completion conditions.

    Returns (ok, error_list).
    Checks: checklist completeness, git commits (if code task with repo), deliverables (if visual_proof).
    """
    from sqlmodel import select
    from app.models.board import Project
    from app.models.deliverable import TaskDeliverable

    errors: list[str] = []

    # 1. Checklist completeness — directly from DB (not from a denormalized counter)
    from app.models.checklist import TaskChecklistItem
    checklist_result = await session.exec(
        select(TaskChecklistItem).where(TaskChecklistItem.task_id == task.id)
    )
    all_checklist_items = checklist_result.all()
    if all_checklist_items:
        pending_items = [i for i in all_checklist_items if i.status not in ("done", "skipped")]
        if pending_items:
            errors.append(f"{len(pending_items)} Checklist-Item(s) noch offen")

    # 2. Git verification — only if:
    #    a) code task with workspace_path + project repo
    #    b) assigned agent has requires_git_workflow=True (default)
    # Designer/Writer/Researcher/Orchestrator produce files/deliverables, not code
    # → the git-commit requirement would block them permanently.
    if task.workspace_path and task.project_id:
        # Check agent flag (only if assigned)
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
                        pass  # Git check is best-effort — don't block on error

    # 3. Deliverable check (if needs_browser or visual_proof)
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
    """Enforce mandatory reflection — Pattern S4 + ADR-A1.

    Raises HTTPException(400) if a reflection comment is missing or too short
    and the status transition counts as "closing" (in_progress/inbox →
    review/done). Board-lead agents are exempt.

    Status code = 400 (production behavior per A1 — NOT 422). The German
    error messages are asserted verbatim by TST-04 (Plan 04-11) — please
    do not adjust them stylistically.

    Pre-conditions (skip if any is false):
      - settings.enforce_reflection (global toggle)
      - new_status in {"review", "done"}
      - task.status NOT already "review" or "user_test"
      - agent.is_board_lead == False

    Logic extracted from the inline block in agent_scoped.py:86-122
    (Phase 4 REF-02 Plan 04-04).
    """
    # Lazy imports — constants live in app.constants, settings in app.config.
    # This avoids circular imports at module load time.
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
    # M2: existence + length alone are not a structural gate — a curl bypass
    # or degenerate model output can post an 80+ char blob with no headers
    # at all and "finish" cleanly. Require all 4 canonical German headers
    # to actually be present (tolerant of #/##/### level, case, trailing
    # colon, ü/ue). Clients (mc-cli / omp bridge) already normalize
    # English -> German before POSTing (Wave 1), so no English aliases here.
    _missing = _missing_reflection_headers(
        reflection_comment.content or "", REFLECTION_REQUIRED_FIELDS,
    )
    if _missing:
        _missing_str = " / ".join(_missing)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Reflexions-Kommentar unvollständig: fehlende Pflichtfelder "
                f"({_missing_str}). Nutze `mc finish` — das normalisiert die "
                f"Header automatisch. Beispiel: `{_mc_hint}`"
            ),
        )


async def enforce_board_rules_agent(
    session: AsyncSession,
    board_id: uuid.UUID,
    task: Task,
    new_status: str,
    agent: Agent,
) -> None:
    """Check board workflow rules for agent status changes.

    Verbatim moved from backend/app/routers/agent_scoped.py:36-172
    (Phase 4 REF-02 Plan 04-04). The inline reflection block was extracted
    into `enforce_reflection` (see above); behavior stays
    identical (same pre-conditions, same HTTP codes, same wording).

    agent_scoped.py holds a re-export shim
    `_enforce_board_rules_agent = enforce_board_rules_agent` for test imports.
    """
    from app.routers.tasks import VALID_TRANSITIONS, STATUS_LABELS
    from app.task_status import check_children_complete

    # Rule 0: check valid status transitions
    current = task.status

    # Guard: done is terminal for agents — re-open only via UI (user action)
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

    # Rule 1: parent/child integrity — parent must not be closed while children are open
    if new_status in ("done", "review"):
        children_ok, children_detail = await check_children_complete(task.id, session)
        if not children_ok:
            raise HTTPException(status_code=400, detail=children_detail)

    # Rule T-1: pre-done validation (checklist + git + deliverable)
    if new_status in ("done", "review"):
        ok, errors = await validate_task_completion(session, task)
        if not ok:
            raise HTTPException(
                status_code=422,
                detail=f"Task kann nicht abgeschlossen werden: {'; '.join(errors)}",
            )

    # Rule 1b: human_review_required is a HARD GATE on done — independent of
    # board flag, project.review_policy, or skip_review. Checked BEFORE those
    # shortcuts below so none of them can be used to route around Mark.
    if new_status == "done" and task.status not in ("review", "user_test"):
        if getattr(task, "human_review_required", None):
            raise HTTPException(
                status_code=400,
                detail="Task erfordert Human-Review (Mark) bevor es auf Done gesetzt werden kann",
            )

    # ADR-023: reflection is an independent lever — independent of any
    # review policy (board flag, project.review_policy, task.skip_review).
    # So check FIRST, BEFORE any early return skips the rest.
    await enforce_reflection(session, task, agent, new_status)

    # T-1: project-specific review policy (overrides board default)
    _project_review_policy = None
    if task.project_id:
        _project = await session.get(Project, task.project_id)
        if _project and _project.project_config:
            _project_review_policy = _project.project_config.get("review_policy")

    # Policy "never" → skip the review gate entirely (reflection already ran above)
    if _project_review_policy == "never" and new_status == "done":
        return

    # skip_review flag on the task (e.g. set by scheduler) → skip the review gate
    if getattr(task, "skip_review", False) and new_status == "done":
        return  # Automation tasks don't need review

    board = await session.get(Board, board_id)
    if not board:
        return

    # Rule 2: task must go through review before it can be set to done.
    # Board-flag is a HARD GATE — keep it for projects that need it. On
    # mc-dev we set it to `false` (ADR-023) so the Dev decides per task
    # whether to call `mc review` first; Rex becomes opt-in. The SOUL of
    # each developer agent has the explicit rules for WHEN to review.
    if board.require_review_before_done:
        if new_status == "done" and task.status not in ("review", "user_test"):
            # Subtasks (with parent_task_id) may go directly to done — review
            # runs at the phase level (parent gets auto-set to review
            # when all subtasks are done).
            is_subtask = task.parent_task_id is not None

            # Check whether the parent task has all subtasks done
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

    # Rule 3: only the Board Lead may change the status
    if board.only_lead_can_change_status and not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur der Board Lead darf den Task-Status aendern",
        )


async def find_reviewer(
    session: AsyncSession,
    board_id: uuid.UUID,
) -> Agent | None:
    """Find the reviewer agent on the board — primarily by role, legacy fallback by name.

    Verbatim moved from backend/app/routers/agent_scoped.py:3533-3560
    (Phase 4 REF-02 Plan 04-04).

    Pattern S2: `find_agent_by_role` is a lazy local import — work_context
    and dispatch must not import each other at module load time.
    """
    from app.scopes import AgentRole
    # CRITICAL (Pattern S2): keep lazy local import to break the cycle
    from app.services.dispatch import find_agent_by_role

    # Primary: role-based search
    reviewer = await find_agent_by_role(session, board_id, AgentRole.REVIEWER)
    if reviewer:
        return reviewer

    # Legacy fallback: name-based for agents without a role.
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
    # Last fallback: Board Lead
    for a in agents:
        if a.is_board_lead:
            return a
    return None


async def find_last_developer(
    session: AsyncSession,
    task: Task,
) -> Agent | None:
    """Find the developer who last submitted the task for review.

    Looks for the agent who set in_progress → review (= submitted code)
    and is NOT the reviewer. This is more reliable than searching for
    in_progress events, because reviewer ACKs and multiple dispatch cycles
    also produce in_progress events.

    Fallback: agent who wrote progress/resolution comments.

    Verbatim moved from backend/app/routers/agent_scoped.py:3563-3608
    (Phase 4 REF-02 Plan 04-04).
    """
    from app.models.activity import ActivityEvent

    # Determine the reviewer (so we can exclude them).
    # Note: calls the work_context-local `find_reviewer` (same module
    # namespace). Tests that patch `app.routers.agent_scoped._find_reviewer`
    # do NOT intercept this internal call — which was already the case
    # before, because the call ran internally in agent_scoped.py via a
    # local name and therefore also didn't go through module lookup. Behavior unchanged.
    reviewer = await find_reviewer(session, task.board_id)
    reviewer_id = reviewer.id if reviewer else None

    # Primary: agent who set in_progress → review (not the reviewer)
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

    # Fallback: agent with progress/resolution comments (not the reviewer)
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
