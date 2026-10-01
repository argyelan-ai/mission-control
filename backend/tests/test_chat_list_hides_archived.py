"""The chat list (Sessions page) must not show archived agents.

Archiving an agent stops its container but keeps the row; the chat list read
every cli-bridge / host agent regardless and stayed full of archived agents
(live 01.10.2026, nine archived agents still listed).
"""

import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.user import User
from app.utils import utcnow

from .conftest import test_engine


async def _seed(runtime: str) -> tuple[str, str, dict]:
    user_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:6]
    active, archived = f"Active{tag}", f"Archived{tag}"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Chat Board", slug=f"cb-{tag}")
        s.add(board)
        s.add(User(id=user_id, email=f"u-{tag}@mc.local", name="Op", role="admin", is_active=True))
        await s.commit()
        s.add(Agent(id=uuid.uuid4(), name=active, role="developer", board_id=board.id, agent_runtime=runtime))
        s.add(Agent(id=uuid.uuid4(), name=archived, role="developer", board_id=board.id,
                    agent_runtime=runtime, archived_at=utcnow()))
        await s.commit()
    return active, archived, {"Authorization": f"Bearer {create_access_token(str(user_id), 'admin')}"}


class _FakeProc:
    def __init__(self, out: bytes):
        self._out = out
        self.returncode = 0

    async def communicate(self):
        return self._out, b""


@pytest.mark.asyncio
async def test_docker_session_list_skips_archived_agents(client: AsyncClient):
    active, archived, headers = await _seed("cli-bridge")
    listing = f"mc-agent-{active.lower()}\trunning\nmc-agent-{archived.lower()}\texited\n".encode()

    async def fake_exec(*args, **kwargs):
        return _FakeProc(listing)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        resp = await client.get("/api/v1/docker-sessions/agents", headers=headers)
    assert resp.status_code == 200, resp.text
    names = [a["name"] for a in resp.json()]
    assert active in names
    assert archived not in names


@pytest.mark.asyncio
async def test_host_session_list_skips_archived_agents(client: AsyncClient):
    active, archived, headers = await _seed("host")
    resp = await client.get("/api/v1/host-sessions/agents", headers=headers)
    assert resp.status_code == 200, resp.text
    names = [a["name"] for a in resp.json()]
    assert active in names
    assert archived not in names
