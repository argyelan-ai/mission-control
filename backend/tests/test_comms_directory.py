"""Directory of reachable sessions (ADR-100 §3 item 2, §6 rule 2).

One service (`app.services.comms_directory`) owns "alive" and "listening";
`GET /api/v1/agent/comms/who` and `POST /api/v1/agent/comms/presence` only
read and feed it. The two words stay apart: a paused agent can be alive
(it still sends signs of life) without listening. After a backend or Redis
restart the state is "unknown" for 60 s, never "not alive".
"""
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.utils import utcnow
from tests.conftest import test_engine

WHO = "/api/v1/agent/comms/who"
PRESENCE = "/api/v1/agent/comms/presence"


async def _agent(name: str, *, scopes=None, last_seen_s: float | None = 5,
                 operational_mode: str = "active", comm_v2: bool = True,
                 archived: bool = False, run_state: str = "idle"):
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            name=name, slug=name.lower(), role="developer",
            scopes=["comms:talk"] if scopes is None else scopes,
            agent_token_hash=token_hash, operational_mode=operational_mode,
            comm_v2=comm_v2, run_state=run_state, harness="omp",
            last_seen_at=None if last_seen_s is None else utcnow() - timedelta(seconds=last_seen_s),
            archived_at=utcnow() if archived else None,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, {"Authorization": f"Bearer {raw}"}


@pytest.fixture
async def warm(monkeypatch, fake_redis):
    """The directory is past its 60 s start window (backend start and Redis
    epoch both long ago) unless a test says otherwise."""
    from app.services import comms_directory
    monkeypatch.setattr(comms_directory, "_PROCESS_STARTED", 0.0)
    await fake_redis.set(comms_directory.EPOCH_KEY, "0")


def _row(body, address):
    rows = [r for r in body["sessions"] if r["address"] == address]
    assert len(rows) == 1, body
    return rows[0]


def test_comms_talk_is_a_scope_but_not_a_default_for_workers():
    from app.scopes import ALL_SCOPES, AgentRole, Scope, get_default_scopes

    assert Scope.COMMS_TALK.value == "comms:talk"
    assert "comms:talk" in ALL_SCOPES
    for role in (AgentRole.DEVELOPER, AgentRole.REVIEWER, AgentRole.TESTER,
                 AgentRole.PLANNER, AgentRole.RESEARCHER, AgentRole.WRITER,
                 AgentRole.DEPLOYER):
        assert "comms:talk" not in get_default_scopes(role), role


@pytest.mark.asyncio
async def test_who_needs_comms_talk(client: AsyncClient, warm):
    _, headers = await _agent("NoTalk", scopes=["tasks:read"])
    assert (await client.get(WHO, headers=headers)).status_code == 403
    assert (await client.get(WHO)).status_code == 401


@pytest.mark.asyncio
async def test_who_lists_only_principals_that_may_talk(client: AsyncClient, warm):
    _, headers = await _agent("Asker")
    await _agent("Talker")
    await _agent("Silent", scopes=["tasks:read"])
    await _agent("Gone", archived=True)

    body = (await client.get(WHO, headers=headers)).json()
    addresses = {r["address"] for r in body["sessions"]}
    assert {"asker", "talker"} <= addresses
    assert "silent" not in addresses
    assert "gone" not in addresses


@pytest.mark.asyncio
async def test_alive_and_listening_are_kept_apart(client: AsyncClient, warm):
    """The critique's case: a paused agent still sends signs of life."""
    _, headers = await _agent("Asker")
    await _agent("Paused", operational_mode="paused", last_seen_s=10)
    await _agent("NoInbox", comm_v2=False, last_seen_s=10)
    await _agent("Stale", last_seen_s=600)

    body = (await client.get(WHO, headers=headers)).json()
    paused = _row(body, "paused")
    assert paused["alive"] == "yes"
    assert paused["listening"] is False
    assert _row(body, "noinbox")["listening"] is False
    assert _row(body, "stale")["alive"] == "no"
    assert _row(body, "asker")["alive"] == "yes"
    assert _row(body, "asker")["listening"] is True


@pytest.mark.asyncio
async def test_unknown_for_60s_after_start_instead_of_not_alive(client: AsyncClient, monkeypatch):
    import time
    from app.services import comms_directory

    monkeypatch.setattr(comms_directory, "_PROCESS_STARTED", time.time() - 10)
    _, headers = await _agent("Asker")
    await _agent("Stale", last_seen_s=600)

    body = (await client.get(WHO, headers=headers)).json()
    assert body["warming_up"] is True
    assert _row(body, "stale")["alive"] == "unknown"


@pytest.mark.asyncio
async def test_redis_restart_means_unknown_for_60s(client: AsyncClient, fake_redis, warm):
    """The epoch key vanishes with a Redis restart: the directory starts its
    60 s window again instead of calling everybody unreachable."""
    _, headers = await _agent("Asker")
    await _agent("Stale", last_seen_s=600)
    assert _row((await client.get(WHO, headers=headers)).json(), "stale")["alive"] == "no"

    await fake_redis.flushall()                        # "restart"
    assert _row((await client.get(WHO, headers=headers)).json(), "stale")["alive"] == "unknown"


@pytest.mark.asyncio
async def test_presence_sets_busy_task_and_accept(client: AsyncClient, warm):
    _, headers = await _agent("Worker")
    _, asker = await _agent("Asker")
    resp = await client.post(PRESENCE, headers=headers,
                             json={"listening": True, "busy": True, "task": "measuring", "accept": "hold"})
    assert resp.status_code == 200, resp.text
    row = _row((await client.get(WHO, headers=asker)).json(), "worker")
    assert row["busy"] is True
    assert row["task"] == "measuring"
    assert row["accept"] == "hold"


@pytest.mark.asyncio
async def test_presence_cannot_make_a_paused_agent_listen(client: AsyncClient, warm):
    _, headers = await _agent("Paused", operational_mode="paused")
    _, asker = await _agent("Asker")
    await client.post(PRESENCE, headers=headers, json={"listening": True})
    assert _row((await client.get(WHO, headers=asker)).json(), "paused")["listening"] is False


@pytest.mark.asyncio
async def test_presence_can_switch_listening_off(client: AsyncClient, warm):
    _, headers = await _agent("Worker")
    _, asker = await _agent("Asker")
    await client.post(PRESENCE, headers=headers, json={"listening": False})
    assert _row((await client.get(WHO, headers=asker)).json(), "worker")["listening"] is False


@pytest.mark.asyncio
async def test_presence_rejects_unknown_accept_mode(client: AsyncClient, warm):
    _, headers = await _agent("Worker")
    resp = await client.post(PRESENCE, headers=headers, json={"accept": "everything"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_presence_expires_after_90_seconds(client: AsyncClient, fake_redis, warm):
    from app.services import comms_directory

    agent, headers = await _agent("Worker")
    await client.post(PRESENCE, headers=headers, json={"busy": True})
    ttl = await fake_redis.ttl(comms_directory.presence_key("agent", agent.id))
    assert 0 < ttl <= 90


def test_only_the_directory_reads_or_writes_presence():
    """ADR-100 §6 rule 2: one presence source. No other backend module may
    touch the presence keys or decide "alive" on its own window."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    owner = app_dir / "services" / "comms_directory.py"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path == owner:
            continue
        text = path.read_text(encoding="utf-8")
        if "mc:comms:presence" in text or "ALIVE_WINDOW_S" in text or "_read_presence" in text:
            offenders.append(str(path.relative_to(app_dir)))
    assert not offenders, f"presence computed outside comms_directory: {offenders}"


@pytest.mark.asyncio
async def test_comms_talk_must_be_granted_explicitly(client: AsyncClient, warm):
    """Security review #11: an empty scope list means "all scopes" elsewhere,
    but talking across harnesses is opt-in per agent — never implied."""
    _, headers = await _agent("Implicit", scopes=[])
    _, asker = await _agent("Asker")
    assert (await client.get(WHO, headers=headers)).status_code == 403
    addresses = {r["address"] for r in (await client.get(WHO, headers=asker)).json()["sessions"]}
    assert "implicit" not in addresses


@pytest.mark.asyncio
async def test_an_archived_agent_cannot_talk(client: AsyncClient, warm):
    """Security review #9: archiving keeps the token, so the comms routes
    must check archived_at themselves."""
    _, headers = await _agent("Retired", archived=True)
    assert (await client.get(WHO, headers=headers)).status_code == 403
    assert (await client.post(PRESENCE, headers=headers, json={})).status_code == 403
