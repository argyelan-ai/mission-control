"""Agent Git Workflow Handlers — extracted from agent_scoped PATCH endpoint (REF-02).

These functions encapsulate the git-side-effect blocks that were previously
inline at agent_scoped.py:3047-3061 (worktree cleanup) + 3127-3169 (PR creation
on review handoff) + 3172-3232 (PR merge on done). They are CALLED from the
PATCH endpoint at the same trigger points as before — same call order, just
behind a function boundary (Option A from research Pitfall 5).

NOT a router file: no @router decorators. Pure handler library.

Pitfall H Contract: The string "PR erstellt:" in handle_review_pr_creation
is greppable-contract for handle_done_pr_merge (TaskComment.content.like("%PR erstellt:%")).
DO NOT change wording.
"""
from __future__ import annotations

import logging
import os
import re

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Project
from app.models.task import Task, TaskComment

logger = logging.getLogger("mc.agent_git")


async def handle_worktree_cleanup(
    session: AsyncSession,
    task: Task,
    agent: Agent,
    new_status: str,
) -> None:
    """VERBATIM relocation of agent_scoped.py:3047-3061 (Worktree Cleanup bei done/failed).

    Best-effort cleanup of task worktree on terminal transitions (done, failed).
    On `failed`, files are kept for debugging via `keep_on_fail=True` so a
    human can post-mortem-inspect the worktree state. On `done`, the worktree
    is fully removed (Bundle 4 disk-hygiene).

    Triggered from agent_scoped.py PATCH endpoint after the status transition
    has already been written to the Task row but BEFORE the
    `task.status_changed` Activity event is emitted (preserves original call
    order — Pitfall 5 Option A).

    Args:
        session: SQLModel async session (passed for symmetry with other handlers
            and to allow future Project lookups; this handler currently uses it
            via `session.get(Project, ...)`).
        task: Task being transitioned. Cleanup is a no-op when
            `task.workspace_path` or `task.project_id` is unset.
        agent: Acting agent. `agent.workspace_path` is required to locate the
            main repo dir.
        new_status: Resolved status ("done" or "failed"). Other values are a
            no-op to match the original guard at line 3048.

    Pitfall: All git_service imports are lazy (Pattern S2) — the dispatch
    cycle pulls git_service at module load, so importing it here at module
    top would re-introduce the cycle.
    """
    # ── Worktree Cleanup bei done/failed (Bundle 4) ──────────────
    if task.workspace_path and task.project_id and new_status in ("done", "failed"):
        try:
            from app.services.git_service import git_service, slugify_project
            _project = await session.get(Project, task.project_id) if task.project_id else None
            if _project and _project.github_repo_url and agent.workspace_path:
                _main_repo = os.path.join(agent.workspace_path, slugify_project(_project.name))
                _keep = new_status == "failed"  # failed: keep files for debugging
                await git_service.cleanup_worktree(_main_repo, task.workspace_path, keep_on_fail=_keep)
                logger.info(
                    "Worktree cleanup: task=%s status=%s keep=%s path=%s",
                    task.id, new_status, _keep, task.workspace_path,
                )
        except Exception as e:
            logger.warning("Worktree cleanup fehlgeschlagen: %s", e)


async def handle_review_pr_creation(
    session: AsyncSession,
    task: Task,
    agent: Agent,
) -> str | None:
    """VERBATIM relocation of agent_scoped.py:3127-3166 (Developer→review PR push).

    Returns the PR URL on success, None on any failure (graceful — does not raise).
    Persists a `progress`-type TaskComment with the PR URL (preserves the
    `PR erstellt: ...` marker that handle_done_pr_merge greps for).

    Pitfall H: marker string `"PR erstellt:"` is contract — DO NOT change.
    """
    # Developer → review: git push + create PR
    pr_url = None
    if task.project_id:
        try:
            from app.services.git_service import git_service, slugify_project
            project = await session.get(Project, task.project_id)
            if project and project.github_repo_url and agent.workspace_path:
                project_slug = slugify_project(project.name)
                task_slug = slugify_project(task.title)
                project_dir = os.path.join(agent.workspace_path, project_slug)
                branch = f"task/{task_slug}"
                # Alles committen + pushen
                try:
                    await git_service._run_cmd(
                        "git", "add", ".", cwd=project_dir,
                    )
                    await git_service._run_cmd(
                        "git", "commit", "-m", f"feat: {task.title}",
                        cwd=project_dir,
                    )
                except RuntimeError:
                    pass  # Nichts zu committen ist OK
                await git_service.push_branch(project_dir, branch)
                pr_url = await git_service.create_pr(
                    project_dir,
                    title=task.title,
                    body=f"Task: {task.title}\n\nBoard: {task.board_id}\nAgent: {agent.name}",
                )
                # Store PR URL as a comment
                pr_comment = TaskComment(
                    task_id=task.id,
                    author_type="system",
                    comment_type="progress",
                    content=f"**PR erstellt:** {pr_url}",
                )
                session.add(pr_comment)
                await session.commit()
        except Exception as e:
            logger.warning("PR-Erstellung fehlgeschlagen: %s", e)

    return pr_url


async def handle_done_pr_merge(
    session: AsyncSession,
    task: Task,
    agent: Agent,
) -> None:
    """VERBATIM relocation of agent_scoped.py:3172-3232 (review/user_test→done PR merge).

    Greps the PR URL out of TaskComment with `PR erstellt:` marker, then
    `gh pr merge --squash --delete-branch`. Worktree cleanup is best-effort.
    """
    # Merge the PR on real completion (after review or after the test gate)
    if task.project_id:
        try:
            from app.services.git_service import git_service, slugify_project
            project = await session.get(Project, task.project_id)
            if project and project.github_repo_url:
                # Extract PR number from comment
                pr_result = await session.exec(
                    select(TaskComment)
                    .where(
                        TaskComment.task_id == task.id,
                        TaskComment.content.like("%PR erstellt:%"),
                    )
                    .order_by(TaskComment.created_at.desc())
                    .limit(1)
                )
                pr_comment = pr_result.first()
                if pr_comment:
                    pr_match = re.search(r"/pull/(\d+)", pr_comment.content)
                    if pr_match:
                        pr_number = int(pr_match.group(1))
                        project_slug = slugify_project(project.name)
                        # Use reviewer or developer workspace
                        reviewer_dir = os.path.join(
                            agent.workspace_path or "", project_slug,
                        )
                        if os.path.isdir(reviewer_dir):
                            await git_service.merge_pr(reviewer_dir, pr_number)
                        else:
                            # Fallback: gh CLI without cwd (uses global auth)
                            await git_service._run_cmd(
                                "gh", "pr", "merge", str(pr_number),
                                "--repo", project.github_repo_name,
                                "--squash", "--delete-branch",
                            )
                        logger.info("PR #%d gemerged fuer Task '%s'", pr_number, task.title)
                        # Workstream B3: worktree cleanup post-merge.
                        # Non-fatal — orphan worktrees waste disk but
                        # don't block anything. Requires access to the
                        # main repo dir (developer workspace), not the
                        # reviewer's; fall back gracefully if missing.
                        try:
                            task_slug = slugify_project(task.title)
                            developer_dir = reviewer_dir
                            if task.assigned_agent_id and task.assigned_agent_id != agent.id:
                                dev = await session.get(Agent, task.assigned_agent_id)
                                if dev and dev.workspace_path:
                                    developer_dir = os.path.join(
                                        dev.workspace_path, project_slug,
                                    )
                            if os.path.isdir(developer_dir):
                                await git_service.cleanup_task_worktree(
                                    developer_dir, task_slug,
                                )
                        except Exception as cleanup_exc:
                            logger.info(
                                "Worktree cleanup skipped for '%s': %s",
                                task.title, cleanup_exc,
                            )
        except Exception as e:
            logger.warning("PR-Merge fehlgeschlagen: %s", e)
