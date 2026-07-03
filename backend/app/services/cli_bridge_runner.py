"""CLI Bridge Runner — workspace setup for Docker cli-bridge agents.

After ADR-022 + the ADR-024 refactor, this is pure backend-side logic:
- No more HTTP bridge daemon (obsolete Free-Code-Bridge era).
- No Docker→host path translation — `agent.workspace_path` is already
  the host path (e.g. `${HOME_HOST}/.mc/workspaces/<slug>`), and the
  backend sees it identically via the `${HOME}/.mc:${HOME}/.mc` bind mount.
- The dispatch message uses `_container_workspace_path()` from dispatch.py
  to convert the host path into the container view (`/workspace/...`).

Workspace layout (ADR-022):
- Task WITH GitHub repo: git worktree under
  `<agent_ws>/projects/<proj_slug>/.worktrees/<task_slug>/`
- Task with project but no repo: plain dir under `<agent_ws>/<task_slug>/`
- Ad-hoc task (no project): `<agent_ws>/<task_slug>/`
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
    """Derives the slug from the agent name.

    Example: "FreeCode" → "freecode", "My Agent" → "my-agent"
    """
    return agent.name.lower().replace(" ", "-")


async def dispatch_to_cli_bridge(
    agent: Agent,
    task: Task,
    message: str,
    session: AsyncSession,
) -> bool:
    """Prepares the workspace for Docker cli-bridge agents.

    Task stays 'inbox' — poll.sh in the container fetches it via /me/poll.
    No HTTP /enqueue, no bridge poll — completion via Agent MC-API.
    """
    try:
        workspace, worktree_path, has_repo = await _resolve_workspace(task, agent, session)
    except Exception as e:
        # Workspace setup failed (e.g. git clone failed because the
        # destination already exists). Until 2026-04-19, the code silently
        # fell back to an empty placeholder — the agent then found a
        # foreign repo on the host mount and committed there. Now: block
        # the task instead of risking accidental commits to the wrong repo.
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
        # Auto-unassign: workspace setup failure is an operator-approval wait,
        # not a callback wait. Prevents a cancel loop in agent_poll.
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

    # `agent.workspace_path` is already the correct host path (ADR-022).
    # The backend sees it identically via the ${HOME}/.mc bind mount. No
    # Docker→host translation needed anymore.
    host_workspace = worktree_path or workspace

    # Persist the workspace path in the DB — used by other tasks (e.g. Tester)
    # to find the code location.
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
    """Resolves the workspace path for the task (host path under agent.workspace_path).

    Three paths:
    - Task + project with github_repo_url → git worktree under
      `<agent_ws>/projects/<proj_slug>/.worktrees/<task_slug>/`
    - Task + project without repo → plain dir under `<agent_ws>/<task_slug>/`
    - Ad-hoc task (no project) → `<agent_ws>/<task_slug>/`

    Returns: (workspace_path, worktree_path, has_repo)
    - worktree_path: str if a git worktree was created, None otherwise
    - has_repo: True if a git repo exists
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
            # Code task with repo — git setup MUST succeed (exception
            # propagates, dispatcher blocks the task). Without a hard fail,
            # the code used to fall back to empty placeholders and agents
            # committed to the wrong repo (Incident 2026-04-19).
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
                # Worktree failure is non-fatal — main repo is functional.
                logger.warning(
                    "Worktree creation failed for task '%s', falling back to main repo: %s",
                    task.title, e,
                )
                worktree_path = None
                workspace = main_repo

            # MC pre-push guard: write the expected remote URL into the workspace
            # (defense-in-depth vs. the 2026-04-19 foreign-repo bug).
            _write_expected_remote(workspace, project.github_repo_url)
        elif project:
            # Project without a GitHub repo → plain dir under the agent workspace.
            workspace = _create_plain_workspace(agent_base, task.title)
    else:
        # Ad-hoc task (no project) → own dir under the agent workspace.
        workspace = _create_plain_workspace(agent_base, task.title)

    return workspace, worktree_path, has_repo


def _create_plain_workspace(base_path: str, task_title: str) -> str:
    """Plain directory without git under `<base_path>/<task-slug>/`.

    `base_path` is the agent workspace (host path, ADR-022), e.g.
    `${HOME_HOST}/.mc/workspaces/sparky`. The backend sees it identically
    via the `${HOME}/.mc` bind mount.
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


