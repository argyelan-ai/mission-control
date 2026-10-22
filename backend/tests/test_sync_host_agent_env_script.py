"""``scripts.sync_host_agent_env`` brings a NEW rendered key to a running host agent.

A host agent's agent.env is re-rendered only when MC flags it for a model sync
(runtime switch, drift). A deploy that adds a key to the rendered env — here
Hermes' ``MC_THINKING_TEMPLATE_KWARG`` — would otherwise never reach a Hermes
that keeps running on the same runtime. The script runs the same sync on
demand. Pinned:

1. ``--dry-run`` names the keys that would change and writes nothing;
2. the real run adds the key, keeps the token, and a second run changes nothing
   (no rewrite — a rewrite restarts the bridge through WatchPaths);
3. it refuses while the agent has a task in progress (unless forced), and for
   an unknown or non-host agent;
4. it prints key names, never values.
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.models.task import Task

KEY = "MC_THINKING_TEMPLATE_KWARG"
OLD_ENV = (
    "MC_AGENT_TOKEN='tok-secret'\n"
    "OPENAI_BASE_URL='http://192.0.2.10:8000/v1'\n"
    "OPENAI_MODEL='DeepSeek-V4.1-Flash-1017'\n"
    "MC_CONTEXT_WINDOW='1048576'\n"
)


@pytest.fixture()
async def hermes(async_session, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    env_path = tmp_path / ".mc" / "agents" / "hermes" / "agent.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(OLD_ENV)
    rt = Runtime(
        slug="dgx-spark-slot",
        display_name="Spark slot",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="DeepSeek-V4.1-Flash-1017",
        max_context_len=1_048_576,
        supports_reasoning=True,
        host_id=uuid.uuid4(),
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes", runtime_id=rt.id
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    return agent, env_path


@pytest.mark.asyncio
async def test_dry_run_names_the_new_key_and_writes_nothing(async_session, hermes):
    from scripts.sync_host_agent_env import sync_one

    _, env_path = hermes
    before = env_path.stat().st_mtime_ns

    changed = await sync_one(async_session, "hermes", dry_run=True)

    assert KEY in changed
    assert env_path.read_text() == OLD_ENV
    assert env_path.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_run_adds_the_key_keeps_the_token_and_is_idempotent(async_session, hermes):
    from scripts.sync_host_agent_env import sync_one

    _, env_path = hermes

    changed = await sync_one(async_session, "hermes", dry_run=False)

    text = env_path.read_text()
    assert changed == [KEY]
    assert f"{KEY}='enable_thinking'" in text
    assert "MC_AGENT_TOKEN='tok-secret'" in text

    before = env_path.stat().st_mtime_ns
    assert await sync_one(async_session, "hermes", dry_run=False) == []
    assert env_path.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_refuses_while_a_task_is_in_progress(async_session, hermes):
    from scripts.sync_host_agent_env import Refused, sync_one

    agent, env_path = hermes
    async_session.add(
        Task(title="t", status="in_progress", board_id=uuid.uuid4(), assigned_agent_id=agent.id)
    )
    await async_session.commit()

    with pytest.raises(Refused, match="in progress"):
        await sync_one(async_session, "hermes", dry_run=False)
    assert env_path.read_text() == OLD_ENV

    # A dry run reads only — allowed; --force overrides.
    assert KEY in await sync_one(async_session, "hermes", dry_run=True)
    assert KEY in await sync_one(async_session, "hermes", dry_run=False, force=True)


@pytest.mark.asyncio
async def test_refuses_unknown_and_non_host_agents(async_session, hermes):
    from scripts.sync_host_agent_env import Refused, sync_one

    with pytest.raises(Refused, match="no agent"):
        await sync_one(async_session, "nobody", dry_run=True)

    async_session.add(Agent(name="Rex", slug="rex", agent_runtime="cli-bridge", harness="omp"))
    await async_session.commit()
    with pytest.raises(Refused, match="not 'host'"):
        await sync_one(async_session, "rex", dry_run=True)


@pytest.mark.asyncio
async def test_main_prints_key_names_never_values(async_session, hermes, monkeypatch, capsys):
    import scripts.sync_host_agent_env as script

    class _Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(script, "AsyncSession", _Session)

    assert await script._main(["hermes", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert KEY in out
    assert "enable_thinking" not in out and "tok-secret" not in out

    assert await script._main(["nobody"]) == 2
