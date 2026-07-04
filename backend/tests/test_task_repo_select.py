"""ADR-052: einheitliche Repo-Auswahl aus der Registry in der Task-Maske."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.repo import Repo
from app.models.task import Task

from tests.conftest import test_engine


async def _mk(session_objs: list):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for o in session_objs:
            s.add(o)
        await s.commit()
        for o in session_objs:
            await s.refresh(o)


def _board(**kw) -> Board:
    return Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
                 auto_dispatch_enabled=False, **kw)


def _repo(**kw) -> Repo:
    d = dict(full_name=f"acme/tool-{uuid.uuid4().hex[:5]}",
             url="https://github.com/acme/tool")
    d.update(kw)
    return Repo(**d)


# ── Task-Erstellung mit repo_id ───────────────────────────────────────

@pytest.mark.asyncio
async def test_create_task_with_registry_repo(auth_client: AsyncClient):
    board, repo = _board(), _repo()
    await _mk([board, repo])

    r = await auth_client.post(f"/api/v1/boards/{board.id}/tasks", json={
        "title": "Ad-hoc mit Registry-Repo", "repo_id": str(repo.id),
    })
    assert r.status_code in (200, 201), r.text
    assert r.json()["repo_id"] == str(repo.id)


@pytest.mark.asyncio
async def test_create_task_rejects_inactive_or_unknown_repo(auth_client: AsyncClient):
    board, inactive = _board(), _repo(is_active=False)
    await _mk([board, inactive])

    r1 = await auth_client.post(f"/api/v1/boards/{board.id}/tasks", json={
        "title": "x", "repo_id": str(inactive.id),
    })
    assert r1.status_code == 400
    r2 = await auth_client.post(f"/api/v1/boards/{board.id}/tasks", json={
        "title": "x", "repo_id": str(uuid.uuid4()),
    })
    assert r2.status_code == 400


# ── Workspace-Setup: Registry-Repo hat Vorrang ────────────────────────

def _ws_agent(board) -> Agent:
    return Agent(
        id=uuid.uuid4(), name="Worker", role="Developer", board_id=board.id,
        agent_runtime="cli-bridge", model="x", workspace_path="/tmp/ws",
    )


@pytest.mark.asyncio
async def test_dispatch_clones_registry_repo(fake_redis):
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    board, repo = _board(), _repo(full_name="acme/mytool",
                                  url="https://github.com/acme/mytool")
    agent = _ws_agent(board)
    task = Task(board_id=board.id, title="Fix bug", status="inbox", repo_id=None)
    await _mk([board, repo, agent, task])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.repo_id = repo.id
        s.add(t)
        await s.commit()
        await s.refresh(t)

        with patch("app.services.dispatch.is_backend_writable_path", return_value=True), \
             patch("app.services.git_service.git_service.ensure_workspace",
                   new=AsyncMock(return_value="/tmp/ws/mytool")) as ensure, \
             patch("app.services.git_service.git_service.create_task_worktree",
                   new=AsyncMock(return_value="/tmp/ws/mytool-wt")), \
             patch("app.services.git_service.git_service.setup_git_identity",
                   new=AsyncMock()):
            ok = await setup_git_workspace_for_dispatch(t, agent, s)

    assert ok is True
    ensure.assert_awaited_once()
    args = ensure.await_args.args
    assert args[1] == "https://github.com/acme/mytool.git"  # clone URL aus Registry
    assert args[2] == "mytool"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.workspace_path == "/tmp/ws/mytool-wt"


@pytest.mark.asyncio
async def test_dispatch_blocks_when_registry_repo_clone_fails(fake_redis):
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    board, repo = _board(), _repo()
    agent = _ws_agent(board)
    task = Task(board_id=board.id, title="Fix", status="inbox")
    await _mk([board, repo, agent, task])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.repo_id = repo.id
        s.add(t)
        await s.commit()
        await s.refresh(t)

        with patch("app.services.dispatch.is_backend_writable_path", return_value=True), \
             patch("app.services.git_service.git_service.ensure_workspace",
                   new=AsyncMock(side_effect=RuntimeError("clone denied"))), \
             patch("app.services.task_lifecycle.apply_terminal_unassign",
                   new=AsyncMock()):
            ok = await setup_git_workspace_for_dispatch(t, agent, s)

    assert ok is False
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.status == "blocked"


# ── Regeln: Task-Repo gewinnt ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_repo_rules_beat_project_rules():
    from app.services.repo_registry import get_repo_rules_for_task

    board = _board()
    proj_repo = _repo(rules_md="Projektregel")
    task_repo = _repo(rules_md="Taskregel")
    await _mk([board, proj_repo, task_repo])
    project = Project(board_id=board.id, name="P", repo_id=proj_repo.id,
                      github_repo_name=proj_repo.full_name)
    task = Task(board_id=board.id, title="t", status="inbox", repo_id=task_repo.id)
    await _mk([project, task])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.merge(task)
        p = await s.merge(project)
        rules = await get_repo_rules_for_task(s, t, p)
    assert rules is not None and rules[1] == "Taskregel"


@pytest.mark.asyncio
async def test_explicit_task_repo_without_rules_blocks_project_fallback():
    from app.services.repo_registry import get_repo_rules_for_task

    board = _board()
    proj_repo = _repo(rules_md="Projektregel")
    bare_repo = _repo(rules_md=None)
    await _mk([board, proj_repo, bare_repo])
    project = Project(board_id=board.id, name="P", repo_id=proj_repo.id,
                      github_repo_name=proj_repo.full_name)
    task = Task(board_id=board.id, title="t", status="inbox", repo_id=bare_repo.id)
    await _mk([project, task])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.merge(task)
        p = await s.merge(project)
        assert await get_repo_rules_for_task(s, t, p) is None


# ── git-info liefert Registry-Status ──────────────────────────────────

@pytest.mark.asyncio
async def test_git_info_exposes_rules_badge(auth_client: AsyncClient):
    board = _board()
    repo = _repo(full_name="acme/ruled", rules_md="- Regel 1")
    await _mk([board, repo])
    project = Project(
        board_id=board.id, name="P", repo_id=repo.id,
        github_repo_name="acme/ruled",
        github_repo_url="https://github.com/acme/ruled.git",
    )
    await _mk([project])

    with patch("app.services.git_service.GitService.list_repo_branches",
               new=AsyncMock(return_value=["main"])):
        r = await auth_client.get(
            f"/api/v1/boards/{board.id}/projects/{project.id}/git-info"
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_rules"] is True
    assert body["repo_id"] == str(repo.id)


# ── POST /repos/new ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_new_repo_registers(auth_client: AsyncClient):
    with patch("app.services.git_service.GITHUB_OWNER", "acme"), \
         patch("app.services.git_service.GitService.create_repo",
               new=AsyncMock(return_value="https://github.com/acme/fresh-tool.git")), \
         patch("app.services.git_service.GitService.init_repo_files",
               new=AsyncMock()) as init_mock:
        r = await auth_client.post("/api/v1/repos/new", json={"name": "Fresh Tool"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["full_name"] == "acme/fresh-tool"
    assert body["source"] == "mc"
    init_mock.assert_awaited_once()  # leeres Repo hätte keinen main-Branch

    # Doppelt anlegen → 409
    with patch("app.services.git_service.GITHUB_OWNER", "acme"):
        r2 = await auth_client.post("/api/v1/repos/new", json={"name": "Fresh Tool"})
    assert r2.status_code == 409
