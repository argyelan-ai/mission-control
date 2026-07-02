"""Tests fuer Unified Push Dispatch + Pending Dispatch Queue."""
import uuid
from unittest.mock import patch

import pytest

from app.services.task_queue import (
    enqueue_pending_dispatch,
    dequeue_pending_dispatch,
    pending_dispatch_length,
)


# ── Pending Dispatch Queue Tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_dispatch_enqueue_dequeue(fake_redis):
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        agent_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())

        await enqueue_pending_dispatch(agent_id, task_id)
        assert await pending_dispatch_length(agent_id) == 1

        result = await dequeue_pending_dispatch(agent_id)
        assert result == task_id
        assert await pending_dispatch_length(agent_id) == 0


@pytest.mark.asyncio
async def test_pending_dispatch_fifo_order(fake_redis):
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        agent_id = str(uuid.uuid4())
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for tid in ids:
            await enqueue_pending_dispatch(agent_id, tid)

        for expected in ids:
            result = await dequeue_pending_dispatch(agent_id)
            assert result == expected


@pytest.mark.asyncio
async def test_pending_dispatch_empty_returns_none(fake_redis):
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        agent_id = str(uuid.uuid4())
        result = await dequeue_pending_dispatch(agent_id)
        assert result is None


# ── Unified Dispatch Tests ──────────────────────────────────────────────
#
# Phase 29 / Gateway-Sunset: the 3 dispatch-via-RPC tests removed here
# (test_dispatch_non_lead_uses_push, test_dispatch_falls_back_to_pending_queue,
# test_pre_assigned_task_gets_pushed) explicitly mocked
# `app.services.dispatch.rpc.chat_send` + `chat_send_isolated`. That code
# path is gone — auto_dispatch_task now routes through runtime-aware
# delivery (cli-bridge / host / claude-code) via dispatch_delivery.py.
# Equivalent coverage lives in:
#   - test_dispatch_routes_to_boss_after_henry.py (Boss-as-lead routing)
#   - test_agent_poll_direct_dispatch.py (cli-bridge poll-based delivery)
#   - test_task_runner_docker_cli_bridge.py (runtime branching)
# The pending-dispatch queue plumbing above remains under test.
