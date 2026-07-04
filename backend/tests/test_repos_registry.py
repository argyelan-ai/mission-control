"""Repos registry (ADR-050): CRUD, import, project linking, rules injection."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board, Project
from app.models.repo import Repo

from tests.conftest import test_engine


async def _mk_board_project(repo: Repo | None = None, **project_kw) -> tuple[Board, Project]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        project = Project(board_id=board.id, name="Proj X", **project_kw)
        if repo is not None:
            project.repo_id = repo.id
            project.github_repo_name = repo.full_name
            project.github_repo_url = f"{repo.url}.git"
        s.add(project)
        await s.commit()
        await s.refresh(project)
        return board, project


async def _mk_repo(**kw) -> Repo:
    defaults = dict(
        full_name=f"owner/repo-{uuid.uuid4().hex[:6]}",
        url="https://github.com/owner/repo-x",
    )
    defaults.update(kw)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        repo = Repo(**defaults)
        s.add(repo)
        await s.commit()
        await s.refresh(repo)
        return repo


# ── CRUD ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_import_repo_registers_via_gh(auth_client: AsyncClient):
    meta = {
        "full_name": "owner/existing-repo",
        "url": "https://github.com/owner/existing-repo",
        "description": "Ein Repo",
        "visibility": "private",
        "default_branch": "main",
    }
    with patch(
        "app.services.git_service.GitService.fetch_repo_meta",
        new=AsyncMock(return_value=meta),
    ):
        r = await auth_client.post("/api/v1/repos", json={"full_name": "owner/existing-repo"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["full_name"] == "owner/existing-repo"
    assert body["source"] == "imported"
    assert body["linked_projects"] == []

    # Duplicate → 409
    with patch(
        "app.services.git_service.GitService.fetch_repo_meta",
        new=AsyncMock(return_value=meta),
    ):
        r2 = await auth_client.post("/api/v1/repos", json={"full_name": "owner/existing-repo"})
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_import_repo_rejects_bad_name(auth_client: AsyncClient):
    r = await auth_client.post("/api/v1/repos", json={"full_name": "kein-owner"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_rules_and_list(auth_client: AsyncClient):
    repo = await _mk_repo()
    r = await auth_client.patch(
        f"/api/v1/repos/{repo.id}",
        json={"rules_md": "- Tests immer mit `pytest -x`\n- Nie force-pushen"},
    )
    assert r.status_code == 200, r.text
    assert "pytest -x" in r.json()["rules_md"]

    r2 = await auth_client.get("/api/v1/repos")
    names = [x["full_name"] for x in r2.json()]
    assert repo.full_name in names


@pytest.mark.asyncio
async def test_inactive_repo_hidden_by_default(auth_client: AsyncClient):
    repo = await _mk_repo(is_active=False)
    r = await auth_client.get("/api/v1/repos")
    assert repo.full_name not in [x["full_name"] for x in r.json()]
    r2 = await auth_client.get("/api/v1/repos?include_inactive=true")
    assert repo.full_name in [x["full_name"] for x in r2.json()]


@pytest.mark.asyncio
async def test_delete_blocked_when_linked(auth_client: AsyncClient):
    repo = await _mk_repo()
    _, project = await _mk_board_project(repo=repo)

    r = await auth_client.delete(f"/api/v1/repos/{repo.id}")
    assert r.status_code == 409

    # Unlink → delete works (and never touches GitHub — no gh mock needed!)
    r2 = await auth_client.delete(f"/api/v1/repos/{repo.id}/link-project/{project.id}")
    assert r2.status_code == 200
    r3 = await auth_client.delete(f"/api/v1/repos/{repo.id}")
    assert r3.status_code == 204

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Project, project.id)
    assert fresh.repo_id is None
    assert fresh.github_repo_name is None
    assert fresh.github_repo_url is None


# ── Import-Kandidaten ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_import_candidates_filters_known_and_archived(auth_client: AsyncClient):
    known = await _mk_repo(full_name="owner/known")
    gh_repos = [
        {"full_name": "owner/known", "url": "u", "description": None,
         "visibility": "private", "default_branch": "main",
         "is_archived": False, "pushed_at": None},
        {"full_name": "owner/archived", "url": "u", "description": None,
         "visibility": "private", "default_branch": "main",
         "is_archived": True, "pushed_at": None},
        {"full_name": "owner/fresh", "url": "u", "description": None,
         "visibility": "public", "default_branch": "main",
         "is_archived": False, "pushed_at": None},
    ]
    with patch(
        "app.services.git_service.GitService.list_github_repos",
        new=AsyncMock(return_value=gh_repos),
    ):
        r = await auth_client.get("/api/v1/repos/import-candidates")
    assert r.status_code == 200
    names = [x["full_name"] for x in r.json()]
    assert names == ["owner/fresh"]
    assert known.full_name not in names


# ── Link-Sync-Kontrakt ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_link_project_syncs_legacy_fields(auth_client: AsyncClient):
    repo = await _mk_repo(full_name="owner/shared", url="https://github.com/owner/shared")
    _, project = await _mk_board_project()

    r = await auth_client.post(
        f"/api/v1/repos/{repo.id}/link-project", json={"project_id": str(project.id)}
    )
    assert r.status_code == 200, r.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Project, project.id)
    assert fresh.repo_id == repo.id
    assert fresh.github_repo_name == "owner/shared"
    assert fresh.github_repo_url == "https://github.com/owner/shared.git"


@pytest.mark.asyncio
async def test_init_repo_creates_registry_row(auth_client: AsyncClient):
    board, project = await _mk_board_project()

    with patch(
        "app.services.git_service.GitService.create_project_repo",
        new=AsyncMock(return_value="https://github.com/testowner/mc-proj-x.git"),
    ), patch("app.services.git_service.GITHUB_OWNER", "testowner"):
        r = await auth_client.post(
            f"/api/v1/boards/{board.id}/projects/{project.id}/init-repo"
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # Canonical owner/name — fixes the legacy "mc-slug"-only form that broke
    # every `gh --repo` call downstream.
    assert body["github_repo_name"] == "testowner/mc-proj-x"
    assert body["repo_id"]

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        repo = await s.get(Repo, uuid.UUID(body["repo_id"]))
    assert repo is not None
    assert repo.full_name == "testowner/mc-proj-x"
    assert repo.source == "mc"


# ── Regeln → Dispatch ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repo_rules_resolved_for_project():
    from app.services.repo_registry import get_repo_rules_for_project

    repo = await _mk_repo(rules_md="- Konvention: Conventional Commits")
    _, project = await _mk_board_project(repo=repo)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        merged = await s.merge(project)
        rules = await get_repo_rules_for_project(s, merged)
    assert rules is not None
    assert rules[0] == repo.full_name
    assert "Conventional Commits" in rules[1]


@pytest.mark.asyncio
async def test_repo_rules_fallback_via_legacy_name():
    """Projects linked only via the legacy string field still get rules."""
    from app.services.repo_registry import get_repo_rules_for_project

    repo = await _mk_repo(full_name="owner/legacy-linked", rules_md="Regel A")
    _, project = await _mk_board_project(
        github_repo_name="owner/legacy-linked",
        github_repo_url="https://github.com/owner/legacy-linked.git",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        merged = await s.merge(project)
        rules = await get_repo_rules_for_project(s, merged)
    assert rules is not None and rules[1] == "Regel A"


@pytest.mark.asyncio
async def test_no_rules_when_empty():
    from app.services.repo_registry import get_repo_rules_for_project

    repo = await _mk_repo(rules_md="   \n  ")
    _, project = await _mk_board_project(repo=repo)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        merged = await s.merge(project)
        assert await get_repo_rules_for_project(s, merged) is None


@pytest.mark.asyncio
async def test_dispatch_context_carries_repo_rules():
    """_load_dispatch_context stashes rules for the message builder."""
    from app.models.agent import Agent
    from app.models.task import Task
    from app.services.task_context_builder import _load_dispatch_context

    repo = await _mk_repo(rules_md="- Immer `npm run test:run` vor Review")
    board, project = await _mk_board_project(repo=repo)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(), name="Worker", role="Developer",
            board_id=board.id, agent_runtime="host", model="x",
        )
        task = Task(
            board_id=board.id, project_id=project.id,
            title="Feature bauen", status="inbox",
        )
        s.add_all([agent, task])
        await s.commit()
        await s.refresh(agent)
        await s.refresh(task)

        ctx = await _load_dispatch_context(task, agent, s)

    assert ctx.repo_rules_repo_name == repo.full_name
    assert "npm run test:run" in ctx.repo_rules_context
