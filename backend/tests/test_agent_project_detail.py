"""GET /api/v1/agent/projects/{project_id} — read-only project + phases for
an agent (scope project:read, own board only).

TOOLS.md has advertised this route for agents with project:read for a long
time (services/tools_md_builder.py), but it did not exist; the MCP server in
agent mode needs it so it never has to fall back to the operator route.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup(scopes: list[str], project_on_own_board: bool = True):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board, Project
    from app.models.project_phase import ProjectPhase

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        own = Board(id=uuid.uuid4(), name="Own", slug=f"own-{uuid.uuid4().hex[:6]}")
        other = Board(id=uuid.uuid4(), name="Other", slug=f"other-{uuid.uuid4().hex[:6]}")
        s.add(own)
        s.add(other)
        raw, token_hash = generate_agent_token()
        agent = Agent(id=uuid.uuid4(), name="Worker", board_id=own.id,
                      agent_token_hash=token_hash, scopes=scopes)
        s.add(agent)
        project = Project(
            id=uuid.uuid4(),
            board_id=own.id if project_on_own_board else other.id,
            name="Proj", status="active", briefing_doc="BRIEF",
            project_config={"internal": "not-for-agents"},
        )
        s.add(project)
        await s.commit()
        s.add(ProjectPhase(project_id=project.id, title="Second", order=1, status="pending"))
        s.add(ProjectPhase(project_id=project.id, title="First", order=0, status="active"))
        await s.commit()
        return project, raw


@pytest.mark.asyncio
async def test_agent_reads_own_board_project_with_phases(client: AsyncClient):
    project, token = await _setup(["tasks:read", "project:read"])
    resp = await client.get(f"/api/v1/agent/projects/{project.id}",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(project.id)
    assert body["briefing_doc"] == "BRIEF"
    assert [p["title"] for p in body["phases"]] == ["First", "Second"]
    assert body["phases"][0]["status"] == "active"
    assert "project_config" not in body


@pytest.mark.asyncio
async def test_agent_cannot_read_other_board_project(client: AsyncClient):
    project, token = await _setup(["tasks:read", "project:read"], project_on_own_board=False)
    resp = await client.get(f"/api/v1/agent/projects/{project.id}",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_without_project_read_gets_403(client: AsyncClient):
    project, token = await _setup(["tasks:read"])
    resp = await client.get(f"/api/v1/agent/projects/{project.id}",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "project:read" in resp.text


@pytest.mark.asyncio
async def test_agent_unknown_project_404(client: AsyncClient):
    _, token = await _setup(["tasks:read", "project:read"])
    resp = await client.get(f"/api/v1/agent/projects/{uuid.uuid4()}",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agent_project_route_rejects_missing_token(client: AsyncClient):
    resp = await client.get(f"/api/v1/agent/projects/{uuid.uuid4()}")
    assert resp.status_code in (401, 403)
