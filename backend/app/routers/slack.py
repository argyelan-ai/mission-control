"""Slack router — connection test for the Slack team-chat channel.

Admin-only: the endpoint reports the state of two System-Tokens, so it sits
behind `require_role(Role.ADMIN)` exactly like the secrets write endpoints
(ADR-033). The response never contains token material — see
`services/slack_client.SlackConnectionResult`.
"""

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.services.slack_client import test_connection

router = APIRouter(prefix="/api/v1/slack", tags=["slack"])


@router.post("/test-connection")
async def slack_test_connection(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Calls Slack's auth.test with the stored bot token and reports the result.

    Always 200 — a failed Slack handshake is a reportable state, not an HTTP
    error, so the UI can render the reason inline instead of a toast.
    """
    result = await test_connection(session)
    return result.to_dict()
