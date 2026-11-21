"""The chat list is dated by the chat, not by the last task (user test 21.11.2026).

The drawer sorted and dated every agent by ``last_task_activity_at``: an
agent the operator chatted with today, whose last TASK was two weeks old,
sat under "Earlier" with the old date. The agent listings now carry
``last_chat_activity_at`` — the newest entry of the session the chat page
shows, falling back to the last task activity when there is no transcript.

Also here (same package): the comms directory called an agent "busy" from a
``run_state`` of ``running`` that its heartbeat had long stopped backing
(an agent switched off mid-turn kept "busy" for days).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token
from app.models.agent import Agent
from app.models.user import User
from app.utils import utcnow

from .conftest import test_engine


def _claude_session(home, slug: str, at: datetime) -> None:
    """One Claude Code chat session of a container agent, last entry ``at``."""
    d = home / ".mc" / "agents" / slug / "claude-config" / "projects" / "-home-agent"
    d.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "uuid": str(uuid.uuid4()), "timestamp": (at - timedelta(minutes=1)).isoformat(),
         "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": at.isoformat(),
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    ]
    (d / f"{uuid.uuid4()}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    from app.services import transcript_chat

    monkeypatch.setattr(transcript_chat, "_host_home", lambda: tmp_path)
    return tmp_path


async def _seed(runtime: str, tag: str) -> tuple[dict, str, str]:
    """Two agents: ``Chatty`` (old task, chat today) and ``Busy`` (task
    yesterday, no chat). Returns headers and both slugs."""
    user_id = uuid.uuid4()
    chatty, busy = f"chatty{tag}", f"busy{tag}"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email=f"u-{tag}@mc.local", name="Op", role="admin", is_active=True))
        s.add(Agent(id=uuid.uuid4(), name=chatty.capitalize(), slug=chatty, role="developer",
                    agent_runtime=runtime, harness="claude",
                    last_task_activity_at=utcnow() - timedelta(days=13)))
        s.add(Agent(id=uuid.uuid4(), name=busy.capitalize(), slug=busy, role="developer",
                    agent_runtime=runtime, harness="claude",
                    last_task_activity_at=utcnow() - timedelta(days=1)))
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(str(user_id), 'admin')}"}, chatty, busy


def _newest_first(rows: list[dict], slugs: set[str]) -> list[str]:
    mine = [r for r in rows if r.get("slug") in slugs]
    assert len(mine) == len(slugs), rows
    for r in mine:
        assert "last_chat_activity_at" in r, r
    mine.sort(key=lambda r: datetime.fromisoformat(r["last_chat_activity_at"]), reverse=True)
    return [r["slug"] for r in mine]


def test_last_chat_activity_is_the_newest_entry_of_the_chat_session(home):
    from app.services.transcript_chat import last_chat_activity

    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    _claude_session(home, "solo", at)

    class _A:
        slug = "solo"
        agent_runtime = "cli-bridge"
        harness = "claude"

    assert last_chat_activity(_A()) == at


def test_no_transcript_falls_back_to_the_last_task():
    from app.services.transcript_chat import chat_activity_fields

    class _A:
        last_task_activity_at = datetime(2026, 11, 8, 0, 13, tzinfo=timezone.utc)

    assert chat_activity_fields(_A(), None) == {"last_chat_activity_at": "2026-11-08T00:13:00+00:00"}
    later = datetime(2026, 11, 20, 17, 21, tzinfo=timezone.utc)
    assert chat_activity_fields(_A(), later) == {"last_chat_activity_at": later.isoformat()}


@pytest.mark.asyncio
async def test_agents_list_sorts_an_old_task_with_a_new_chat_first(client: AsyncClient, home):
    headers, chatty, busy = await _seed("cli-bridge", uuid.uuid4().hex[:6])
    _claude_session(home, chatty, datetime.now(timezone.utc) - timedelta(minutes=3))

    resp = await client.get("/api/v1/agents", headers=headers)
    assert resp.status_code == 200, resp.text
    assert _newest_first(resp.json(), {chatty, busy}) == [chatty, busy]


@pytest.mark.asyncio
async def test_docker_session_rows_carry_the_chat_time(client: AsyncClient, home):
    headers, chatty, busy = await _seed("cli-bridge", uuid.uuid4().hex[:6])
    _claude_session(home, chatty, datetime.now(timezone.utc) - timedelta(minutes=3))
    listing = "".join(f"mc-agent-{n}\trunning\n" for n in (chatty, busy)).encode()

    class _Proc:
        returncode = 0

        async def communicate(self):
            return listing, b""

    async def fake_exec(*args, **kwargs):
        return _Proc()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        resp = await client.get("/api/v1/docker-sessions/agents", headers=headers)
    assert resp.status_code == 200, resp.text
    assert _newest_first(resp.json(), {chatty, busy}) == [chatty, busy]


@pytest.mark.asyncio
async def test_host_session_rows_carry_the_chat_time(client: AsyncClient, home):
    # Host agents without a readable transcript keep their task date.
    headers, chatty, busy = await _seed("host", uuid.uuid4().hex[:6])
    resp = await client.get("/api/v1/host-sessions/agents", headers=headers)
    assert resp.status_code == 200, resp.text
    assert _newest_first(resp.json(), {chatty, busy}) == [busy, chatty]


# ── comms directory: busy needs a living heartbeat ──────────────────────────


class _Principal:
    kind = "agent"

    def __init__(self, agent):
        self.agent = agent
        self.id = str(agent.id)


def _jarvis_like(last_seen_s: float, run_state: str = "running") -> Agent:
    return Agent(id=uuid.uuid4(), name="Voice", slug="voice", role="developer",
                 run_state=run_state, comm_v2=True,
                 last_seen_at=utcnow() - timedelta(seconds=last_seen_s))


@pytest.mark.asyncio
async def test_a_dead_heartbeat_is_never_busy(fake_redis):
    from app.services import comms_directory

    # Switched off mid-turn 13 h ago: run_state still says running.
    st = await comms_directory.state_of(_Principal(_jarvis_like(13 * 3600)), redis=fake_redis, warming=False)
    assert st["alive"] == "no"
    assert st["busy"] is False


@pytest.mark.asyncio
async def test_a_running_turn_with_a_living_heartbeat_is_busy(fake_redis):
    from app.services import comms_directory

    st = await comms_directory.state_of(_Principal(_jarvis_like(10)), redis=fake_redis, warming=False)
    assert st["alive"] == "yes"
    assert st["busy"] is True


@pytest.mark.asyncio
async def test_a_running_turn_alive_only_through_presence_is_busy(fake_redis):
    """Critic 21.11.2026: alive takes the newer of Redis presence and the DB
    beat; busy must use that same sign of life, or an agent whose DB beat is
    older than 90 s but whose presence is fresh reads not busy and messages
    go into its running turn."""
    import json as _json

    from app.services import comms_directory

    agent = _jarvis_like(200)  # DB beat stale, presence below is fresh
    principal = _Principal(agent)
    await fake_redis.set(
        comms_directory.presence_key(principal.kind, principal.id),
        _json.dumps({"at": utcnow().isoformat(), "listening": True, "busy": False}),
    )
    st = await comms_directory.state_of(principal, redis=fake_redis, warming=False)
    assert st["alive"] == "yes"
    assert st["busy"] is True


@pytest.mark.asyncio
async def test_agents_list_never_relabels_a_dead_agent_as_idle(client: AsyncClient, home):
    """Critic 21.11.2026: /agents set run_state "idle" for an agent whose beat
    is gone — the task form shows that as "bereit" for a switched-off agent,
    and the single-agent GET still said "running". The listing keeps the
    stored value like the detail GET; "off" is the UI's call from last_seen
    (isAgentOff), never a fake "ready"."""
    tag = uuid.uuid4().hex[:6]
    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email=f"u-{tag}@mc.local", name="Op", role="admin", is_active=True))
        a = Agent(id=uuid.uuid4(), name=f"Off{tag}", slug=f"off{tag}", role="developer",
                  agent_runtime="host", harness="claude", run_state="running",
                  last_seen_at=utcnow() - timedelta(hours=13))
        s.add(a)
        await s.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(user_id), 'admin')}"}
    rows = (await client.get("/api/v1/agents", headers=headers)).json()
    listed = next(r for r in rows if r["slug"] == f"off{tag}")
    detail = (await client.get(f"/api/v1/agents/{a.id}", headers=headers)).json()
    assert listed["run_state"] != "idle"
    assert listed["run_state"] == detail["run_state"]
