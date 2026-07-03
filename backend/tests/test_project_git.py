"""Tests for the Project Git-Info endpoint."""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from unittest.mock import AsyncMock, patch

from .conftest import test_engine
from app.models.board import Board, Project


@pytest.mark.asyncio
class TestProjectGitInfo:
    """GET /api/v1/boards/{board_id}/projects/{project_id}/git-info"""

    async def _create_board_and_project(self, **project_kwargs) -> tuple[uuid.UUID, uuid.UUID]:
        """Helper: create board + project in DB."""
        board_id = uuid.uuid4()
        project_id = uuid.uuid4()
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = Board(id=board_id, name="Test Board", slug=f"test-{board_id.hex[:8]}")
            s.add(board)
            await s.flush()
            project = Project(
                id=project_id,
                board_id=board_id,
                name="Test Project",
                **project_kwargs,
            )
            s.add(project)
            await s.commit()
        return board_id, project_id

    async def test_returns_401_without_auth(self, client: AsyncClient):
        board_id = uuid.uuid4()
        project_id = uuid.uuid4()
        resp = await client.get(f"/api/v1/boards/{board_id}/projects/{project_id}/git-info")
        assert resp.status_code == 401

    async def test_returns_404_for_missing_project(self, auth_client: AsyncClient):
        board_id = uuid.uuid4()
        project_id = uuid.uuid4()
        resp = await auth_client.get(f"/api/v1/boards/{board_id}/projects/{project_id}/git-info")
        assert resp.status_code == 404

    async def test_project_without_repo(self, auth_client: AsyncClient):
        """Project without github_repo_name returns has_repo=False."""
        board_id, project_id = await self._create_board_and_project()
        resp = await auth_client.get(f"/api/v1/boards/{board_id}/projects/{project_id}/git-info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_repo"] is False
        assert data["repo_name"] is None
        assert data["repo_url"] is None
        assert data["branches"] == []

    async def test_project_with_repo(self, auth_client: AsyncClient):
        """Project with github_repo_name returns repo info + branches."""
        board_id, project_id = await self._create_board_and_project(
            github_repo_name="test-owner/test-repo",
            github_repo_url="https://github.com/test-owner/test-repo.git",
        )
        mock_branches = ["main", "feature/login", "task/fix-bug"]
        with patch(
            "app.routers.project_git.GitService.list_repo_branches",
            new_callable=AsyncMock,
            return_value=mock_branches,
        ):
            resp = await auth_client.get(f"/api/v1/boards/{board_id}/projects/{project_id}/git-info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_repo"] is True
        assert data["repo_name"] == "test-owner/test-repo"
        assert data["repo_url"] == "https://github.com/test-owner/test-repo.git"
        assert data["branches"] == mock_branches
