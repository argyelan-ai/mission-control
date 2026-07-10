"""
Tests for the x_post hook in resolve_approval() (routers/approvals.py).

Covers:
- Approval-gate: rejecting an x_post approval never calls XPublisher.post_text()
- Approving an x_post approval calls XPublisher.post_text() and stores the URL
  in resolver_note + activity_event detail
- A failed post (e.g. missing secrets / rate-limited) does not raise — the
  approval still resolves to "approved", the failure is recorded
- content_pipeline_id in the payload: a successful post updates the linked
  ContentPipeline row (published_url/published_platform/published_at/status)
  instead of creating a second lifecycle
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_x_post_approval(
    *,
    text: str = "Hello from Mission Control",
    status: str = "pending",
    content_pipeline_id: uuid.UUID | None = None,
    requester_task_id: uuid.UUID | None = None,
):
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.approval import Approval

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="MC Dev", slug=f"mc-dev-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        agent = Agent(name="Writer", role="writer", scopes=[], board_id=board.id)
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

        payload = {
            "text": text,
            "requester_agent_id": str(agent.id),
            "requester_task_id": str(requester_task_id) if requester_task_id else None,
            "content_pipeline_id": str(content_pipeline_id) if content_pipeline_id else None,
        }
        approval = Approval(
            board_id=board.id,
            agent_id=agent.id,
            action_type="x_post",
            description=f"Post to X: {text[:80]}",
            payload=payload,
            status=status,
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    return approval, board, agent


@pytest.mark.asyncio
async def test_reject_never_calls_post_text(auth_client, fake_redis):
    approval, board, agent = await _make_x_post_approval()

    with patch(
        "app.services.x_publisher.post_text", new_callable=AsyncMock
    ) as mock_post:
        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "rejected", "resolver_note": "not now"},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_calls_post_text_and_stores_url(auth_client, fake_redis):
    approval, board, agent = await _make_x_post_approval(text="Approved draft")

    fake_result = {
        "ok": True,
        "tweet_id": "999",
        "url": "https://x.com/i/status/999",
    }
    with patch(
        "app.services.x_publisher.post_text",
        new=AsyncMock(return_value=fake_result),
    ) as mock_post:
        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock) as mock_emit:
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "approved", "resolver_note": "go"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    mock_post.assert_awaited_once()
    _, called_text = mock_post.call_args.args
    assert called_text == "Approved draft"
    assert "https://x.com/i/status/999" in body["resolver_note"]

    # emit_event called with the published event carrying the URL in detail
    published_calls = [
        c for c in mock_emit.call_args_list
        if c.kwargs.get("event_type") == "x_post.published"
    ]
    assert len(published_calls) == 1
    assert published_calls[0].kwargs["detail"]["url"] == "https://x.com/i/status/999"


@pytest.mark.asyncio
async def test_approve_failed_post_does_not_raise(auth_client, fake_redis):
    approval, board, agent = await _make_x_post_approval()

    fake_result = {
        "ok": False,
        "error_type": "missing_secrets",
        "error": "X-Secrets fehlen in der Vault: x_api_key, x_api_secret, x_access_token, x_access_token_secret",
    }
    with patch(
        "app.services.x_publisher.post_text",
        new=AsyncMock(return_value=fake_result),
    ):
        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "approved", "resolver_note": "go"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Approval itself still resolves — the publish failure is recorded, not raised.
    assert body["status"] == "approved"
    assert "FAILED" in body["resolver_note"]
    assert "missing_secrets" in body["resolver_note"]


@pytest.mark.asyncio
async def test_approve_updates_linked_content_pipeline(auth_client, fake_redis):
    from app.models.content import ContentPipeline

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        pipeline = ContentPipeline(
            board_id=uuid.uuid4(),  # FK not enforced by sqlite test engine
            title="Test pipeline",
            content_type="social",
            status="approved",
        )
        s.add(pipeline)
        await s.commit()
        await s.refresh(pipeline)
        pipeline_id = pipeline.id
        pipeline_board_id = pipeline.board_id

    approval, board, agent = await _make_x_post_approval(
        content_pipeline_id=pipeline_id,
    )
    # Re-target the approval's board to the pipeline's board isn't required —
    # the hook looks up ContentPipeline purely by id from the payload.

    fake_result = {"ok": True, "tweet_id": "42", "url": "https://x.com/i/status/42"}
    with patch(
        "app.services.x_publisher.post_text",
        new=AsyncMock(return_value=fake_result),
    ):
        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "approved"},
            )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(ContentPipeline, pipeline_id)
        assert refreshed.published_url == "https://x.com/i/status/42"
        assert refreshed.published_platform == "twitter"
        assert refreshed.status == "published"
        assert refreshed.published_at is not None
