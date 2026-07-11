"""Core hook plumbing for verticals (ADR-044):

1. x_post_resolved_hooks fire from _handle_x_post_resolution (approve+reject),
   errors swallowed.
2. task_done_hooks fire for done tasks WITHOUT pipeline_id (hooks self-filter
   — the old `and task.pipeline_id` gate starved non-pipeline verticals).

Core-level tests: run in stripped installations too.
"""
import uuid
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from app.models.approval import Approval
from app.models.board import Board
from app.verticals import hooks as vertical_hooks


@pytest.fixture(autouse=True)
async def _fake_redis_for_module():
    """Ensure all tests in this module have a fake Redis so emit_event doesn't
    reach the network.  Mirrors what conftest.client does, but scoped here so
    the session-only tests (no HTTP client) also work."""
    import app.redis_client as rc

    server = fakeredis.aioredis.FakeServer()
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    original = rc._redis
    rc._redis = redis
    yield redis
    rc._redis = original
    await redis.aclose()


@pytest.fixture(autouse=True)
def _clean_hook_registries():
    saved_x = list(vertical_hooks.x_post_resolved_hooks)
    saved_t = list(vertical_hooks.task_done_hooks)
    yield
    vertical_hooks.x_post_resolved_hooks[:] = saved_x
    vertical_hooks.task_done_hooks[:] = saved_t


async def _make_approval(session, status="approved"):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    approval = Approval(
        board_id=board.id,
        action_type="x_post",
        description="test draft",
        payload={"text": "hello world"},
        status=status,
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    return board, approval


@pytest.mark.asyncio
async def test_x_post_approve_runs_hooks_with_result(session):
    from app.routers.approvals import _handle_x_post_resolution

    _, approval = await _make_approval(session)
    seen: list[tuple] = []

    async def hook(sess, appr, resolution_status, result):
        seen.append((appr.id, resolution_status, result))

    vertical_hooks.x_post_resolved_hooks.append(hook)

    ok_result = {"ok": True, "tweet_id": "1", "url": "https://x.com/i/status/1"}
    with patch(
        "app.services.x_publisher.post_text",
        new=AsyncMock(return_value=ok_result),
    ):
        await _handle_x_post_resolution(session, approval, "approved")

    assert len(seen) == 1
    assert seen[0][0] == approval.id
    assert seen[0][1] == "approved"
    assert seen[0][2]["ok"] is True


@pytest.mark.asyncio
async def test_x_post_reject_runs_hooks_with_none_result(session):
    from app.routers.approvals import _handle_x_post_resolution

    _, approval = await _make_approval(session, status="rejected")
    seen: list[tuple] = []

    async def hook(sess, appr, resolution_status, result):
        seen.append((resolution_status, result))

    vertical_hooks.x_post_resolved_hooks.append(hook)
    await _handle_x_post_resolution(session, approval, "rejected")

    assert seen == [("rejected", None)]


@pytest.mark.asyncio
async def test_x_post_hook_errors_are_swallowed(session):
    from app.routers.approvals import _handle_x_post_resolution

    _, approval = await _make_approval(session, status="rejected")

    async def bad_hook(sess, appr, resolution_status, result):
        raise RuntimeError("boom")

    vertical_hooks.x_post_resolved_hooks.append(bad_hook)
    # Must not raise:
    await _handle_x_post_resolution(session, approval, "rejected")


@pytest.mark.asyncio
async def test_task_done_hooks_fire_without_pipeline_id(auth_client, make_board, make_task):
    """PATCH task -> done via the operator route runs task_done_hooks even
    when task.pipeline_id is None (bench tasks have no ContentPipeline)."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="bench oneshot", status="in_progress")

    seen: list[uuid.UUID] = []

    async def hook(sess, done_task):
        seen.append(done_task.id)

    vertical_hooks.task_done_hooks.append(hook)

    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "done"},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [task.id]
