import uuid
from unittest.mock import patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession


@pytest.mark.asyncio
async def test_dispatch_message_includes_parent_credentials_for_auth_child(
    session: AsyncSession, make_agent, make_task,
):
    """Root credentials must appear in the child dispatch message when the child requires auth."""
    from app.services.dispatch import _build_dispatch_message

    board_id = uuid.uuid4()
    agent = await make_agent(
        "Cody", board_id=board_id, role="developer"
    )
    parent = await make_task(
        board_id,
        title="Root Task",
        credentials_encrypted="enc-parent",
        requires_auth=True,
    )
    child = await make_task(
        board_id,
        title="Cred-Verify Auth Child",
        parent_task_id=parent.id,
        requires_auth=True,
        assigned_agent_id=agent.id,
        status="inbox",
    )

    with patch("app.services.encryption.safe_decrypt", return_value="test:pass123"):
        msg = await _build_dispatch_message(child, agent, session)

    assert "## Credentials" in msg
    assert "test:pass123" in msg
