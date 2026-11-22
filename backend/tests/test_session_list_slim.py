"""The chat list endpoints send only the fields the chat reads.

`/host-sessions/agents` and `/docker-sessions/agents` used to return
`agent.model_dump()`: every prompt document (soul, tools, memory) and the
agent's token hash, polled every 5-10 s by the chat page. Six agents were
about 130 KB gzip per poll (test team 21.11.2026: 1.8 MB per idle minute).

The rows now follow `SessionAgentRow`. The prompt documents and the token
hash must never come back; the fields the chat page, chat list, quick sheet
and chat view read must stay.
"""

import json
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.user import User

from .conftest import test_engine

# Never in a chat list row: prompt documents and the token hash.
FORBIDDEN = (
    "agent_token_hash",
    "soul_md",
    "soul_persona_md",
    "tools_md",
    "memory_md",
    "identity_md",
    "rules_md",
    "dispatch_config",
    "scopes",
)

# Read by the chat page / chat list / quick sheet / chat view (see the PR).
REQUIRED = (
    "id",
    "name",
    "slug",
    "emoji",
    "status",
    "run_state",
    "harness",
    "model",
    "agent_runtime",
    "runtime_id",
    "board_id",
    "current_task_id",
    "last_seen_at",
    "last_task_activity_at",
    "updated_at",
    "headless_chat",
    "tasks_in_own_session",
    "can_start_chat",  # "New chat" gates on it (chats slice S2)
)

BIG_DOC = "x" * 50_000


async def _seed(runtime: str) -> tuple[str, dict]:
    user_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:6]
    name = f"Slim{tag}"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Slim Board", slug=f"sb-{tag}")
        s.add(board)
        s.add(User(id=user_id, email=f"u-{tag}@mc.local", name="Op", role="admin", is_active=True))
        await s.commit()
        s.add(
            Agent(
                id=uuid.uuid4(),
                name=name,
                slug=name.lower(),
                emoji="x",
                role="developer",
                board_id=board.id,
                agent_runtime=runtime,
                soul_md=BIG_DOC,
                tools_md=BIG_DOC,
                memory_md=BIG_DOC,
                agent_token_hash="hash-must-not-leak",
            )
        )
        await s.commit()
    return name, {"Authorization": f"Bearer {create_access_token(str(user_id), 'admin')}"}


class _FakeProc:
    def __init__(self, out: bytes):
        self._out = out
        self.returncode = 0

    async def communicate(self):
        return self._out, b""


def _row(resp, name: str) -> dict:
    assert resp.status_code == 200, resp.text
    rows = [a for a in resp.json() if a["name"] == name]
    assert len(rows) == 1, resp.json()
    return rows[0]


def _assert_slim(row: dict) -> None:
    for key in FORBIDDEN:
        assert key not in row, f"{key} leaked into the chat list row"
    for key in REQUIRED:
        assert key in row, f"{key} missing from the chat list row"
    # Three 50 KB documents on the agent; the row stays small.
    assert len(json.dumps(row)) < 2_000


@pytest.mark.asyncio
async def test_host_session_rows_are_slim(client: AsyncClient):
    name, headers = await _seed("host")
    row = _row(await client.get("/api/v1/host-sessions/agents", headers=headers), name)
    _assert_slim(row)
    assert row["session_name"] == f"{name.lower()}-host"
    assert row["session_running"] is False
    assert row["emoji"] == "x"


@pytest.mark.asyncio
async def test_docker_session_rows_are_slim(client: AsyncClient):
    name, headers = await _seed("cli-bridge")
    listing = f"mc-agent-{name.lower()}\trunning\n".encode()

    async def fake_exec(*args, **kwargs):
        return _FakeProc(listing)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        resp = await client.get("/api/v1/docker-sessions/agents", headers=headers)
    row = _row(resp, name)
    _assert_slim(row)
    assert row["container_state"] == "running"
    assert row["headless_chat"] in (True, False)
