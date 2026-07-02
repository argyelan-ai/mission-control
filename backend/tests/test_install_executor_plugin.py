import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.services.install_executor import InstallExecutor


@pytest.mark.asyncio
async def test_install_plugin_appends_to_cli_plugins(async_session: AsyncSession):
    agent = Agent(name="Cody", slug="cody", role="Dev",
                  cli_plugins=["existing@official"], scopes=[])
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent.id,
        action_type="install_plugin", description="Install superpowers",
        payload={
            "name": "superpowers@claude-plugins-official",
            "source": "claude-plugins-official",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._call_plugin_install",
               new_callable=AsyncMock) as mock_install, \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock):
        mock_install.return_value = {"installed_version": "5.0.7"}
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert "superpowers@claude-plugins-official" in agent.cli_plugins
    assert "existing@official" in agent.cli_plugins
    assert result.result == "success"
    mock_install.assert_awaited_once_with("claude-plugins-official",
                                          "superpowers@claude-plugins-official")


@pytest.mark.asyncio
async def test_uninstall_plugin_removes(async_session: AsyncSession):
    agent = Agent(name="Cody", slug="cody", role="Dev",
                  cli_plugins=["a@x", "b@y"], scopes=[])
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent.id,
        action_type="uninstall_plugin", description="test",
        payload={
            "name": "b@y",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock):
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert agent.cli_plugins == ["a@x"]
    assert result.result == "success"
