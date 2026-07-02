"""Phase 26 / Plan 26-08 GREEN tests for HERM-14 (F8): deliverable path
validator must accept Host-form paths AND FileResponse resolver must map
host-form paths back to the Docker-internal mount.

Replaces the RED stub from Plan 26-01.
"""

import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

# The host-form deliverable resolver falls back to settings.home_host, which
# defaults to the real Path.home() — match that dynamically instead of a
# hardcoded machine-specific path (the HOME_HOST monkeypatches below document
# intent but the resolver reads the cached settings singleton, not a live
# env lookup, so the expected value must equal the real Path.home()).
HOME = str(Path.home())


async def _create_test_data(session: AsyncSession):
    """Board + Agent (mit tasks:write scope) + Task in_progress."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    board = Board(id=board_id, name="Test Board", slug="test-26-08")
    session.add(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="Hermes",
        board_id=board_id,
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write", "knowledge:read", "knowledge:write"],
    )
    session.add(agent)

    task = Task(
        id=task_id,
        board_id=board_id,
        title="Hermes file deliverable",
        status="in_progress",
        assigned_agent_id=agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task, raw_token


# ── Validator: accept ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_host_path_deliverable_accepted(client: AsyncClient):
    """F8/HERM-14: ~/.mc/deliverables/<task_id>/foo.pdf accepted."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "file",
                "title": "Hermes Report",
                "path": f"~/.mc/deliverables/{task.id}/report.pdf",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_host_path_resolved_form_accepted(client: AsyncClient, monkeypatch):
    """F8/HERM-14: ${HOME_HOST}/.mc/deliverables/<task_id>/foo.pdf accepted."""
    monkeypatch.setenv("HOME_HOST", HOME)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "file",
                "title": "Hermes Report (resolved)",
                "path": f"{HOME}/.mc/deliverables/{task.id}/report.pdf",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_docker_path_still_accepted(client: AsyncClient):
    """No regression: existing /deliverables/<task_id>/ form still 201."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "file",
                "title": "Docker-form file",
                "path": f"/deliverables/{task.id}/asset.png",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


# ── Validator: traversal protection (both forms) ────────────────────────


@pytest.mark.asyncio
async def test_host_path_traversal_rejected(client: AsyncClient):
    """F8/HERM-14: traversal protection extends to host-form paths."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "deliverable_type": "file",
            "title": "Evil",
            "path": f"~/.mc/deliverables/{task.id}/../../etc/passwd",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_docker_path_traversal_still_rejected(client: AsyncClient):
    """No regression: existing Docker-form traversal still 422."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "deliverable_type": "file",
            "title": "Evil",
            "path": f"/deliverables/{task.id}/../../etc/passwd",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_shared_deliverables_path_accepted(client: AsyncClient):
    """/shared-deliverables/<task_id>/foo.pdf accepted — mc-playwright Sidecar Output."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "file",
                "title": "PDF via mc pdf (Sidecar)",
                "path": f"/shared-deliverables/{task.id}/bericht.pdf",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_shared_deliverables_traversal_rejected(client: AsyncClient):
    """/shared-deliverables/<task_id>/../../etc/passwd still 422."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "deliverable_type": "file",
            "title": "Evil shared",
            "path": f"/shared-deliverables/{task.id}/../../etc/passwd",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_arbitrary_host_path_rejected(client: AsyncClient):
    """No regression: arbitrary host paths outside /.mc/deliverables/<task_id>/ rejected."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "deliverable_type": "file",
            "title": "Bad",
            "path": "/home/agent/foo.pdf",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


# ── FileResponse resolver: host-form maps to Docker mount ───────────────


@pytest.mark.asyncio
async def test_fileresponse_host_path_resolver_maps_to_docker_form(monkeypatch):
    """The internal resolver `_resolve_deliverable_fs_path` maps host-form
    deliverable.path back to /deliverables/<slug>/... (the path the backend
    container actually has mounted via the deliverables volume).
    """
    from app.routers.tasks import _resolve_deliverable_fs_path
    monkeypatch.setenv("HOME_HOST", HOME)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, _token = await _create_test_data(s)

        from app.models.deliverable import TaskDeliverable
        # Tilde host form (Hermes-style)
        deliv_tilde = TaskDeliverable(
            task_id=task.id,
            agent_id=agent.id,
            deliverable_type="file",
            title="tilde",
            path=f"~/.mc/deliverables/{task.id}/report.pdf",
        )
        # Resolved host form
        deliv_resolved = TaskDeliverable(
            task_id=task.id,
            agent_id=agent.id,
            deliverable_type="file",
            title="resolved",
            path=f"{HOME}/.mc/deliverables/{task.id}/report.pdf",
        )
        # Docker form (regression baseline)
        deliv_docker = TaskDeliverable(
            task_id=task.id,
            agent_id=agent.id,
            deliverable_type="file",
            title="docker",
            path=f"/deliverables/{task.id}/asset.png",
        )
        s.add_all([deliv_tilde, deliv_resolved, deliv_docker])
        await s.commit()
        await s.refresh(deliv_tilde)
        await s.refresh(deliv_resolved)
        await s.refresh(deliv_docker)

        slug = agent.name.lower().replace(" ", "-")
        # Phase 26 UAT-found correction: host-runtime workers (Hermes) write
        # directly to ~/.mc/deliverables/<task_id>/<file> WITHOUT a slug
        # subfolder. After mapping host→docker the resolver must NOT inject
        # the slug (otherwise the path doesn't exist on disk).
        # Docker-form deliverables (from container agents) keep the slug
        # injection because that's how the backend's volume mount is structured.
        expected_tilde = f"/deliverables/{task.id}/report.pdf"
        expected_resolved = f"/deliverables/{task.id}/report.pdf"
        expected_docker = f"/deliverables/{slug}/{task.id}/asset.png"

        assert await _resolve_deliverable_fs_path(deliv_tilde, s) == expected_tilde
        assert await _resolve_deliverable_fs_path(deliv_resolved, s) == expected_resolved
        # No regression: Docker form still resolves with slug
        assert await _resolve_deliverable_fs_path(deliv_docker, s) == expected_docker


@pytest.mark.asyncio
async def test_fileresponse_host_path_resolver_target_host(monkeypatch):
    """target='host' for host-form returns the macOS Finder-reveal path."""
    from app.routers.tasks import _resolve_deliverable_fs_path
    monkeypatch.setenv("HOME_HOST", HOME)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, _token = await _create_test_data(s)

        from app.models.deliverable import TaskDeliverable
        deliv = TaskDeliverable(
            task_id=task.id,
            agent_id=agent.id,
            deliverable_type="file",
            title="tilde",
            path=f"~/.mc/deliverables/{task.id}/report.pdf",
        )
        s.add(deliv)
        await s.commit()
        await s.refresh(deliv)

        # Phase 26 UAT-found correction: host-form deliverables don't have a
        # slug subfolder on disk — the host path is the original `~/.mc/...`
        # form (where Hermes wrote it), not the slug-prefixed `.mc-deliverables`
        # form (which is for container agents).
        expected = f"{HOME}/.mc/deliverables/{task.id}/report.pdf"
        assert await _resolve_deliverable_fs_path(deliv, s, target="host") == expected
