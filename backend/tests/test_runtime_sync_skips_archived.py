"""Archived agents must never be flagged, synced, restarted, recreated or
started again by runtime propagation (live incident 02.10.2026).

What happened: agent Rex was archived on 01.10. (``archived_at`` set, its
``mc-agent-rex`` container stopped by ``agent_lifecycle.archive_agent``). On
02.10. at 20:34 UTC, PR #739 (flag agents bound to a slot runtime for
re-sync when the context window changes) went live. The next watcher tick
flagged Rex's row too — ``mark_agents_for_sync`` only checked
``runtime_id``, never ``archived_at`` — and the sync loop restarted the
stopped container. It then ran for 21h before anyone noticed.

Every assertion below has a counter-case (sabotage probe) that would pass
without the fix: skip the ``archived_at`` check and the archived agent gets
flagged/restarted/started just like a live one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import docker_agent_sync, recipe_switcher, runtime_manager
from app.services import runtime_propagation as rp
from app.utils import utcnow

MODEL = "GLM-5.3-Flash-EXL3"


# ── Shared DB setup (mirrors tests/test_slot_context_window.py) ─────────────


async def _host(session: AsyncSession, **kw) -> Host:
    host = Host(
        slug="box-a", display_name="BOX-A", kind="ssh", ssh_host="192.0.2.10", **kw
    )
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, **kw) -> LocalRecipe:
    fields = dict(
        slug="recipe-tf",
        display_name="Recipe TF",
        engine="vllm_docker",
        model_identifier=MODEL,
        launch_template=(
            "docker run -d --name {container_name} --label mc.runtime.slug={slug} "
            "-p {port}:8000 img"
        ),
        port=8000,
        context_len=1048576,
    )
    fields.update(kw)
    recipe = LocalRecipe(**fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


async def _slot(session: AsyncSession, host: Host, **kw) -> Runtime:
    fields = dict(
        display_name="BOX-A :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier=MODEL,
        max_context_len=250000,
        preferred_context_len=250000,
        exclusive_memory=False,
        is_slot=True,
        enabled=True,
    )
    fields.update(kw)
    rt = Runtime(slug="box-a-slot", host_id=host.id, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _agent(session: AsyncSession, rt: Runtime, *, archived: bool = False) -> Agent:
    agent = Agent(
        name="rex", slug="rex", role="developer", agent_runtime="cli-bridge",
        harness="omp", model=MODEL, runtime_id=rt.id,
        archived_at=utcnow() if archived else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _start_patches():
    from app.services import runtime_grace

    return (
        patch.object(recipe_switcher, "probe_running", AsyncMock(return_value=False)),
        patch.object(
            runtime_manager, "start_runtime",
            AsyncMock(return_value={"ok": True, "message": "gestartet"}),
        ),
        patch.object(runtime_grace, "mark_switching", AsyncMock()),
        patch.object(runtime_grace, "clear_switching", AsyncMock()),
    )


# ── 1. mark_agents_for_sync must not flag an archived agent ────────────────


@pytest.mark.asyncio
async def test_archived_agent_is_not_flagged_on_window_change(session):
    """RED before the fix: the window-change path flags every agent bound
    to the slot, including an archived one."""
    host = await _host(session)
    recipe = await _recipe(session)
    slot = await _slot(session, host)
    archived = await _agent(session, slot, archived=True)

    p1, p2, p3, p4 = _start_patches()
    with p1, p2, p3, p4:
        result = await recipe_switcher.start_recipe_on_host(session, host, recipe)

    assert result["ok"] is True
    await session.refresh(archived)
    assert archived.pending_runtime_sync is False, (
        "an archived agent must never be flagged for runtime re-sync — "
        "its container was stopped on purpose"
    )


@pytest.mark.asyncio
async def test_live_agent_on_the_same_slot_is_still_flagged(session):
    """Counter-case: the fix must not break the behaviour PR #739 added —
    a live agent on the same runtime still gets flagged."""
    host = await _host(session)
    recipe = await _recipe(session)
    slot = await _slot(session, host)
    live = await _agent(session, slot, archived=False)

    p1, p2, p3, p4 = _start_patches()
    with p1, p2, p3, p4:
        await recipe_switcher.start_recipe_on_host(session, host, recipe)

    await session.refresh(live)
    assert live.pending_runtime_sync is True


@pytest.mark.asyncio
async def test_mark_agents_for_sync_skips_archived_directly(session):
    host = await _host(session)
    slot = await _slot(session, host)
    archived = await _agent(session, slot, archived=True)

    flagged = await rp.mark_agents_for_sync(session, slot)

    assert flagged == 0
    await session.refresh(archived)
    assert archived.pending_runtime_sync is False


# ── 2. _sync_one must never restart/reload an already-flagged archived row ─


@pytest.mark.asyncio
async def test_sync_one_clears_flag_without_restarting_archived_agent(session):
    """Defense in depth: a row flagged before this fix (or by a stray write)
    must still never reach the restart path."""
    host = await _host(session)
    slot = await _slot(session, host)
    archived = await _agent(session, slot, archived=True)
    archived.pending_runtime_sync = True
    session.add(archived)
    await session.commit()

    with patch.object(
        docker_agent_sync, "restart_docker_agent_container"
    ) as restart_mock:
        await rp._sync_one(session, archived)

    restart_mock.assert_not_called()
    await session.refresh(archived)
    assert archived.pending_runtime_sync is False


@pytest.mark.asyncio
async def test_sync_pending_agents_never_restarts_an_archived_agent(session):
    """End-to-end sabotage probe for the exact live incident: an archived
    agent sits flagged (stray pre-fix state) and the watcher tick runs."""
    host = await _host(session)
    slot = await _slot(session, host)
    archived = await _agent(session, slot, archived=True)
    archived.pending_runtime_sync = True
    session.add(archived)
    await session.commit()

    with patch.object(
        docker_agent_sync, "restart_docker_agent_container"
    ) as restart_mock:
        await rp.sync_pending_agents(session, force=True, runtime_id=slot.id)

    restart_mock.assert_not_called()


# ── 3. docker_agent_sync choke points refuse to touch an archived agent ────


def _mk_agent(**kw) -> Agent:
    fields = dict(name="rex", agent_runtime="cli-bridge")
    fields.update(kw)
    return Agent(**fields)


def test_restart_docker_agent_container_skips_archived():
    agent = _mk_agent(archived_at=utcnow())
    with patch("subprocess.run") as run_mock:
        result = docker_agent_sync.restart_docker_agent_container(agent)

    run_mock.assert_not_called()
    assert result["status"] == "skipped (archived)"


def test_restart_docker_agent_container_runs_for_a_live_agent():
    """Counter-case: the guard must not swallow the normal, non-archived path."""
    agent = _mk_agent(archived_at=None)
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=proc) as run_mock:
        result = docker_agent_sync.restart_docker_agent_container(agent)

    run_mock.assert_called_once()
    assert result["status"] == "restarted"


def test_ensure_agent_container_started_skips_archived():
    agent = _mk_agent(archived_at=utcnow())
    with patch.object(docker_agent_sync, "_agent_container_running") as running_mock, \
         patch.object(docker_agent_sync, "restart_docker_agent_container") as restart_mock:
        result = docker_agent_sync.ensure_agent_container_started(agent)

    running_mock.assert_not_called()
    restart_mock.assert_not_called()
    assert result["status"] == "skipped (archived)"


def test_start_docker_agent_container_skips_archived():
    agent = _mk_agent(archived_at=utcnow())
    with patch("subprocess.run") as run_mock:
        result = docker_agent_sync.start_docker_agent_container(agent)

    run_mock.assert_not_called()
    assert result["ok"] == "false"


def test_start_docker_agent_container_runs_for_a_live_agent():
    agent = _mk_agent(archived_at=None)
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=proc) as run_mock:
        result = docker_agent_sync.start_docker_agent_container(agent)

    run_mock.assert_called_once()
    assert result["ok"] == "true"
