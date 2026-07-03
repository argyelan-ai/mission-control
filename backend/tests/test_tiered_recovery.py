"""Tests for REC-01 / REC-02 / REC-03 — Tiered Recovery (Plan 06-05).

Verifies `_run_tiered_recovery` extension of
`app.services.task_runner._check_stale_in_progress`.

Tier sequence (D-15 — task stays `in_progress` throughout):
  Tier 1: gateway_client.send_heartbeat with asyncio.timeout(10) — agent reachable?
  Tier 2: per-runtime restart
            - docker → docker_agent_sync.restart_docker_agent_container
            - host   → cli_terminal._host_agent_lifecycle("restart")
            - cli-bridge / openclaw → skip (no process to restart)
  Tier 3: build recap via task_context_builder.build_recovery_context;
          dispatch via runtime_context.get_session_context_for_runtime
  Tier 4: emit_event severity='error' (auto-Discord) — operator notified
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ────────────────────────────────────────────────────────────


async def _make_fixtures(make_board, make_agent, make_task, *, runtime: str = "docker"):
    """Create a (board, agent, task) tuple with the requested runtime."""
    board = await make_board(name=f"Recovery Board {runtime}", slug=f"rec-{runtime}-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name=f"Worker-{runtime}",
        board_id=board.id,
        is_board_lead=False,
        role="developer",
        agent_runtime=runtime,
    )
    task = await make_task(
        board_id=board.id,
        title=f"Stale task on {runtime}",
        status="in_progress",
        assigned_agent_id=agent.id,
    )
    return board, agent, task


@asynccontextmanager
async def _patched_test_session():
    """Yield a session bound to the test SQLite engine."""
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


# ── Test 1: Tier 1 heartbeat probe ─────────────────────────────────────
# Removed (Phase 29 / Wave 4 cleanup, D-21 in task_runner.py): Tier 1
# (gateway_client.send_heartbeat) was removed with the Openclaw sunset.
# Recovery now jumps straight to Tier 2 (per-runtime restart). There is
# no more cross-runtime "is the agent alive?" probe for cli-bridge /
# host / claude-code agents. See task_runner._run_tiered_recovery.


# ── Test 2: Tier 2 docker runtime calls restart_docker_agent_container ──


@pytest.mark.asyncio
async def test_tier2_docker_runtime_calls_restart_docker_agent_container(
    fake_redis, make_board, make_agent, make_task,
):
    """When agent.agent_runtime == 'docker' and Tier 1 fails, restart is called."""
    _board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task, runtime="docker",
    )

    from app.services.task_runner import task_runner

    restart_spy = MagicMock(return_value={"status": "restarted", "container": "mc-agent-test"})

    # Phase 29 / Wave 4 cleanup: Tier 1 (gateway_client.send_heartbeat) was
    # removed with the Openclaw sunset — no patch needed anymore, Tier 2 is
    # called directly.

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.docker_agent_sync.restart_docker_agent_container", restart_spy), \
         patch('app.services.task_runner.logger'), \
         patch("asyncio.sleep", new_callable=AsyncMock):  # skip 30s wait
        async with _patched_test_session() as session:
            await task_runner._run_tiered_recovery(session, task, agent)

    assert restart_spy.call_count >= 1, "restart_docker_agent_container should be called for runtime=docker"
    # Verify agent passed positionally
    call_args = restart_spy.call_args
    assert call_args.args[0] is agent or (call_args.args and call_args.args[0].id == agent.id)


# ── Test 3: Tier 2 host runtime calls _host_agent_lifecycle("restart") ──


@pytest.mark.asyncio
async def test_tier2_host_runtime_calls_host_agent_lifecycle_restart(
    fake_redis, make_board, make_agent, make_task,
):
    """When agent.agent_runtime == 'host' and Tier 1 fails, _host_agent_lifecycle is called."""
    _board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task, runtime="host",
    )

    from app.services.task_runner import task_runner

    lifecycle_spy = AsyncMock(return_value={"ok": True, "action": "restart", "agent": "boss"})

    # Phase 29 / Wave 4 cleanup: Tier 1 (gateway_client.send_heartbeat) was
    # removed with the Openclaw sunset — no patch needed anymore, Tier 2 is
    # called directly.

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
         patch("app.routers.cli_terminal._host_agent_lifecycle", lifecycle_spy), \
         patch('app.services.task_runner.logger'), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        async with _patched_test_session() as session:
            await task_runner._run_tiered_recovery(session, task, agent)

    assert lifecycle_spy.await_count >= 1, "_host_agent_lifecycle should be called for runtime=host"
    call_args = lifecycle_spy.await_args
    assert call_args.args[0] is agent or (call_args.args and call_args.args[0].id == agent.id)
    assert call_args.args[1] == "restart"


# ── Test 4: Tier 3 uses build_recovery_context + get_session_context_for_runtime ──




# ── Test 5: Tier 4 emits agent.recovery_failed severity=error (Discord auto) ──


