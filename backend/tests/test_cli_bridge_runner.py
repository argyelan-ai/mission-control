"""Test that dispatch_to_cli_bridge prepares workspace with correct agent slug.

History: Until 2026-04-05, dispatch_to_cli_bridge called urllib.urlopen to
send an HTTP /enqueue call to the bridge. Commit 80d7e85 (2026-04-13,
"eliminate Docker agent dispatch race condition") removed this call —
Docker agents now only read the disk queue via poll.sh.

These tests were rewritten accordingly: they now verify that the agent
slug is correctly derived in the emit_event message (previously it was
part of the HTTP payload).
"""
import uuid
from unittest.mock import AsyncMock, patch
import pytest

from app.models.agent import Agent
from app.models.task import Task


@pytest.mark.anyio
async def test_dispatch_uses_agent_slug_freecode(session):
    """Agent 'FreeCode' → slug 'freecode' in the cli_bridge_ready event."""
    agent = Agent(
        id=uuid.uuid4(), name="FreeCode",
        agent_runtime="cli-bridge",
        workspace_path="/tmp/ws",
        tools_md="Bearer test-token",
    )
    task = Task(id=uuid.uuid4(), title="T1", status="inbox", board_id=uuid.uuid4())

    with patch("app.services.cli_bridge_runner._resolve_workspace",
               return_value=("/tmp/ws", None, False)), \
         patch("app.services.cli_bridge_runner.emit_event",
               new_callable=AsyncMock) as mock_emit:
        from app.services.cli_bridge_runner import dispatch_to_cli_bridge
        result = await dispatch_to_cli_bridge(agent, task, "msg", session)

    assert result is True
    # Event message contains the derived slug
    mock_emit.assert_awaited()
    event_msg = mock_emit.call_args.args[2]
    assert "freecode" in event_msg


@pytest.mark.anyio
async def test_dispatch_uses_agent_slug_cody(session):
    """Agent 'Cody' → slug 'cody' in the cli_bridge_ready event."""
    agent = Agent(
        id=uuid.uuid4(), name="Cody",
        agent_runtime="cli-bridge",
        workspace_path="/tmp/ws",
        tools_md="Bearer test-token",
    )
    task = Task(id=uuid.uuid4(), title="T2", status="inbox", board_id=uuid.uuid4())

    with patch("app.services.cli_bridge_runner._resolve_workspace",
               return_value=("/tmp/ws", None, False)), \
         patch("app.services.cli_bridge_runner.emit_event",
               new_callable=AsyncMock) as mock_emit:
        from app.services.cli_bridge_runner import dispatch_to_cli_bridge
        result = await dispatch_to_cli_bridge(agent, task, "msg", session)

    assert result is True
    mock_emit.assert_awaited()
    event_msg = mock_emit.call_args.args[2]
    assert "cody" in event_msg


@pytest.mark.anyio
async def test_dispatch_uses_agent_slug_with_spaces(session):
    """Agent 'My Agent' → slug 'my-agent' (space → hyphen)."""
    agent = Agent(
        id=uuid.uuid4(), name="My Agent",
        agent_runtime="cli-bridge",
        workspace_path="/tmp/ws",
        tools_md="Bearer test-token",
    )
    task = Task(id=uuid.uuid4(), title="T3", status="inbox", board_id=uuid.uuid4())

    with patch("app.services.cli_bridge_runner._resolve_workspace",
               return_value=("/tmp/ws", None, False)), \
         patch("app.services.cli_bridge_runner.emit_event",
               new_callable=AsyncMock) as mock_emit:
        from app.services.cli_bridge_runner import dispatch_to_cli_bridge
        result = await dispatch_to_cli_bridge(agent, task, "msg", session)

    assert result is True
    mock_emit.assert_awaited()
    event_msg = mock_emit.call_args.args[2]
    assert "my-agent" in event_msg


