"""CLI Bridge Runner — Workspace-Setup fuer Docker cli-bridge Agents.

Nach ADR-022 + ADR-024-Refactor ist das reine Backend-Seiten-Logik:
- Kein HTTP-Bridge-Daemon mehr (obsolete Free-Code-Bridge-Aera).
- Keine Pfad-Translation Docker→Host — `agent.workspace_path` ist schon
  der Host-Pfad (z.B. `${HOME_HOST}/.mc/workspaces/<slug>`), und Backend
  sieht ihn identisch via `${HOME}/.mc:${HOME}/.mc` Bind-Mount.
- Dispatch-Message nutzt `_container_workspace_path()` aus dispatch.py um
  den Host-Pfad in die Container-View (`/workspace/...`) umzurechnen.

Workspace-Layout (ADR-022):
- Task MIT GitHub-Repo: Git-Worktree unter
  `<agent_ws>/projects/<proj_slug>/.worktrees/<task_slug>/`
- Task mit Projekt ohne Repo: Plain-Dir unter `<agent_ws>/<task_slug>/`
- Ad-hoc Task (kein Projekt): `<agent_ws>/<task_slug>/`
"""

import logging
import os

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Project
from app.models.task import Task
from app.services.activity import emit_event
from app.services.git_service import git_service, slugify_project, slugify_workspace_slug

logger = logging.getLogger("mc.cli_bridge")


def _agent_slug(agent: Agent) -> str:
    """Leitet den Slug aus dem Agent-Namen ab.

    Beispiel: "FreeCode" → "freecode", "My Agent" → "my-agent"
    """
    return agent.name.lower().replace(" ", "-")


async def dispatch_to_cli_bridge(
    agent: Agent,
    task: Task,
    message: str,
    session: AsyncSession,
) -> bool:
    """Bereitet Workspace vor fuer Docker cli-bridge Agents.

    Task bleibt 'inbox' — poll.sh im Container holt ihn via /me/poll.
    Kein HTTP /enqueue, kein Bridge-Poll — Completion via Agent MC-API.
    """
    try:
        workspace, worktree_path, has_repo = await _resolve_workspace(task, agent, session)
    except Exception as e:
        # Workspace-Setup fehlgeschlagen (z.B. git clone scheiterte weil
        # destination existiert). Bis 2026-04-19 fiel der Code still auf einen
        # leeren placeholder zurueck — Agent fand dann ein fremdes Repo auf dem
        # Host-Mount und committete dort. Jetzt: Task blockieren statt
        # versehentlich Commits ins falsche Repo zu riskieren.
        logger.error(
            "Workspace setup failed for task %s ('%s'): %s",
            task.id, task.title, e,
        )
        from app.models.task import TaskComment
        blocker = TaskComment(
            task_id=task.id,
            author_type="system",
            comment_type="blocker",
            content=(
                "**Workspace-Setup fehlgeschlagen** — Dispatch abgebrochen, kein "
                "Placeholder-Fallback.\n\n"
                f"**Fehler:** `{type(e).__name__}: {e}`\n\n"
                "Typische Ursachen:\n"
                "- Destination-Dir existiert mit anderem Inhalt (manueller Cleanup "
                "im Agent-Workspace noetig)\n"
                "- Project.github_repo_url falsch konfiguriert (UI/DB pruefen)\n"
                "- GH_TOKEN fehlt Rechte oder Repo nicht zugreifbar\n\n"
                "**Question for @Operator** — Welcher Cleanup-Weg ist richtig?"
            ),
        )
        task.status = "blocked"
        # Auto-Unassign: Workspace-Setup-Fehler ist Operator-Approval-Wait,
        # kein Callback-Wait. Verhindert Cancel-Schleife im agent_poll.
        from app.services.task_lifecycle import apply_terminal_unassign
        await apply_terminal_unassign(session, task, "blocked")
        session.add(task)
        session.add(blocker)
        await session.commit()
        await emit_event(
            session, "task.blocked",
            f"Workspace setup failed for '{task.title}'",
            severity="error",
            board_id=task.board_id, task_id=task.id, agent_id=agent.id,
            detail={"error": str(e)},
        )
        return False

    # `agent.workspace_path` ist schon der korrekte Host-Pfad (ADR-022).
    # Backend sieht ihn identisch via ${HOME}/.mc Bind-Mount. Keine
    # Docker→Host Translation mehr noetig.
    host_workspace = worktree_path or workspace

    # Workspace-Pfad in DB persistieren — wird von anderen Tasks (z.B. Tester)
    # genutzt um Code-Speicherort zu finden.
    task.workspace_path = host_workspace
    session.add(task)
    await session.commit()

    logger.info(
        "CLI Bridge prepared workspace for '%s' (agent=%s, workspace=%s)",
        task.title, _agent_slug(agent), host_workspace,
    )

    await emit_event(
        session, "task.cli_bridge_ready",
        f"Workspace bereit fuer '{task.title}' (agent: {_agent_slug(agent)}, workspace: {host_workspace})",
        board_id=task.board_id, task_id=task.id, agent_id=agent.id,
    )

    return True


async def _resolve_workspace(task: Task, agent: Agent, session: AsyncSession):
    """Workspace-Pfad fuer den Task aufloesen (Host-Pfad unter agent.workspace_path).

    Drei Pfade:
    - Task + Projekt mit github_repo_url → Git-Worktree unter
      `<agent_ws>/projects/<proj_slug>/.worktrees/<task_slug>/`
    - Task + Projekt ohne Repo → Plain-Dir unter `<agent_ws>/<task_slug>/`
    - Ad-hoc Task (kein Projekt) → `<agent_ws>/<task_slug>/`

    Returns: (workspace_path, worktree_path, has_repo)
    - worktree_path: str wenn Git Worktree erstellt, None sonst
    - has_repo: True wenn Git-Repo vorhanden
    """
    if not agent.workspace_path:
        raise RuntimeError(
            f"Agent {agent.name} hat keine workspace_path — Migration 0087 "
            f"nicht gelaufen? Task-Dispatch kann ohne Base-Dir nicht fortgesetzt "
            f"werden."
        )

    agent_base = agent.workspace_path
    workspace = agent_base
    worktree_path = None
    has_repo = False

    if task.project_id:
        project = await session.get(Project, task.project_id)
        if project and project.github_repo_url:
            # Code-Task mit Repo — Git-Setup MUSS erfolgreich sein (Exception
            # propagiert, Dispatcher blockt den Task). Ohne harten Fail fiel
            # der Code frueher auf leere Placeholder zurueck und Agents
            # committeten ins falsche Repo (Incident 2026-04-19).
            has_repo = True
            task_slug = slugify_workspace_slug(task.title)
            main_repo = await git_service.ensure_workspace(
                agent_base,
                project.github_repo_url,
                slugify_project(project.name),
            )
            try:
                worktree_path = await git_service.create_task_worktree(
                    main_repo,
                    task_slug,
                )
                workspace = worktree_path
                await git_service.setup_git_identity(worktree_path, agent.name)
                logger.info(
                    "CLI Bridge worktree for task '%s': %s",
                    task.title, worktree_path,
                )
            except Exception as e:
                # Worktree-Fehler non-fatal — main-repo ist funktionsfaehig.
                logger.warning(
                    "Worktree creation failed for task '%s', falling back to main repo: %s",
                    task.title, e,
                )
                worktree_path = None
                workspace = main_repo

            # MC pre-push guard: erwartete Remote-URL in den Workspace
            # schreiben (Defense-in-depth vs. 2026-04-19 fremd-Repo-Bug).
            _write_expected_remote(workspace, project.github_repo_url)
        elif project:
            # Projekt ohne GitHub-Repo → Plain-Dir unter Agent-Workspace.
            workspace = _create_plain_workspace(agent_base, task.title)
    else:
        # Ad-hoc Task (kein Projekt) → eigenes Dir unter Agent-Workspace.
        workspace = _create_plain_workspace(agent_base, task.title)

    return workspace, worktree_path, has_repo


def _create_plain_workspace(base_path: str, task_title: str) -> str:
    """Plain-Verzeichnis ohne Git unter `<base_path>/<task-slug>/`.

    `base_path` ist der Agent-Workspace (Host-Pfad, ADR-022), z.B.
    `${HOME_HOST}/.mc/workspaces/sparky`. Backend sieht ihn identisch via
    `${HOME}/.mc` Bind-Mount.
    """
    slug = slugify_workspace_slug(task_title)
    task_dir = os.path.join(base_path, slug)
    os.makedirs(task_dir, exist_ok=True)
    logger.info("CLI Bridge plain workspace created: %s", task_dir)
    return task_dir


def _write_expected_remote(workspace: str, repo_url: str) -> None:
    """Pin the expected git remote URL for the pre-push hook.

    The agent-side pre-push hook (docker/mc-agent-base/lib/mc-pre-push.sh)
    reads `<workspace>/.mc-expected-remote` and aborts pushes whose
    origin URL doesn't match. Idempotent: overwrites on each dispatch
    so task-redispatches with a different workspace stay in sync.
    """
    try:
        marker = os.path.join(workspace, ".mc-expected-remote")
        with open(marker, "w") as f:
            f.write(repo_url.strip() + "\n")
        os.chmod(marker, 0o644)
    except OSError as e:
        # Non-fatal: hook will be silent instead of enforcing. Log loud
        # so we notice if the marker was never written.
        logger.warning(
            "Could not write .mc-expected-remote to %s: %s — pre-push guard disabled for this task",
            workspace, e,
        )


