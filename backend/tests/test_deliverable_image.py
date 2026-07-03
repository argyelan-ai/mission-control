"""Tests: deliverable image delivery."""
import uuid
import pytest


@pytest.mark.anyio
async def test_deliverable_image_not_found(auth_client, make_board, make_task):
    """404 if the deliverable doesn't exist."""
    board = await make_board()
    task = await make_task(board.id)
    fake_id = str(uuid.uuid4())
    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables/{fake_id}/image"
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_deliverable_image_wrong_type(auth_client, make_board, make_task, make_agent, session):
    """400 if the deliverable is not a screenshot."""
    from app.models.deliverable import TaskDeliverable
    board = await make_board()
    agent = await make_agent(board_id=board.id)
    task = await make_task(board.id)

    d = TaskDeliverable(
        task_id=task.id,
        agent_id=agent.id,
        deliverable_type="url",
        title="Link",
        path="http://example.com",
    )
    session.add(d)
    await session.commit()
    await session.refresh(d)

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables/{d.id}/image"
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_deliverable_image_file_missing(auth_client, make_board, make_task, make_agent, session):
    """404 if the image file doesn't exist."""
    from app.models.deliverable import TaskDeliverable
    board = await make_board()
    agent = await make_agent(board_id=board.id)
    task = await make_task(board.id)

    d = TaskDeliverable(
        task_id=task.id,
        agent_id=agent.id,
        deliverable_type="screenshot",
        title="Screenshot",
        path="/tmp/nonexistent-screenshot-abc123.png",
    )
    session.add(d)
    await session.commit()
    await session.refresh(d)

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables/{d.id}/image"
    )
    assert resp.status_code == 404
