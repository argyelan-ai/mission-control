"""A host agent's ``agent.env`` is only rewritten when its content changes.

Live incident 2026-10-20: the host bridge of a singleton host agent restarted
about every 10.5 minutes, all day (two process starts per round). Its launchd
job watches ``agent.env`` (``WatchPaths``), so every write — even one with
the same bytes — restarts the bridge and cuts off running chats and turns.

The chain, each link checked against the live logs:

  1. The runtime watcher's stale-model net (``_sync_stale_agent_models``)
     counted an ARCHIVED cli-bridge agent that still carried the previous
     model as "stale". ``mark_agents_for_sync`` skips archived agents, so the
     archived row never got fixed — and the net flagged every other agent on
     the runtime again after each 600 s guard window.
  2. The host sync (``sync_host_agent_model``) then wrote ``agent.env`` with
     unchanged content → launchd restarted the bridge (start 1), and the
     adapter reload restarted it again (start 2).

Covered here, each with a counter-case:

  * the writer skips identical content (mtime and inode untouched) and writes
    changed content exactly once, in place (same inode — no tmp+rename, which
    would detach a single-file bind mount), owner-only,
  * a repeated host model sync leaves ``agent.env`` untouched, a real model
    change still rewrites it,
  * the watcher's net ignores archived agents, but still flags a live agent
    that carries an old model.

Test data: box-a / agent-a / org/neu. No network, no Docker.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_watcher import RuntimeWatcher
from app.utils import utcnow

OLD_NS = 1_000_000_000 * 1_000_000_000  # a fixed mtime far in the past (2001)


def _age(path: Path) -> None:
    """Pin the file's mtime to a known old value, so any write shows up."""
    os.utime(path, ns=(OLD_NS, OLD_NS))


# ── 1. The writer ────────────────────────────────────────────────────────────


def test_identical_content_is_not_rewritten(tmp_path):
    from app.services.agent_env_writer import write_env_file_if_changed

    path = tmp_path / "agent.env"
    path.write_text("A='1'\nB='2'\n")
    os.chmod(path, 0o600)
    _age(path)
    inode = path.stat().st_ino

    wrote = write_env_file_if_changed(path, "A='1'\nB='2'\n")

    assert wrote is False
    st = path.stat()
    assert st.st_mtime_ns == OLD_NS  # no write → launchd WatchPaths stays quiet
    assert st.st_ino == inode


def test_changed_content_is_written_exactly_once_in_place(tmp_path):
    from app.services.agent_env_writer import write_env_file_if_changed

    path = tmp_path / "agent.env"
    path.write_text("A='1'\n")
    os.chmod(path, 0o644)  # a loose mode gets tightened on the real write
    _age(path)
    inode = path.stat().st_ino

    assert write_env_file_if_changed(path, "A='2'\n") is True
    st = path.stat()
    assert path.read_text() == "A='2'\n"
    assert st.st_mtime_ns != OLD_NS
    # Same inode: a single-file bind mount (or any open watcher) keeps seeing
    # the file. A tmp+rename replace would hand out a new inode.
    assert st.st_ino == inode
    assert stat.S_IMODE(st.st_mode) == 0o600

    # The same content a second time is a no-op — exactly one write in total.
    _age(path)
    assert write_env_file_if_changed(path, "A='2'\n") is False
    assert path.stat().st_mtime_ns == OLD_NS


def test_missing_file_is_created_owner_only(tmp_path):
    from app.services.agent_env_writer import write_env_file_if_changed

    path = tmp_path / "agents" / "agent-a" / "agent.env"

    assert write_env_file_if_changed(path, "MC_AGENT_TOKEN='x'\n") is True
    assert path.read_text() == "MC_AGENT_TOKEN='x'\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── 2. The host model sync uses it ───────────────────────────────────────────


async def _host_agent(session: AsyncSession, home: Path) -> tuple[Agent, Runtime, Path]:
    env_dir = home / ".mc" / "agents" / "agent-a"
    env_dir.mkdir(parents=True)
    env_path = env_dir / "agent.env"
    # deliberately low-entropy dummy token — a realistic one trips gitleaks
    env_path.write_text("MC_AGENT_TOKEN='aa11aa11'\nOPENAI_MODEL='old'\n")
    os.chmod(env_path, 0o600)

    rt = Runtime(
        slug="box-a-vllm",
        display_name="box-a vLLM",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/neu",
        enabled=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    agent = Agent(
        name="agent-a",
        slug="agent-a",
        role="developer",
        agent_runtime="host",
        harness="openclaude",
        runtime_id=rt.id,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, rt, env_path


@pytest.mark.asyncio
async def test_repeated_host_model_sync_leaves_agent_env_untouched(
    async_session, tmp_path, monkeypatch
):
    from app.services.host_harness_adapter import sync_host_agent_model

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    agent, rt, env_path = await _host_agent(async_session, tmp_path)

    # First sync: the model really changes ('old' → 'org/neu') → one write.
    await sync_host_agent_model(agent, rt, session=async_session)
    first = env_path.read_text()
    assert "OPENAI_MODEL='org/neu'" in first
    assert "MC_AGENT_TOKEN='aa11aa11'" in first

    # Every later sync against the same runtime must not touch the file —
    # that write is what restarted the WatchPaths bridge every round.
    _age(env_path)
    inode = env_path.stat().st_ino
    for _ in range(3):
        await sync_host_agent_model(agent, rt, session=async_session)
    st = env_path.stat()
    assert st.st_mtime_ns == OLD_NS
    assert st.st_ino == inode
    assert env_path.read_text() == first


@pytest.mark.asyncio
async def test_host_model_sync_still_writes_a_real_model_change(
    async_session, tmp_path, monkeypatch
):
    """Counter-case: skipping identical writes must not swallow real changes."""
    from app.services.host_harness_adapter import sync_host_agent_model

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    agent, rt, env_path = await _host_agent(async_session, tmp_path)
    await sync_host_agent_model(agent, rt, session=async_session)
    _age(env_path)

    rt.model_identifier = "org/next"
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    await sync_host_agent_model(agent, rt, session=async_session)

    assert env_path.stat().st_mtime_ns != OLD_NS
    assert "OPENAI_MODEL='org/next'" in env_path.read_text()
    assert "MC_AGENT_TOKEN='aa11aa11'" in env_path.read_text()


# ── 3. The watcher's net ignores archived agents ─────────────────────────────


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis

    return _get


async def _slot_with_agents(
    session: AsyncSession, *, stale_archived: bool
) -> Runtime:
    host = Host(slug="box-a", display_name="BOX-A", kind="ssh", ssh_host="192.0.2.10")
    session.add(host)
    await session.commit()
    await session.refresh(host)
    slot = Runtime(
        slug="box-a-slot",
        host_id=host.id,
        display_name="BOX-A :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/neu",
        exclusive_memory=False,
        is_slot=True,
        enabled=True,
    )
    session.add(slot)
    await session.commit()
    await session.refresh(slot)
    # The live shape: a host agent and a cli-bridge agent already on the
    # served model, plus a cli-bridge agent that still carries the old one.
    session.add(
        Agent(name="agent-host", slug="agent-host", role="developer",
              agent_runtime="host", harness="hermes", model="org/neu",
              runtime_id=slot.id)
    )
    session.add(
        Agent(name="agent-a", slug="agent-a", role="developer",
              agent_runtime="cli-bridge", harness="omp", model="org/neu",
              runtime_id=slot.id)
    )
    session.add(
        Agent(name="agent-old", slug="agent-old", role="developer",
              agent_runtime="cli-bridge", harness="omp", model="org/alt",
              runtime_id=slot.id,
              archived_at=utcnow() if stale_archived else None)
    )
    await session.commit()
    return slot


async def _tick(session, fake_redis, *, flagged: int):
    watcher = RuntimeWatcher(interval=90)
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("org/neu", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)
    ), patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=flagged),
    ) as mock_mark:
        await watcher.tick(session=session)
    return mock_mark


@pytest.mark.asyncio
async def test_watcher_net_ignores_an_archived_agent_with_an_old_model(
    async_session, fake_redis
):
    """An archived agent is never synced, so it can never stop being "stale" —
    counting it re-flagged (and restarted) the live agents every round."""
    slot = await _slot_with_agents(async_session, stale_archived=True)

    mock_mark = await _tick(async_session, fake_redis, flagged=2)

    mock_mark.assert_not_awaited()
    assert await fake_redis.get(RedisKeys.runtime_agent_model_guard(slot.slug)) is None


@pytest.mark.asyncio
async def test_watcher_net_still_flags_a_live_agent_with_an_old_model(
    async_session, fake_redis
):
    """Counter-case: the same old model on a live agent is still caught."""
    slot = await _slot_with_agents(async_session, stale_archived=False)

    mock_mark = await _tick(async_session, fake_redis, flagged=3)

    assert mock_mark.await_count == 1
    assert mock_mark.await_args.args[1].id == slot.id
    assert await fake_redis.get(RedisKeys.runtime_agent_model_guard(slot.slug))
