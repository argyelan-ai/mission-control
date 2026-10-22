"""Hermes learns how its runtime turns thinking OFF (``MC_THINKING_TEMPLATE_KWARG``).

The chat chip's "none" for Hermes must really stop DeepSeek from thinking. A
self-hosted reasoning runtime (TensorFold, vLLM, SGLang) does that through its
chat template — ``chat_template_kwargs: {"enable_thinking": false}`` — not
through Ollama's ``think`` that Hermes sends by default. MC knows which
runtimes those are (``supports_reasoning`` + a host of our own), so it names
the switch in Hermes' agent.env; scripts/hermes-config-patch.py turns it into
``model.thinking_template_kwarg`` (test_hermes_config_patcher.py).

Rules pinned here:
1. reasoning runtime on our own host → ``enable_thinking``;
2. no reasoning, or a cloud endpoint (no host) → ``""`` — an explicit "none",
   so a runtime switch clears the key that ``existing.update(...)`` in the
   agent.env sync would otherwise keep;
3. only Hermes gets the key — another harness' agent.env must not grow one
   (an agent.env rewrite restarts a host bridge).
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime

ENV = "MC_THINKING_TEMPLATE_KWARG"


def _runtime(**kw) -> Runtime:
    base = dict(
        slug="dgx-spark-slot",
        display_name="Spark slot",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="DeepSeek-V4.1-Flash-1017",
        max_context_len=1_048_576,
        supports_reasoning=True,
        host_id=uuid.uuid4(),
        enabled=True,
    )
    base.update(kw)
    return Runtime(**base)


def _hermes() -> Agent:
    return Agent(name="Hermes", slug="hermes", agent_runtime="host", harness="hermes")


@pytest.mark.asyncio
async def test_reasoning_runtime_on_own_host_names_the_template_switch(async_session):
    from app.routers.internal import build_runtime_env

    env = await build_runtime_env(_runtime(), async_session, agent=_hermes())
    assert env[ENV] == "enable_thinking"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"supports_reasoning": False},
        {"host_id": None, "runtime_type": "cloud", "endpoint": "https://ollama.com/v1"},
    ],
    ids=["no-reasoning", "cloud"],
)
async def test_other_runtimes_say_none_explicitly(async_session, overrides):
    from app.routers.internal import build_runtime_env

    env = await build_runtime_env(_runtime(**overrides), async_session, agent=_hermes())
    assert env[ENV] == ""


@pytest.mark.asyncio
async def test_hermes_runtime_type_branch_carries_it(async_session):
    from app.routers.internal import build_runtime_env

    env = await build_runtime_env(_runtime(runtime_type="hermes"), async_session)
    assert env[ENV] == "enable_thinking"


@pytest.mark.asyncio
async def test_hermes_bootstrap_env_carries_it(async_session):
    """``build_hermes_agent_env`` calls build_runtime_env WITHOUT the agent
    (harness derives to openclaude) — the key must still reach agent.env."""
    from app.services.agent_bootstrap import build_hermes_agent_env

    env = await build_hermes_agent_env(_runtime(), "tok", session=async_session)
    assert env[ENV] == "enable_thinking"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["omp", "openclaude", "claude"])
async def test_other_harnesses_get_no_key(async_session, harness):
    from app.routers.internal import build_runtime_env

    agent = Agent(name="X", slug="x", agent_runtime="host", harness=harness)
    env = await build_runtime_env(_runtime(), async_session, agent=agent)
    assert ENV not in env
