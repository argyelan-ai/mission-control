import pytest
from app.utils import utcnow
from app.services.dispatch import find_dispatch_target


@pytest.mark.asyncio
async def test_archived_agent_not_dispatch_target(session, make_board, make_task):
    from app.models.agent import Agent
    board = await make_board()
    task = await make_task(board_id=board.id)
    archived = Agent(name="ArchivedDev", slug="archiveddev", agent_runtime="cli-bridge",
                     board_id=board.id, archived_at=utcnow())
    session.add(archived); await session.commit()
    target, reason = await find_dispatch_target(session, task, board.id)
    assert target is None or target.id != archived.id


@pytest.mark.asyncio
async def test_archived_explicit_assignment_falls_through(session, make_board, make_task):
    from app.models.agent import Agent
    board = await make_board()
    archived = Agent(name="ArchivedAssignee", slug="archivedassignee", agent_runtime="cli-bridge",
                     board_id=board.id, archived_at=utcnow())
    session.add(archived); await session.commit()
    task = await make_task(board_id=board.id, assigned_agent_id=archived.id)
    target, reason = await find_dispatch_target(session, task, board.id)
    assert target is None or target.id != archived.id
    assert reason != "explicit_assignment"


@pytest.mark.asyncio
async def test_active_agent_still_dispatch_target(session, make_board, make_task):
    from app.models.agent import Agent
    board = await make_board()
    task = await make_task(board_id=board.id)
    active = Agent(name="ActiveDev", slug="activedev", agent_runtime="cli-bridge",
                   board_id=board.id)
    session.add(active); await session.commit()
    target, reason = await find_dispatch_target(session, task, board.id)
    assert target is not None and target.id == active.id


@pytest.mark.asyncio
async def test_get_agents_excludes_archived_by_default(auth_client, make_agent):
    await make_agent(name="ActiveOne", agent_runtime="cli-bridge")
    await make_agent(name="ArchivedOne", agent_runtime="cli-bridge", archived_at=utcnow())
    resp = await auth_client.get("/api/v1/agents")
    names = [a["name"] for a in resp.json()]
    assert "ActiveOne" in names
    assert "ArchivedOne" not in names


@pytest.mark.asyncio
async def test_get_agents_include_archived(auth_client, make_agent):
    await make_agent(name="ArchivedTwo", agent_runtime="cli-bridge", archived_at=utcnow())
    resp = await auth_client.get("/api/v1/agents?include_archived=true")
    names = [a["name"] for a in resp.json()]
    assert "ArchivedTwo" in names
