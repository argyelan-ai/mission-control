"""Dispatch-message envelope invariants.

Workstream A2 removed the self-contained curl snippets for worker agents —
the lifecycle is now `mc ack` / `mc done` / etc., which read $MC_API_URL
and $MC_AGENT_TOKEN from the container env at runtime.

Invariants that still matter:
 * $MC_TOKEN must NEVER appear — the correct env var is $MC_AGENT_TOKEN.
 * Board-Lead / Planner prompts still emit curl (they need create-task
   payloads the CLI does not cover), and those curls must reference
   $MC_API_URL + $MC_AGENT_TOKEN.
 * Worker prompts reference the `mc` CLI for lifecycle transitions.
"""
import uuid
import pytest
from app.services.dispatch import _format_dispatch_message, DispatchContext
from app.models.agent import Agent
from app.models.task import Task


def _make_cli_agent(name="TestAgent", board_id=None, **overrides):
    base = dict(
        id=uuid.uuid4(),
        name=name,
        role="developer",
        board_id=board_id or uuid.uuid4(),
        agent_runtime="cli-bridge",
        is_board_lead=False,
        provision_status="provisioned",
    )
    base.update(overrides)
    return Agent(**base)


def _make_task(board_id):
    return Task(
        id=uuid.uuid4(),
        board_id=board_id,
        title="Test Task",
        status="inbox",
    )


@pytest.mark.asyncio
async def test_worker_dispatch_references_mc_cli():
    """Workers get `mc`-based lifecycle hints instead of curl blocks."""
    board_id = uuid.uuid4()
    agent = _make_cli_agent(board_id=board_id)
    task = _make_task(board_id)
    ctx = DispatchContext()
    msg = _format_dispatch_message(task, agent, ctx)
    # Lifecycle refs for workers
    assert "mc ack" in msg
    assert "mc comment progress" in msg
    assert "mc blocked" in msg
    # Hardcoded host URL must never leak
    assert "http://localhost/api/v1" not in msg


@pytest.mark.asyncio
async def test_dispatch_message_no_mc_token_var():
    """No `$MC_TOKEN` anywhere — the canonical env var is `$MC_AGENT_TOKEN`."""
    board_id = uuid.uuid4()
    agent = _make_cli_agent(board_id=board_id)
    task = _make_task(board_id)
    ctx = DispatchContext()
    msg = _format_dispatch_message(task, agent, ctx)
    assert "$MC_TOKEN" not in msg


@pytest.mark.asyncio
async def test_board_lead_dispatch_still_emits_curl_env_vars():
    """Board-Lead orchestrator prompt still renders curl with $MC_API_URL +
    $MC_AGENT_TOKEN (create-task payload is not yet covered by `mc`)."""
    board_id = uuid.uuid4()
    agent = _make_cli_agent(
        name="Henry", board_id=board_id, role="lead", is_board_lead=True,
    )
    task = _make_task(board_id)
    ctx = DispatchContext()
    msg = _format_dispatch_message(task, agent, ctx)
    assert "$MC_API_URL" in msg
    assert "$MC_AGENT_TOKEN" in msg
    assert "/api/v1/agent/boards/" in msg
