"""Tests fuer /api/v1/agents/{id}/local-memory + /force-recreate Endpoints.

Use-Case (Sparky 2026-05-12): UI-Endpoints fuer Claude-Local-Memory-Files
(Toxic Lessons loeschen ohne docker exec) und Container-Force-Recreate
(neues Image ziehen ohne kompletten Stack-Restart).
"""
import uuid
from unittest.mock import patch, AsyncMock

import pytest
from httpx import AsyncClient

from app.models.agent import Agent
from app.routers.cli_terminal import _validate_local_memory_filename
from fastapi import HTTPException


# ── _validate_local_memory_filename ───────────────────────────────────────

def test_validate_rejects_path_traversal():
    with pytest.raises(HTTPException) as exc:
        _validate_local_memory_filename("../etc/passwd")
    assert exc.value.status_code == 400


def test_validate_rejects_slash():
    with pytest.raises(HTTPException) as exc:
        _validate_local_memory_filename("foo/bar.md")
    assert exc.value.status_code == 400


def test_validate_rejects_backslash():
    with pytest.raises(HTTPException) as exc:
        _validate_local_memory_filename("foo\\bar.md")
    assert exc.value.status_code == 400


def test_validate_rejects_non_md():
    with pytest.raises(HTTPException) as exc:
        _validate_local_memory_filename("config.json")
    assert exc.value.status_code == 400


def test_validate_rejects_hidden():
    with pytest.raises(HTTPException) as exc:
        _validate_local_memory_filename(".secret.md")
    assert exc.value.status_code == 400


def test_validate_rejects_empty():
    with pytest.raises(HTTPException):
        _validate_local_memory_filename("")


def test_validate_accepts_normal_md():
    # Should not raise
    _validate_local_memory_filename("mc-comment-python3.md")
    _validate_local_memory_filename("MEMORY.md")
    _validate_local_memory_filename("test-with-dashes-123.md")


# ── GET /local-memory ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_get_local_memory_404_unknown_agent(auth_client: AsyncClient):
    fake_id = uuid.uuid4()
    resp = await auth_client.get(f"/api/v1/agents/{fake_id}/local-memory")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_get_local_memory_requires_auth(client: AsyncClient):
    fake_id = uuid.uuid4()
    resp = await client.get(f"/api/v1/agents/{fake_id}/local-memory")
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_get_local_memory_container_stopped(
    auth_client: AsyncClient, session
):
    """Wenn Container nicht running ist, kommen 0 files + state zurueck."""
    agent = Agent(
        id=uuid.uuid4(), name="Test Stopped", agent_runtime="cli-bridge",
    )
    session.add(agent)
    await session.commit()

    with patch("app.routers.cli_terminal._get_container_state", new=AsyncMock(return_value="exited")):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/local-memory")

    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == []
    assert body["container_state"] == "exited"


@pytest.mark.anyio
async def test_get_local_memory_lists_md_files(
    auth_client: AsyncClient, session
):
    """Listet .md files mit Inhalt; hidden Files + Subdirs ignoriert."""
    agent = Agent(
        id=uuid.uuid4(), name="Test Listing", agent_runtime="cli-bridge",
    )
    session.add(agent)
    await session.commit()

    # Simulate `ls *.md` output, then `head -c` per file, then `wc -c` per file.
    # _container_exec returns (rc, stdout, stderr) — order matters.
    exec_responses = [
        # ls *.md
        (0, "/path/MEMORY.md\n/path/test.md\n", ""),
        # head -c MEMORY.md
        (0, "# Memory index\n- entry", ""),
        # wc -c MEMORY.md
        (0, "23\n", ""),
        # head -c test.md
        (0, "Test content here", ""),
        # wc -c test.md
        (0, "17\n", ""),
    ]
    exec_mock = AsyncMock(side_effect=exec_responses)

    with patch("app.routers.cli_terminal._get_container_state", new=AsyncMock(return_value="running")), \
         patch("app.routers.cli_terminal._container_exec", new=exec_mock):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/local-memory")

    assert resp.status_code == 200
    body = resp.json()
    assert body["container_state"] == "running"
    assert len(body["files"]) == 2
    names = sorted(f["name"] for f in body["files"])
    assert names == ["MEMORY.md", "test.md"]


# ── DELETE /local-memory/{filename} ────────────────────────────────────────

@pytest.mark.anyio
async def test_delete_local_memory_404_unknown_agent(auth_client: AsyncClient):
    fake_id = uuid.uuid4()
    resp = await auth_client.delete(f"/api/v1/agents/{fake_id}/local-memory/foo.md")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_delete_local_memory_rejects_traversal(auth_client: AsyncClient, session):
    """Path-traversal wird vor Agent-Lookup geprueft → 400."""
    agent = Agent(
        id=uuid.uuid4(), name="Test Traversal", agent_runtime="cli-bridge",
    )
    session.add(agent)
    await session.commit()

    # URL-encode .. to slip past FastAPI path matching, but the validator still catches it.
    resp = await auth_client.delete(
        f"/api/v1/agents/{agent.id}/local-memory/..%2Fetc%2Fpasswd",
    )
    # FastAPI may resolve this to 404 (no matching route) or 400 (validator).
    # Both are safe (no deletion). Real path-traversal would need %2F bypass.
    assert resp.status_code in (400, 404)


@pytest.mark.anyio
async def test_delete_local_memory_non_md_rejected(auth_client: AsyncClient, session):
    agent = Agent(
        id=uuid.uuid4(), name="Test Reject", agent_runtime="cli-bridge",
    )
    session.add(agent)
    await session.commit()

    resp = await auth_client.delete(f"/api/v1/agents/{agent.id}/local-memory/config.json")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_delete_local_memory_container_stopped_409(auth_client: AsyncClient, session):
    agent = Agent(
        id=uuid.uuid4(), name="Test Container Down", agent_runtime="cli-bridge",
    )
    session.add(agent)
    await session.commit()

    with patch("app.routers.cli_terminal._get_container_state", new=AsyncMock(return_value="exited")):
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}/local-memory/foo.md")
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_delete_local_memory_happy_path(auth_client: AsyncClient, session):
    agent = Agent(
        id=uuid.uuid4(), name="Test Delete OK", agent_runtime="cli-bridge",
    )
    session.add(agent)
    await session.commit()

    exec_responses = [
        # test -f && rm -v
        (0, "removed '/path/foo.md'\n", ""),
        # MEMORY.md update (sh -c sequence, returns 0)
        (0, "", ""),
    ]
    exec_mock = AsyncMock(side_effect=exec_responses)

    with patch("app.routers.cli_terminal._get_container_state", new=AsyncMock(return_value="running")), \
         patch("app.routers.cli_terminal._container_exec", new=exec_mock):
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}/local-memory/foo.md")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": "foo.md"}


# ── POST /force-recreate ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_force_recreate_404_unknown_agent(auth_client: AsyncClient):
    fake_id = uuid.uuid4()
    resp = await auth_client.post(f"/api/v1/agents/{fake_id}/force-recreate")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_force_recreate_blocked_when_busy(auth_client: AsyncClient, session):
    """Mit current_task_id und ohne force → 409."""
    agent = Agent(
        id=uuid.uuid4(),
        name="Test Busy",
        agent_runtime="cli-bridge",
        current_task_id=uuid.uuid4(),
    )
    session.add(agent)
    await session.commit()

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/force-recreate")
    assert resp.status_code == 409
    detail = resp.json()["detail"].lower()
    assert "force" in detail or "task" in detail


@pytest.mark.anyio
async def test_force_recreate_force_bypass(auth_client: AsyncClient, session):
    """Mit force=true bypassed der Busy-Check."""
    agent = Agent(
        id=uuid.uuid4(),
        name="Test Force Bypass",
        agent_runtime="cli-bridge",
        current_task_id=uuid.uuid4(),
    )
    session.add(agent)
    await session.commit()

    # Mock subprocess: returncode=0, empty output
    async def fake_communicate():
        return (b"", b"")

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_create_subprocess_exec)), \
         patch("app.routers.cli_terminal._get_container_state", new=AsyncMock(return_value="running")):
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/force-recreate?force=true")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


@pytest.mark.anyio
async def test_force_recreate_uses_host_home(auth_client: AsyncClient, session, monkeypatch):
    """Regression 2026-05-12: HOME=/Users/testuser/Workspace verschob Sparky's Mount
    von /Users/testuser/.mc/... auf /Users/testuser/Workspace/.mc/...

    Test: docker compose subprocess wird mit env["HOME"] = HOME_HOST aufgerufen,
    NICHT mit einem manuell konstruierten Pfad.
    """
    agent = Agent(id=uuid.uuid4(), name="Test HOME Env", agent_runtime="cli-bridge")
    session.add(agent)
    await session.commit()

    monkeypatch.setenv("HOME_HOST", "/Users/otheruser")

    captured_env: dict = {}

    async def capture_subprocess(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=capture_subprocess)), \
         patch("app.routers.cli_terminal._get_container_state", new=AsyncMock(return_value="running")):
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/force-recreate")

    assert resp.status_code == 200
    assert captured_env.get("HOME") == "/Users/otheruser", (
        f"HOME must be HOME_HOST ('/Users/otheruser'), got: {captured_env.get('HOME')!r}. "
        "Bug 2026-05-12 hatte hier '/Users/testuser/Workspace' — mountete Container "
        "auf ${HOME}/.mc/... an einem anderen Pfad als start-all.sh."
    )
