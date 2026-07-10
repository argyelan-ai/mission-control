"""Template instantiation binds runtime_id + harness (2026-07-10).

Template-created agents previously had no runtime binding — inconsistent
with custom create, so they silently fell back to docker-compose env.
"""
import uuid

import pytest

import app.routers.agent_templates as tpl_module
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.models.runtime import Runtime


def test_instantiate_request_validates_harness():
    with pytest.raises(ValueError, match="harness muss"):
        tpl_module.InstantiateRequest(board_id=uuid.uuid4(), harness="nope")


@pytest.mark.asyncio
async def test_instantiate_binds_runtime_and_harness(async_session):
    rt = Runtime(
        slug="tpl-rt", display_name="Tpl RT", runtime_type="lmstudio",
        endpoint="http://x/v1", model_identifier="m", enabled=True,
    )
    tpl = AgentTemplate(name="Planner", emoji="🧠", role="planner", scopes=[])
    async_session.add(rt)
    async_session.add(tpl)
    await async_session.commit()
    await async_session.refresh(rt)
    await async_session.refresh(tpl)

    agent, _token = await tpl_module._do_instantiate(
        template=tpl, board_id=None, name=None, model=None,
        session=async_session, runtime_id="tpl-rt", harness="openclaude",
    )
    refreshed = await async_session.get(Agent, agent.id)
    assert refreshed.runtime_id == rt.id
    assert refreshed.harness == "openclaude"
