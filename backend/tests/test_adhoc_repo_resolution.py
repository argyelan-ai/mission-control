"""Ad-hoc cards without a project must get a prepared git clone from the
real remote — never a bare non-git directory, never a `gh`-resolved short
name (task af914128, incident 2026-09-14).

Two gaps existed side by side:

1. `cli_bridge_runner._resolve_workspace` treated EVERY ad-hoc task (no
   task.project_id) as a plain non-git directory, regardless of
   task.repo_id — so a git-requiring agent dispatched onto an ad-hoc card
   never had a `.git` at all. Reproduced live: this very task's own
   dispatch left `task.workspace_path` as a plain `_tasks/<id>` dir with no
   `.git`, on an agent (`requires_git_workflow=True`) that should have
   gotten a real clone.
2. `task_context_builder.setup_git_workspace_for_dispatch`'s ad-hoc branch
   DID attempt a git clone, but swallowed any failure with a bare
   `logger.warning` and continued dispatch anyway — the exact silent
   fallback the project-with-repo branch right above it was hardened
   against after the 2026-04-19 incident (test_workspace_no_silent_fallback.py).

Fix: both call into the new `repo_registry.resolve_adhoc_repo_target`
precedence (task.repo_id -> board.default_project_id -> shared
`mc-workspace` scratch repo — never "no repo"), gated by
`agent.requires_git_workflow`, and both now hard-fail (blocker comment +
status=blocked + terminal-unassign) instead of dispatching into an
unprepared workspace.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.repo import Repo
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


def _board(**kw) -> Board:
    return Board(
        id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
        auto_dispatch_enabled=False, **kw,
    )


def _repo(**kw) -> Repo:
    full_name = kw.pop("full_name", f"argyelan-ai/tool-{uuid.uuid4().hex[:5]}")
    d = dict(full_name=full_name, url=f"https://github.com/{full_name}")
    d.update(kw)
    return Repo(**d)


async def _seed(*objs):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for o in objs:
            s.add(o)
        await s.commit()
        for o in objs:
            await s.refresh(o)


# ── 1. resolve_adhoc_repo_target: precedence ────────────────────────────

@pytest.mark.asyncio
async def test_resolve_adhoc_repo_prefers_explicit_repo_id():
    """task.repo_id (Maske, ADR-052) wins even when a board default exists."""
    from app.services.repo_registry import resolve_adhoc_repo_target

    project = Project(
        id=uuid.uuid4(), board_id=uuid.uuid4(), name="Board Default Project",
        github_repo_url="https://github.com/argyelan-ai/board-default.git",
    )
    board = _board(default_project_id=project.id)
    project.board_id = board.id
    explicit_repo = _repo(full_name="argyelan-ai/explicit-choice")
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Ad-hoc mit explizitem Repo",
        status="inbox", repo_id=explicit_repo.id,
    )
    await _seed(board, project, explicit_repo, task)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        loaded_task = await s.get(Task, task.id)
        url, slug = await resolve_adhoc_repo_target(s, loaded_task)

    assert url == "https://github.com/argyelan-ai/explicit-choice.git"
    assert slug == "explicit-choice"


@pytest.mark.asyncio
async def test_resolve_adhoc_repo_falls_back_to_board_default():
    """No repo_id -> board.default_project_id's repo, not the scratch repo."""
    from app.services.repo_registry import resolve_adhoc_repo_target

    board = _board()
    project = Project(
        id=uuid.uuid4(), board_id=board.id, name="MC Development",
        github_repo_url="https://github.com/argyelan-ai/mission-control.git",
    )
    board.default_project_id = project.id
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Ad-hoc ohne Repo-Auswahl",
        status="inbox",
    )
    await _seed(board, project, task)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        loaded_task = await s.get(Task, task.id)
        url, slug = await resolve_adhoc_repo_target(s, loaded_task)

    assert url == "https://github.com/argyelan-ai/mission-control.git"
    # slug comes from the *project's* name (slugify_project), same
    # convention the project-with-repo branch already uses — not the repo's
    # short name.
    assert slug == "mc-development"


@pytest.mark.asyncio
async def test_resolve_adhoc_repo_last_resort_is_shared_scratch_repo():
    """Neither repo_id nor board default -> ADHOC_REPO, never a crash/None."""
    from app.services.git_service import ADHOC_REPO
    from app.services.repo_registry import resolve_adhoc_repo_target

    board = _board()  # no default_project_id
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Voellig ad-hoc",
        status="inbox",
    )
    await _seed(board, task)

    with patch(
        "app.services.git_service.git_service.ensure_adhoc_repo",
        new_callable=AsyncMock,
        return_value="https://github.com/marknx/mc-workspace.git",
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            loaded_task = await s.get(Task, task.id)
            url, slug = await resolve_adhoc_repo_target(s, loaded_task)

    assert url == "https://github.com/marknx/mc-workspace.git"
    assert slug == ADHOC_REPO


# ── 2. cli_bridge_runner: ad-hoc git-requiring task gets a real clone ──

def _cli_bridge_agent(**kw) -> Agent:
    d = dict(
        id=uuid.uuid4(), name=f"Dev-{uuid.uuid4().hex[:6]}", role="developer",
        agent_runtime="cli-bridge", workspace_path="/tmp/mc-test-ws",
        requires_git_workflow=True,
    )
    d.update(kw)
    return Agent(**d)


@pytest.mark.asyncio
async def test_cli_bridge_adhoc_git_agent_gets_real_clone_not_plain_dir():
    """Root-cause regression guard: before the fix, `_resolve_workspace`
    called `_create_plain_workspace` unconditionally for any task without
    project_id — even for a git-requiring agent with task.repo_id set."""
    from app.services.cli_bridge_runner import _resolve_workspace

    board = _board()
    repo = _repo(full_name="argyelan-ai/mission-control")
    agent = _cli_bridge_agent()
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Ad-hoc Backend-Fix",
        status="inbox", repo_id=repo.id, assigned_agent_id=agent.id,
    )
    await _seed(board, repo, agent, task)

    with patch(
        "app.services.cli_bridge_runner.git_service.ensure_workspace",
        new_callable=AsyncMock, return_value="/tmp/mc-test-ws/mission-control",
    ) as mock_ensure, patch(
        "app.services.cli_bridge_runner.git_service.create_task_worktree",
        new_callable=AsyncMock, return_value="/tmp/mc-test-ws/mission-control/.worktrees/x",
    ), patch(
        "app.services.cli_bridge_runner.git_service.setup_git_identity",
        new_callable=AsyncMock,
    ), patch(
        "app.services.cli_bridge_runner._write_expected_remote",
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            loaded_task = await s.get(Task, task.id)
            loaded_agent = await s.get(Agent, agent.id)
            workspace, worktree_path, has_repo = await _resolve_workspace(
                loaded_task, loaded_agent, s,
            )

    assert has_repo is True, "git-requiring ad-hoc task must resolve has_repo=True"
    assert worktree_path is not None
    mock_ensure.assert_awaited_once()
    called_url = mock_ensure.await_args.args[1]
    assert called_url == "https://github.com/argyelan-ai/mission-control.git", (
        "must clone the full registry URL, never a short name"
    )


@pytest.mark.asyncio
async def test_cli_bridge_adhoc_non_coder_agent_still_gets_plain_dir():
    """Regression guard the other direction: requires_git_workflow=False
    (Research/Writing) must NOT be forced through a git clone — unchanged
    behavior, plain dir under the agent workspace."""
    from app.services.cli_bridge_runner import _resolve_workspace

    board = _board()
    agent = _cli_bridge_agent(role="researcher", requires_git_workflow=False)
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Ad-hoc Recherche",
        status="inbox", assigned_agent_id=agent.id,
    )
    await _seed(board, agent, task)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        loaded_task = await s.get(Task, task.id)
        loaded_agent = await s.get(Agent, agent.id)
        workspace, worktree_path, has_repo = await _resolve_workspace(
            loaded_task, loaded_agent, s,
        )

    assert has_repo is False
    assert worktree_path is None
    assert workspace.startswith(agent.workspace_path)


@pytest.mark.asyncio
async def test_cli_bridge_adhoc_git_clone_failure_blocks_task_hard():
    """Same hard-fail contract as the project-with-repo branch
    (test_workspace_no_silent_fallback.py) — a clone failure on a
    git-requiring ad-hoc task must block, not silently dispatch into an
    unprepared workspace."""
    from app.services.cli_bridge_runner import dispatch_to_cli_bridge

    board = _board()
    agent = _cli_bridge_agent()
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Ad-hoc, Klon schlaegt fehl",
        status="in_progress", assigned_agent_id=agent.id,
    )
    await _seed(board, agent, task)

    with patch(
        "app.services.git_service.git_service.ensure_adhoc_repo",
        new_callable=AsyncMock,
        return_value="https://github.com/marknx/mc-workspace.git",
    ), patch(
        "app.services.cli_bridge_runner.git_service.ensure_workspace",
        new_callable=AsyncMock,
        side_effect=RuntimeError("git clone failed: destination exists"),
    ), patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            loaded_agent = await s.get(Agent, agent.id)
            loaded_task = await s.get(Task, task.id)
            result = await dispatch_to_cli_bridge(loaded_agent, loaded_task, "prompt", s)

    assert result is False

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reloaded = await s.get(Task, task.id)
        assert reloaded.status == "blocked"
        from sqlmodel import select
        comments = list((await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all())
        blockers = [c for c in comments if c.comment_type == "blocker"]
        assert len(blockers) == 1
        assert "Workspace-Setup fehlgeschlagen" in blockers[0].content


# ── 3. task_context_builder: same hard-fail contract for other runtimes ─

@pytest.mark.asyncio
async def test_task_context_builder_adhoc_git_failure_blocks_not_warns():
    """Before the fix this branch caught every exception with a bare
    `logger.warning` and returned True — dispatch continued with
    task.workspace_path unset, Phase-C then created a plain non-git dir.
    Now it must apply the same blocker/terminal-unassign contract as the
    project-with-repo branch."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    board = _board()
    agent = _cli_bridge_agent(agent_runtime="host")
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Ad-hoc, Setup schlaegt fehl",
        status="inbox", assigned_agent_id=agent.id,
    )
    await _seed(board, agent, task)

    with patch(
        "app.services.git_service.git_service.ensure_adhoc_repo",
        new_callable=AsyncMock,
        side_effect=RuntimeError("GITHUB_OWNER is not configured"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            loaded_agent = await s.get(Agent, agent.id)
            loaded_task = await s.get(Task, task.id)
            result = await setup_git_workspace_for_dispatch(loaded_task, loaded_agent, s)

    assert result is False, (
        "ad-hoc git setup failure for a git-requiring agent must block "
        "dispatch, not silently continue"
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reloaded = await s.get(Task, task.id)
        assert reloaded.status == "blocked"
        from sqlmodel import select
        comments = list((await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all())
        assert any(c.comment_type == "blocker" for c in comments)


# ── 4. dispatch prompt names the real repo for ad-hoc git workspaces ───

def test_read_origin_remote_sync_reads_git_config(tmp_path):
    from app.services.dispatch_message_builder import _read_origin_remote_sync

    repo_dir = tmp_path / "mission-control"
    git_dir = repo_dir / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n'
        "\turl = https://github.com/argyelan-ai/mission-control.git\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    )

    assert _read_origin_remote_sync(str(repo_dir)) == (
        "https://github.com/argyelan-ai/mission-control.git"
    )


def test_read_origin_remote_sync_none_for_non_git_dir(tmp_path):
    from app.services.dispatch_message_builder import _read_origin_remote_sync

    plain_dir = tmp_path / "plain-workspace"
    plain_dir.mkdir()

    assert _read_origin_remote_sync(str(plain_dir)) is None
    assert _read_origin_remote_sync(None) is None


def test_dispatch_prompt_names_repo_for_adhoc_git_workspace(tmp_path):
    """Half 1 DoD: 'Der Agent sieht im Prompt, wo sein Repo liegt.' Before
    the fix, a task with workspace_path set but no project got a generic
    'push to GitHub' line with no repository named at all."""
    from app.services.dispatch_message_builder import _format_dispatch_message
    from app.services.task_context_builder import DispatchContext

    repo_dir = tmp_path / "mission-control"
    git_dir = repo_dir / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        '[remote "origin"]\n'
        "\turl = https://github.com/argyelan-ai/mission-control.git\n"
    )

    task = MagicMock()
    task.id = uuid.uuid4()
    task.board_id = uuid.uuid4()
    task.title = "Ad-hoc Backend-Fix"
    task.description = "Fix the repo-resolution gap."
    task.priority = "medium"
    task.parent_task_id = None
    task.status = "inbox"
    task.target_url = None
    task.credentials_encrypted = None
    task.credential_id = None
    task.help_request_from = None
    task.workspace_path = str(repo_dir)
    task.workspace_port = None
    task.acceptance_criteria = None
    task.intake_mode = None
    task.auto_reason = None
    task.dispatch_attempt_id = None

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Deployer"
    agent.role = "deployer"
    agent.is_board_lead = False
    agent.requires_git_workflow = True
    agent.rules_md = None
    agent.agent_runtime = "host"
    agent.workspace_path = str(repo_dir)

    ctx = DispatchContext(
        project=None, project_tags=[], dependency_context=None,
        semantic_memory_context=None, credentials_text=None,
        team_agents=[], child_tasks=[], feedback_context=None,
    )

    msg = _format_dispatch_message(task, agent, ctx)
    assert "argyelan-ai/mission-control.git" in msg, (
        f"dispatch prompt must name the resolved repo — got:\n{msg}"
    )
