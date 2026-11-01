"""GET /api/v1/agent/boxes/status — box facts an agent may read (ADR-100 §3 item 1).

Read-only. The answer is composed from the sources the runtimes page and the
switch lock already use: the runtime rows, the watcher's live snapshot in
Redis, the switch lock itself (``box_guard.switch_lock``) and head occupancy.
The one rule that matters most: a box is never "free" because an engine that
does not serve reports zero requests — that is exactly the hand-test case.
"""
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

URL = "/api/v1/agent/boxes/status"


async def _agent_headers(scopes: list[str]) -> dict[str, str]:
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="Boxes", slug=f"boxes-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)
        s.add(Agent(name="Reader", role="developer", scopes=scopes,
                    board_id=board.id, agent_token_hash=token_hash))
        await s.commit()
    return {"Authorization": f"Bearer {raw_token}"}


async def _host(slug: str, *, autostart: bool = False):
    from app.models.host import Host

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        host = Host(slug=slug, display_name=slug.upper(), kind="ssh", autostart_enabled=autostart)
        s.add(host)
        await s.commit()
        await s.refresh(host)
        return host


async def _runtime(slug: str, host_id, *, endpoint: str, enabled: bool = True,
                   model: str = "model-a", worker_id=None):
    from app.models.runtime import Runtime
    from app.models.runtime_host import RuntimeHost

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = Runtime(slug=slug, display_name=slug, runtime_type="vllm_docker",
                     endpoint=endpoint, host_id=host_id, model_identifier=model,
                     enabled=enabled)
        s.add(rt)
        await s.commit()
        await s.refresh(rt)
        if worker_id is not None:
            s.add(RuntimeHost(runtime_id=rt.id, host_id=worker_id, role="worker", node_rank=1))
            await s.commit()
        return rt


async def _live(fake_redis, slug: str, **fields):
    from app.redis_client import RedisKeys

    payload = {"last_probe_at": "2026-11-01T10:00:00+00:00", **fields}
    await fake_redis.set(RedisKeys.runtime_live(slug), json.dumps(payload))


@pytest.fixture
def engine_load(monkeypatch):
    """Requests in flight per endpoint, as the one engine prober reports them.

    The switch lock calls ``heads.engine.probe_running_requests``; faking it
    there means the route must go through the real lock to see these values.
    """
    loads: dict[str, tuple[int | None, str | None]] = {}

    async def fake_probe(endpoint):
        return loads.get(endpoint, (None, "unreachable"))

    from app.services.heads import engine
    monkeypatch.setattr(engine, "probe_running_requests", fake_probe)
    return loads


def _box(body, slug):
    matches = [b for b in body["boxes"] if b["slug"] == slug]
    assert len(matches) == 1, body
    return matches[0]


@pytest.mark.asyncio
async def test_needs_an_agent_token(client: AsyncClient):
    resp = await client.get(URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_needs_tasks_read_scope(client: AsyncClient):
    headers = await _agent_headers(["vault:read"])
    resp = await client.get(URL, headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_serving_engine_with_running_requests_is_busy_and_locked(
    client: AsyncClient, fake_redis, engine_load,
):
    host = await _host("box-a", autostart=True)
    await _runtime("rt-a", host.id, endpoint="http://box-a:8000/v1")
    await _live(fake_redis, "rt-a", reachable=True, served_model="model-a")
    engine_load["http://box-a:8000/v1"] = (2, None)

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    assert resp.status_code == 200, resp.text
    box = _box(resp.json(), "box-a")
    assert box["verdict"] == "busy"
    assert box["auto_recovery"] is True
    [rt] = box["runtimes"]
    assert rt["slug"] == "rt-a"
    assert rt["model"] == "model-a"
    assert rt["served_model"] == "model-a"
    assert rt["engine"] == "serving"
    assert rt["running_requests"] == 2
    assert rt["switch_lock"] == "locked"


@pytest.mark.asyncio
async def test_serving_idle_engine_is_free(client: AsyncClient, fake_redis, engine_load):
    host = await _host("box-b", autostart=True)
    await _runtime("rt-b", host.id, endpoint="http://box-b:8000/v1")
    await _live(fake_redis, "rt-b", reachable=True, served_model="model-a")
    engine_load["http://box-b:8000/v1"] = (0, None)

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    box = _box(resp.json(), "box-b")
    assert box["verdict"] == "free"
    assert box["runtimes"][0]["switch_lock"] == "open"
    assert box["runtimes"][0]["running_requests"] == 0


@pytest.mark.asyncio
async def test_stopped_engine_is_never_free_even_with_zero_requests(
    client: AsyncClient, fake_redis, engine_load,
):
    """Hand test: engine stopped by hand, auto-recovery off. The prober may
    even say 0 — the box is still not free, and the answer says why."""
    host = await _host("box-c", autostart=False)
    await _runtime("rt-c", host.id, endpoint="http://box-c:8000/v1")
    await _live(fake_redis, "rt-c", reachable=False, served_model=None)
    engine_load["http://box-c:8000/v1"] = (0, None)

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    box = _box(resp.json(), "box-c")
    assert box["verdict"] == "not_serving"
    assert box["auto_recovery"] is False
    assert box["runtimes"][0]["engine"] == "not_serving"
    assert "manual_test_possible" in box["reasons"]


@pytest.mark.asyncio
async def test_switch_in_progress_is_busy(client: AsyncClient, fake_redis, engine_load):
    host = await _host("box-d", autostart=True)
    await _runtime("rt-d", host.id, endpoint="http://box-d:8000/v1")
    await _live(fake_redis, "rt-d", reachable=False, served_model=None,
                status="switching", phase="starting", switch_source="recipe")

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    box = _box(resp.json(), "box-d")
    assert box["verdict"] == "busy"
    rt = box["runtimes"][0]
    assert rt["engine"] == "switching"
    assert rt["switching"] == {"phase": "starting", "source": "recipe"}


@pytest.mark.asyncio
async def test_unreadable_load_is_unknown_not_free(client: AsyncClient, fake_redis, engine_load):
    host = await _host("box-e", autostart=True)
    await _runtime("rt-e", host.id, endpoint="http://box-e:8000/v1")
    await _live(fake_redis, "rt-e", reachable=True, served_model="model-a")
    engine_load["http://box-e:8000/v1"] = (None, "no running-requests metric")

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    box = _box(resp.json(), "box-e")
    assert box["verdict"] == "unknown"
    rt = box["runtimes"][0]
    assert rt["switch_lock"] == "unknown"
    assert rt["running_requests"] is None
    assert rt["load_unknown_reason"] == "no running-requests metric"


@pytest.mark.asyncio
async def test_no_live_snapshot_means_engine_unknown(client: AsyncClient, fake_redis, engine_load):
    host = await _host("box-f", autostart=True)
    await _runtime("rt-f", host.id, endpoint="http://box-f:8000/v1")
    engine_load["http://box-f:8000/v1"] = (0, None)

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    box = _box(resp.json(), "box-f")
    assert box["runtimes"][0]["engine"] == "unknown"
    assert box["verdict"] == "unknown"


@pytest.mark.asyncio
async def test_disabled_and_cloud_runtimes_are_left_out(client: AsyncClient, fake_redis, engine_load):
    """Same rows as the runtimes page: enabled, bound to a box."""
    host = await _host("box-g", autostart=True)
    await _runtime("rt-g-off", host.id, endpoint="http://box-g:8001/v1", enabled=False)
    await _runtime("rt-cloud", None, endpoint="https://cloud.example/v1")

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    slugs = {rt["slug"] for b in resp.json()["boxes"] for rt in b["runtimes"]}
    assert "rt-g-off" not in slugs
    assert "rt-cloud" not in slugs
    assert all(b["slug"] != "box-g" for b in resp.json()["boxes"])


@pytest.mark.asyncio
async def test_two_box_runtime_holds_the_worker_box_too(client: AsyncClient, fake_redis, engine_load):
    head = await _host("box-h", autostart=True)
    worker = await _host("box-w", autostart=True)
    await _runtime("rt-duo", head.id, endpoint="http://box-h:8000/v1", worker_id=worker.id)
    await _live(fake_redis, "rt-duo", reachable=True, served_model="model-a")
    engine_load["http://box-h:8000/v1"] = (1, None)

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    body = resp.json()
    assert _box(body, "box-h")["runtimes"][0]["role"] == "head"
    w = _box(body, "box-w")
    assert w["runtimes"][0]["slug"] == "rt-duo"
    assert w["runtimes"][0]["role"] == "worker"
    assert w["verdict"] == "busy"


@pytest.mark.asyncio
async def test_head_on_box_makes_it_busy(client: AsyncClient, fake_redis, engine_load, monkeypatch):
    host = await _host("box-i", autostart=True)
    await _runtime("rt-i", host.id, endpoint="http://box-i:8000/v1")
    await _live(fake_redis, "rt-i", reachable=True, served_model="model-a")
    engine_load["http://box-i:8000/v1"] = (0, None)

    from app.services.heads import box_guard
    monkeypatch.setattr(box_guard, "heads_on", lambda ids: [
        {"run_id": "run-1", "task_id": "t-1", "title": "job", "harness": "omp",
         "runtime_slug": "rt-i", "since": None}
    ] if str(host.id) in {str(i) for i in ids} else [])

    resp = await client.get(URL, headers=await _agent_headers(["tasks:read"]))
    box = _box(resp.json(), "box-i")
    assert box["verdict"] == "busy"
    assert box["head"]["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_reading_writes_nothing(client: AsyncClient, fake_redis, engine_load, session: AsyncSession):
    """Read-only: no row and no Redis key changes."""
    from app.models.host import Host

    host = await _host("box-j", autostart=False)
    await _runtime("rt-j", host.id, endpoint="http://box-j:8000/v1")
    await _live(fake_redis, "rt-j", reachable=True, served_model="model-a")
    engine_load["http://box-j:8000/v1"] = (0, None)
    headers = await _agent_headers(["tasks:read"])
    # The agent-auth cache key is written by the login dependency, not by
    # this route — prime it with a first call, then compare a second one.
    assert (await client.get(URL, headers=headers)).status_code == 200
    before = {k: await fake_redis.get(k) for k in await fake_redis.keys("mc:runtime*")}
    keys_before = sorted(await fake_redis.keys("*"))

    resp = await client.get(URL, headers=headers)
    assert resp.status_code == 200
    assert sorted(await fake_redis.keys("*")) == keys_before
    assert {k: await fake_redis.get(k) for k in await fake_redis.keys("mc:runtime*")} == before
    refreshed = await session.get(Host, host.id)
    assert refreshed.autostart_enabled is False
