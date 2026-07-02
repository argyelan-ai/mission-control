import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.services.install_executor import InstallExecutor


@pytest.mark.asyncio
async def test_install_mcp_appends_to_mcp_servers(async_session: AsyncSession):
    agent = Agent(name="Cody", role="Dev", scopes=[], mcp_servers=["existing"])
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent.id,
        action_type="install_mcp", description="Install filesystem",
        payload={
            "name": "filesystem",
            "source": "npm:@modelcontextprotocol/server-filesystem",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._call_mcp_install",
               new_callable=AsyncMock) as mock_install, \
         patch("app.services.install_executor._call_mcp_smoke_test",
               new_callable=AsyncMock, return_value=True), \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock):
        mock_install.return_value = {"installed_version": "0.4.1"}
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert "filesystem" in agent.mcp_servers
    assert "existing" in agent.mcp_servers
    assert result.result == "success"


@pytest.mark.asyncio
async def test_install_mcp_rollback_on_smoke_test_fail(async_session: AsyncSession):
    agent = Agent(name="Cody", role="Dev", scopes=[], mcp_servers=None)
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent.id,
        action_type="install_mcp", description="test",
        payload={
            "name": "broken",
            "source": "npm:@modelcontextprotocol/server-broken",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._call_mcp_install",
               new_callable=AsyncMock) as mock_install, \
         patch("app.services.install_executor._call_mcp_smoke_test",
               new_callable=AsyncMock, return_value=False), \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock), \
         patch("app.services.install_executor._call_mcp_uninstall",
               new_callable=AsyncMock):
        mock_install.return_value = {"installed_version": "0.1"}
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert agent.mcp_servers is None
    assert result.result == "rolled_back"
    assert "smoke test" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_uninstall_mcp_removes(async_session: AsyncSession):
    """Un-assign path: MCP from one agent, no other agents involved → registry cleanup fires."""
    agent = Agent(name="Cody", role="Dev", scopes=[],
                  mcp_servers=["filesystem", "supabase"])
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent.id,
        action_type="uninstall_mcp", description="test",
        payload={
            "name": "filesystem",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock), \
         patch("app.services.install_executor._call_mcp_uninstall",
               new_callable=AsyncMock) as mock_registry_uninstall:
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert agent.mcp_servers == ["supabase"]
    assert result.result == "success"
    # Orphan after un-assign → registry dir cleanup must fire.
    mock_registry_uninstall.assert_awaited_once_with("filesystem")


@pytest.mark.asyncio
async def test_uninstall_mcp_keeps_registry_when_another_agent_references_it(
    async_session: AsyncSession,
):
    """If a second agent still has the MCP in its allowlist, the registry
    directory must survive — the user only un-assigned it from one agent.
    """
    agent_a = Agent(name="Cody", role="Dev", scopes=[],
                    mcp_servers=["filesystem"])
    agent_b = Agent(name="Rex", role="Reviewer", scopes=[],
                    mcp_servers=["filesystem", "supabase"])
    async_session.add(agent_a)
    async_session.add(agent_b)
    await async_session.commit()
    await async_session.refresh(agent_a)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent_a.id,
        action_type="uninstall_mcp", description="test",
        payload={
            "name": "filesystem",
            "target_agent_id": str(agent_a.id),
            "requester_agent_id": str(agent_a.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock), \
         patch("app.services.install_executor._call_mcp_uninstall",
               new_callable=AsyncMock) as mock_registry_uninstall:
        result = await InstallExecutor(async_session).execute(approval)

    assert result.result == "success"
    mock_registry_uninstall.assert_not_awaited()


@pytest.mark.asyncio
async def test_uninstall_mcp_keeps_registry_when_any_agent_has_null_allowlist(
    async_session: AsyncSession,
):
    """mcp_servers=None means 'all installed MCPs' — if any agent has that,
    every installed MCP is implicitly in use, so we must not delete it.
    """
    agent_a = Agent(name="Cody", role="Dev", scopes=[],
                    mcp_servers=["filesystem"])
    agent_wild = Agent(name="Henry", role="Lead", scopes=[],
                       mcp_servers=None)
    async_session.add(agent_a)
    async_session.add(agent_wild)
    await async_session.commit()
    await async_session.refresh(agent_a)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent_a.id,
        action_type="uninstall_mcp", description="test",
        payload={
            "name": "filesystem",
            "target_agent_id": str(agent_a.id),
            "requester_agent_id": str(agent_a.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock), \
         patch("app.services.install_executor._call_mcp_uninstall",
               new_callable=AsyncMock) as mock_registry_uninstall:
        result = await InstallExecutor(async_session).execute(approval)

    assert result.result == "success"
    mock_registry_uninstall.assert_not_awaited()


@pytest.mark.asyncio
async def test_uninstall_mcp_registry_cleanup_failure_is_non_fatal(
    async_session: AsyncSession,
):
    """If the registry-dir rmtree fails, the un-assign itself must still
    report success — allowlist was updated cleanly. Error is captured in log.
    """
    agent = Agent(name="Cody", role="Dev", scopes=[],
                  mcp_servers=["filesystem"])
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent.id,
        action_type="uninstall_mcp", description="test",
        payload={
            "name": "filesystem",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock), \
         patch("app.services.install_executor._call_mcp_uninstall",
               new_callable=AsyncMock,
               side_effect=OSError("permission denied")):
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert agent.mcp_servers == []
    assert result.result == "success"
