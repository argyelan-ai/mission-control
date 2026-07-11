"""provision_agent_background — deterministic workspace_path assignment.

Bug context (2026-07-08): migration 0087 was a ONE-TIME backfill that gave
every cli-bridge agent a `~/.mc/workspaces/<slug>` workspace_path. The
modern provisioning path (`provision_agent_background`) never set
`agent.workspace_path` itself — only the dead legacy Free-Code-Bridge
setter (`provision_cli_agent` in routers/cli_terminal.py, hardcoded to
~/FreeCode/projects) ever did. Any cli-bridge agent created or reset AFTER
0087 got workspace_path=NULL forever, and `cli_bridge_runner._resolve_workspace()`
hard-fails on first dispatch with a misleading "Migration 0087 nicht
gelaufen?" error. This just happened to the "Installer" agent.

Fix: provision_agent_background deterministically assigns workspace_path
for cli-bridge agents (mirroring migration 0087's exact slug convention —
`slugify_project()` from git_service.py, the same helper the migration's
SQL regexp mirrors) whenever it is still NULL/empty. Host/other runtimes
are left untouched (they are not workspace-bound the same way, e.g.
Boss reads ~/Workspace directly).
"""
from __future__ import annotations

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from tests.conftest import test_engine


@pytest.fixture
def _patched_engine(monkeypatch):
    """provision_agent_background builds its own session from app.database.engine."""
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.fixture
def _happy_sync(monkeypatch):
    """Compose render + file sync + container start succeed, events collected."""
    events: list[tuple] = []

    async def fake_write_compose(session):
        return {"changed": "true"}

    async def fake_sync(session, ag):
        return {"SOUL.md": "written", "TOOLS.md": "written (from DB)"}

    async def fake_emit(session, event_type, message, **kwargs):
        events.append((event_type, message, kwargs.get("severity")))

    def fake_ensure(ag):
        return {"status": "recreated", "container": "mc-agent-x", "mode": "recreate"}

    monkeypatch.setattr(
        "app.services.compose_renderer.write_compose_agents", fake_write_compose
    )
    monkeypatch.setattr(
        "app.services.docker_agent_sync.sync_docker_agent_files", fake_sync
    )
    monkeypatch.setattr(
        "app.services.docker_agent_sync.ensure_agent_container_started", fake_ensure
    )
    monkeypatch.setattr("app.services.provisioning.emit_event", fake_emit)
    return events


async def _make_agent(
    name: str = "Installer", runtime: str = "cli-bridge", workspace_path: str | None = None
) -> Agent:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            name=name,
            agent_runtime=runtime,
            provision_status="local",
            workspace_path=workspace_path,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent


async def _reload(agent_id) -> Agent:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Agent, agent_id)


@pytest.mark.asyncio
async def test_cli_bridge_agent_gets_default_workspace_path(
    monkeypatch, _patched_engine, _happy_sync
):
    """A freshly-created (post-0087) cli-bridge agent with workspace_path=NULL
    must be assigned the standard `~/.mc/workspaces/<slug>` path during
    provisioning — the exact bug that hard-failed dispatch for 'Installer'."""
    from app.services import provisioning

    agent = await _make_agent(name="Installer", workspace_path=None)
    assert agent.workspace_path is None

    await provisioning.provision_agent_background(agent.id)

    reloaded = await _reload(agent.id)
    assert reloaded.workspace_path == f"{settings.home_host}/.mc/workspaces/installer"


@pytest.mark.asyncio
async def test_cli_bridge_agent_existing_workspace_path_untouched(
    monkeypatch, _patched_engine, _happy_sync
):
    """Re-provisioning must not clobber a hand-edited/existing workspace_path."""
    from app.services import provisioning

    custom_path = "/some/custom/path"
    agent = await _make_agent(name="Rex", workspace_path=custom_path)

    await provisioning.provision_agent_background(agent.id)

    reloaded = await _reload(agent.id)
    assert reloaded.workspace_path == custom_path


@pytest.mark.asyncio
async def test_host_agent_workspace_path_not_forced(monkeypatch, _patched_engine):
    """Host-runtime agents (Boss/Hermes/Jarvis) are not workspace-bound the
    same way — provisioning must leave workspace_path=NULL alone (regression
    guard against over-eager forcing)."""
    from app.services import provisioning

    async def fake_emit(session, event_type, message, **kwargs):
        pass

    monkeypatch.setattr("app.services.provisioning.emit_event", fake_emit)

    agent = await _make_agent(name="Boss Host Agent", runtime="host", workspace_path=None)

    await provisioning.provision_agent_background(agent.id)

    reloaded = await _reload(agent.id)
    assert reloaded.workspace_path is None
    assert reloaded.provision_status == "provisioned"


@pytest.mark.asyncio
async def test_cli_bridge_resolve_workspace_self_heals_null_path(monkeypatch):
    """cli_bridge_runner._resolve_workspace() must self-heal a NULL
    workspace_path (belt-and-suspenders vs. legacy agents that predate this
    fix and never got reprovisioned) instead of hard-failing dispatch with
    the misleading 'Migration 0087 nicht gelaufen?' RuntimeError."""
    from app.models.task import Task
    from app.services import cli_bridge_runner

    agent = Agent(
        id=None, name="Ghost Agent", agent_runtime="cli-bridge", workspace_path=None
    )
    task = Task(title="Ad-hoc task", project_id=None)

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        session.add(agent)
        await session.commit()
        await session.refresh(agent)

        monkeypatch.setattr(
            cli_bridge_runner, "_create_plain_workspace",
            lambda base, title: f"{base}/healed-task",
        )

        workspace, worktree_path, has_repo = await cli_bridge_runner._resolve_workspace(
            task, agent, session
        )

        assert workspace == f"{settings.home_host}/.mc/workspaces/ghost-agent/healed-task"
        assert worktree_path is None
        assert has_repo is False
