"""A host agent's ``agent.env`` is only rewritten when its content changes.

Live incident 2026-10-20: the host bridge of a singleton host agent restarted
about every 10.5 minutes, all day (two process starts per round). Its launchd
job watches ``agent.env`` (``WatchPaths``), so every write — even one with
the same bytes — restarts the bridge and cuts off running chats and turns.

The chain, each link checked against the live logs:

  1. The runtime watcher's stale-model net (``_sync_stale_agent_models``)
     counted an ARCHIVED cli-bridge agent that still carried the previous
     model as "stale". ``mark_agents_for_sync`` skips archived agents, so the
     archived row never got fixed — and the net flagged EVERY agent on the
     runtime again after each 600 s guard window.
  2. The host sync (``sync_host_agent_model``) then wrote ``agent.env`` with
     unchanged content → launchd restarted the bridge (start 1), and the
     adapter reload restarted it again (start 2).

The same shape returns with any LIVE agent that cannot converge (give-up
cooldown, failing health check): as long as the net flags the whole runtime,
a converged host agent gets an ``adapter.reload`` (a process restart) every
window, with ``agent.env`` untouched. So "stale" and "flagged" are one set,
chosen by one filter (``runtime_propagation.is_model_sync_target``).

Covered here, each with a counter-case:

  * the writer skips identical content (mtime and inode untouched) and writes
    changed content exactly once, in place (same inode — no tmp+rename, which
    would detach a single-file bind mount), owner-only, as the exact bytes it
    compares against; provisioning uses the same writer (built once),
  * a repeated host model sync leaves ``agent.env`` untouched, a real model
    change still rewrites it,
  * the watcher's net ignores archived agents and host agents without an
    adapter, flags only the stale agents (a stale host agent included), and
    a stuck live agent never restarts a converged host agent over three
    guard windows — end to end through the real flagging and sync passes.

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


def test_non_ascii_content_converges_after_one_write(tmp_path):
    """The write produces exactly the bytes the comparison checks against —
    otherwise a value outside ASCII would make every sync write again."""
    from app.services.agent_env_writer import write_env_file_if_changed

    path = tmp_path / "agent.env"
    content = "LABEL='Grüße – ok ✓'\n"

    assert write_env_file_if_changed(path, content) is True
    assert path.read_bytes() == content.encode("utf-8")
    _age(path)
    assert write_env_file_if_changed(path, content) is False
    assert path.stat().st_mtime_ns == OLD_NS


def test_provisioning_uses_the_one_owner_only_writer():
    """Built once: the owner-only open/fchmod/write rule for secret agent
    files lives in agent_env_writer — host provisioning (agent.env,
    models.yml, .mcp.json) calls that, it does not carry its own copy."""
    from app.services import agent_env_writer, host_provisioning

    assert host_provisioning.write_owner_only_file is agent_env_writer.write_owner_only_file
    assert "O_TRUNC" not in Path(host_provisioning.__file__).read_text()


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


# ── 3. The watcher's net flags exactly the stale agents ──────────────────────


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis

    return _get


async def _slot(session: AsyncSession) -> Runtime:
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
    return slot


async def _bind(
    session: AsyncSession,
    slot: Runtime,
    name: str,
    *,
    model: str | None,
    agent_runtime: str = "cli-bridge",
    harness: str = "omp",
    archived: bool = False,
) -> Agent:
    agent = Agent(
        name=name,
        slug=name,
        role="developer",
        agent_runtime=agent_runtime,
        harness=harness,
        model=model,
        runtime_id=slot.id,
        archived_at=utcnow() if archived else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _slot_with_agents(
    session: AsyncSession, *, stale_archived: bool
) -> Runtime:
    """The live shape: a host agent and a cli-bridge agent already on the
    served model, plus a cli-bridge agent that still carries the old one."""
    slot = await _slot(session)
    await _bind(session, slot, "agent-host", model="org/neu",
                agent_runtime="host", harness="hermes")
    await _bind(session, slot, "agent-a", model="org/neu")
    await _bind(session, slot, "agent-old", model="org/alt", archived=stale_archived)
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


async def _tick_real_flagging(session, redis) -> None:
    """One tick with the REAL ``mark_agents_for_sync`` — only the sync pass
    after it is stubbed, so the flags it set stay visible to the test."""
    watcher = RuntimeWatcher(interval=90)
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("org/neu", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(redis)
    ), patch(
        "app.services.runtime_watcher.sync_pending_agents", new=AsyncMock()
    ):
        await watcher.tick(session=session)


async def _flags(session: AsyncSession, *agents: Agent) -> dict[str, bool]:
    out = {}
    for agent in agents:
        await session.refresh(agent)
        out[agent.name] = bool(agent.pending_runtime_sync)
    return out


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

    mock_mark = await _tick(async_session, fake_redis, flagged=1)

    assert mock_mark.await_count == 1
    assert mock_mark.await_args.args[1].id == slot.id
    assert await fake_redis.get(RedisKeys.runtime_agent_model_guard(slot.slug))


@pytest.mark.asyncio
async def test_watcher_net_flags_only_the_stale_agent(async_session, fake_redis):
    """One live agent with an old model must not drag the whole runtime into
    a sync. Before: the net flagged EVERY agent on the runtime, so a converged
    host agent got a full sync plus a process restart (adapter reload) once
    per guard window for as long as one other agent stayed stale."""
    slot = await _slot(async_session)
    host_agent = await _bind(async_session, slot, "agent-host", model="org/neu",
                             agent_runtime="host", harness="hermes")
    current = await _bind(async_session, slot, "agent-a", model="org/neu")
    stale = await _bind(async_session, slot, "agent-old", model="org/alt")

    await _tick_real_flagging(async_session, fake_redis)

    assert await _flags(async_session, host_agent, current, stale) == {
        "agent-host": False,
        "agent-a": False,
        "agent-old": True,
    }


@pytest.mark.asyncio
async def test_watcher_net_flags_a_stale_host_agent(async_session, fake_redis):
    """A host agent with an adapter is synced exactly like a cli-bridge agent
    (``_sync_one`` writes its model back), so its own old model counts too.
    Before, the net only reached host agents as a side effect of some
    cli-bridge agent on the same runtime being stale."""
    slot = await _slot(async_session)
    host_agent = await _bind(async_session, slot, "agent-host", model="org/alt",
                             agent_runtime="host", harness="hermes")
    current = await _bind(async_session, slot, "agent-a", model="org/neu")

    await _tick_real_flagging(async_session, fake_redis)

    assert await _flags(async_session, host_agent, current) == {
        "agent-host": True,
        "agent-a": False,
    }
    assert await fake_redis.get(RedisKeys.runtime_agent_model_guard(slot.slug))


@pytest.mark.asyncio
async def test_watcher_net_ignores_a_host_agent_without_an_adapter(
    async_session, fake_redis
):
    """Counter-case: a host agent no sync can reach (no adapter registered for
    its harness) never counts as stale — ``mark_agents_for_sync`` would skip
    it, so it would hold the guard forever without anything changing."""
    slot = await _slot(async_session)
    await _bind(async_session, slot, "agent-host", model="org/alt",
                agent_runtime="host", harness="no-such-harness")

    mock_mark = await _tick(async_session, fake_redis, flagged=0)

    mock_mark.assert_not_awaited()
    assert await fake_redis.get(RedisKeys.runtime_agent_model_guard(slot.slug)) is None


@pytest.mark.asyncio
async def test_a_stuck_agent_never_restarts_a_converged_host_agent(
    async_session, isolate_redis_singleton, tmp_path, monkeypatch
):
    """The full loop, end to end through the real flagging and sync passes.

    A live cli-bridge agent that cannot converge (here: in the give-up
    cooldown after three failed syncs) stays "stale" for good. The net
    re-fires after every guard window. Each round must reach only that agent
    — the host agent on the right model must get no process restart at all.
    Before: one host restart per window (~137 a day), with ``agent.env``
    untouched, through ``adapter.reload``."""
    from app.services import runtime_propagation
    from app.services.host_harness_adapter import HOST_ADAPTERS

    redis = isolate_redis_singleton
    monkeypatch.setenv("HOME_HOST", str(tmp_path))  # never the real ~/.mc
    slot = await _slot(async_session)
    host_agent = await _bind(async_session, slot, "agent-host", model="org/neu",
                             agent_runtime="host", harness="hermes")
    stuck = await _bind(async_session, slot, "agent-stuck", model="org/alt")
    await redis.set(RedisKeys.agent_model_sync_giveup(str(stuck.id)), "1", ex=900)

    reload = AsyncMock(return_value={"ok": True})
    mark = AsyncMock(wraps=runtime_propagation.mark_agents_for_sync)
    watcher = RuntimeWatcher(interval=90)
    guard = RedisKeys.runtime_agent_model_guard(slot.slug)
    with patch.object(HOST_ADAPTERS["hermes"], "reload", new=reload), patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("org/neu", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(redis)
    ), patch("app.services.runtime_watcher.mark_agents_for_sync", new=mark):
        for _ in range(3):
            await watcher.tick(session=async_session)
            assert await redis.get(guard)  # the net did fire this window
            await redis.delete(guard)  # the 600 s guard window runs out

    assert mark.await_count == 3  # three windows, the net fired in each
    reload.assert_not_awaited()
    await async_session.refresh(host_agent)
    assert host_agent.pending_runtime_sync is False
