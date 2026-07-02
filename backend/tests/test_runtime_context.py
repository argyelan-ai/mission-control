"""Tests fuer runtime_context.py Helpers (REL-06 + REL-07).

Plan 04: workspace_path_for_runtime ist live → die 3 Workspace-Tests sind GREEN.
Plan 05: get_session_context_for_runtime ist live → die 2 Session-Tests sind GREEN.
"""
import pytest

from app.services.runtime_context import (
    get_session_context_for_runtime,
    workspace_path_for_runtime,
)


def test_workspace_path_passthrough_for_host():
    """Host-Runtime: Path bleibt host-perspektivisch (passthrough)."""
    class FakeAgent:
        agent_runtime = "host"

    assert workspace_path_for_runtime(FakeAgent(), "/Users/testuser/.mc/x") == "/Users/testuser/.mc/x"


def test_workspace_path_translates_for_cli_bridge():
    """cli-bridge-Runtime: Path wird in /workspace/... uebersetzt."""
    class FakeAgent:
        agent_runtime = "cli-bridge"
        name = "cody"

    out = workspace_path_for_runtime(FakeAgent(), "/Users/testuser/.mc/workspaces/cody/x.py")
    assert out is not None and out.startswith("/workspace")


def test_workspace_path_per_runtime():
    """Roadmap success criterion 4: workspace_path_for_runtime ist die einzige API."""
    class HostAgent: agent_runtime = "host"
    class DockerAgent: agent_runtime = "cli-bridge"; name = "cody"
    class GwAgent: agent_runtime = "openclaw"; name = "henry"

    # Pure smoke — alle drei Runtimes muessen aufrufbar sein ohne Exception
    assert workspace_path_for_runtime(HostAgent(), "/tmp/x") is not None
    assert workspace_path_for_runtime(DockerAgent(), "/Users/testuser/.openclaw/agents/cody/x") is not None
    # openclaw kann None zurueckgeben wenn Pfad nicht im Mount liegt — das ist OK
    _ = workspace_path_for_runtime(GwAgent(), "/var/external/x")


@pytest.mark.asyncio
async def test_no_reset_no_recap():
    """reset_session=False → recap is None, reset_required=False."""
    class FakeAgent:
        pass

    ctx = await get_session_context_for_runtime(
        FakeAgent(), task=None, reset_session=False, session=None,
    )
    assert ctx.reset_required is False
    assert ctx.recovery_recap is None


@pytest.mark.asyncio
async def test_reset_returns_recap(session, make_board, make_agent, make_task):
    """reset_session=True → recap MUSS gefuellt sein (ABSOLUTE VERBOTE)."""
    board = await make_board()
    agent = await make_agent(board_id=board.id)
    task = await make_task(board_id=board.id, title="Test recap", status="in_progress")

    ctx = await get_session_context_for_runtime(
        agent, task=task, reset_session=True, session=session,
    )
    assert ctx.reset_required is True
    assert ctx.recovery_recap is not None
    # Recap MUSS Task-ID + Title irgendwo enthalten (Structured Recovery Recap)
    assert str(task.id) in ctx.recovery_recap or "Test recap" in ctx.recovery_recap
