"""Incident 2026-09-17 — a harness switch that changes the container image
must either succeed or report failure. It used to do neither.

The recreate was invoked WITHOUT ``-p mission-control``: compose derived the
project from the compose files' directory, did not recognise the running
``mc-agent-*`` container as its own, tried to CREATE it and died with
"Conflict. The container name is already in use". The OLD container kept
running the OLD image while DB/config already said the new harness — and the
switch reported success anyway. The operator learned about it from a black
terminal.

These tests pin the three layers of the fix:

  1. the compose call carries the project flag (fails if it is removed),
  2. a failed container step raises SwitchContainerStepFailed AFTER rolling
     back — never a success verdict with a warning,
  3. after a recreate the running container's image is verified against the
     image the new harness requires; a mismatch is a failed switch.

Helpers mirror tests/test_agent_harness_switch.py 1:1.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.compose_renderer import pick_image_for_harness
from app.services.agent_runtime_switch import (
    SwitchContainerStepFailed,
    switch_agent_runtime,
)


# ── auto-redis fixture (identical to test_agent_harness_switch.py) ──────────


@pytest.fixture(autouse=True)
def _patched_redis(fake_redis):
    async def _async_get_redis():
        return fake_redis

    with patch("app.services.agent_runtime_switch.get_redis", _async_get_redis), \
         patch("app.services.sse.get_redis", _async_get_redis), \
         patch("app.redis_client.get_redis", _async_get_redis):
        yield fake_redis


# ── helpers ────────────────────────────────────────────────────────────────


async def _mk_runtime(session, *, slug: str, runtime_type: str = "cloud") -> Runtime:
    rt = Runtime(
        slug=slug,
        display_name=f"RT {slug}",
        runtime_type=runtime_type,
        endpoint="http://example.com/v1",
        model_identifier=f"model-{slug}",
        enabled=True,
        supports_tools=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _mk_omp_agent(session, *, runtime_id) -> Agent:
    agent = Agent(
        name=f"A-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        harness="omp",
        runtime_id=runtime_id,
        cli_plugins=[],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _success_patches(*, restart_result=None, running_image=None):
    """Side-effect stubs for a cross-image switch (omp → openclaude).

    restart_docker_agent_container returns ``restart_result`` (default: a
    successful recreate); inspect_container_image returns ``running_image``
    (default: the image the new harness requires, i.e. verification passes).
    """
    return [
        patch(
            "app.services.agent_runtime_switch.sync_docker_agent_files",
            AsyncMock(return_value={}),
        ),
        patch(
            "app.services.agent_runtime_switch.restart_docker_agent_container",
            side_effect=lambda *a, **k: restart_result
            or {"status": "recreated", "container": "mc-agent-x", "mode": "recreate"},
        ),
        patch(
            "app.services.agent_runtime_switch.wait_for_agent_healthy",
            AsyncMock(return_value={"healthy": True, "reason": "ok"}),
        ),
        patch(
            "app.services.agent_runtime_switch.write_compose_agents",
            AsyncMock(return_value={"changed": "false"}),
        ),
        patch(
            "app.services.agent_runtime_switch.inspect_container_image",
            MagicMock(return_value=running_image),
        ),
    ]


async def _switch_omp_to_openclaude(session, agent, rt):
    return await switch_agent_runtime(session, agent, rt.id, new_harness="openclaude")


# ── 1. The compose call carries the project flag ───────────────────────────


def test_force_recreate_cmd_carries_project_flag(tmp_path, monkeypatch):
    """The recreate command MUST address the mission-control project
    explicitly — without ``-p`` compose derives the project from the compose
    files' directory, does not recognise the running mc-agent-* container as
    its own, and dies on the name conflict (incident 2026-09-17). This test
    fails if the flag is removed or moved behind the -f arguments where a
    later refactor could drop it."""
    from app.config import settings
    from app.services.docker_agent_sync import (
        COMPOSE_PROJECT_NAME,
        restart_docker_agent_container,
    )

    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker").mkdir(exist_ok=True)
    (tmp_path / "docker" / "docker-compose.agents.yml").write_text("services: {}\n")
    monkeypatch.setattr(settings, "mc_repo_path", str(tmp_path))

    agent = Agent(name="d035a498-switch-verdict", agent_runtime="cli-bridge")

    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        result = restart_docker_agent_container(agent, force_recreate=True)

    assert result["status"] == "recreated", result
    cmd = run_mock.call_args.args[0]
    # The flag sits immediately after `docker compose` — before anything that
    # could make a refactor believe the project is derived elsewhere.
    assert cmd[0:4] == ["docker", "compose", "-p", COMPOSE_PROJECT_NAME]
    assert COMPOSE_PROJECT_NAME == "mission-control"


def test_project_name_matches_compose_network():
    """The constant is not a second source of truth: docker-compose.yml pins
    the default network to ``mission-control_default`` and the deploy path
    (ADR-083, scripts/stt-server/README.md) invokes with the same literal —
    the code-side carrier must keep matching them."""
    from app.services.docker_agent_sync import COMPOSE_PROJECT_NAME

    repo_root = None
    for candidate in (
        __import__("pathlib").Path(__file__).resolve().parents[2],
        __import__("pathlib").Path(__file__).resolve().parents[1],
    ):
        if (candidate / "docker-compose.yml").is_file():
            repo_root = candidate
            break
    assert repo_root is not None, "docker-compose.yml not found above backend/"

    compose_text = (repo_root / "docker-compose.yml").read_text()
    assert f"name: {COMPOSE_PROJECT_NAME}_default" in compose_text, (
        "COMPOSE_PROJECT_NAME drifted from the network docker-compose.yml pins — "
        "recreates would join a different project than the fleet runs in"
    )


# ── 2. A failed container step is a FAILED switch ──────────────────────────


@pytest.mark.asyncio
async def test_failed_recreate_raises_container_step_failure(async_session):
    """Recreate dies on the name conflict → SwitchContainerStepFailed, and
    the agent is rolled back to the binding that matches the still-running
    container. Before the fix this path returned success with a warning."""
    rt_old = await _mk_runtime(async_session, slug="omp-old")
    rt_new = await _mk_runtime(async_session, slug="cloud-new")
    agent = await _mk_omp_agent(async_session, runtime_id=rt_old.id)

    conflict = (
        "error: Conflict. The container name \"/mc-agent-x\" is already in "
        "use by container abc123. You have to remove (or rename) that "
        "container to be able to reuse that name."
    )
    p = _success_patches(restart_result={"status": conflict, "container": "mc-agent-x", "mode": "recreate"})
    with p[0], p[1], p[2], p[3], p[4]:
        with pytest.raises(SwitchContainerStepFailed) as exc_info:
            await _switch_omp_to_openclaude(async_session, agent, rt_new)

    assert "container recreate failed" in str(exc_info.value)
    await async_session.refresh(agent)
    # Rollback: the binding matches the container that is still running.
    assert agent.harness == "omp"
    assert agent.runtime_id == rt_old.id


@pytest.mark.asyncio
async def test_failed_recreate_is_not_success_with_warning(async_session):
    """The verdict must not degrade into the old lying shape: a switch result
    (no exception) carrying the failure as a mere warning."""
    rt_old = await _mk_runtime(async_session, slug="omp-old2")
    rt_new = await _mk_runtime(async_session, slug="cloud-new2")
    agent = await _mk_omp_agent(async_session, runtime_id=rt_old.id)

    p = _success_patches(
        restart_result={
            "status": "error: docker compose up timed out (90s)",
            "container": "mc-agent-x",
            "mode": "recreate",
        }
    )
    with p[0], p[1], p[2], p[3], p[4]:
        with pytest.raises(SwitchContainerStepFailed):
            await _switch_omp_to_openclaude(async_session, agent, rt_new)


# ── 3. Post-switch image verification — prove it, don't trust it ───────────


@pytest.mark.asyncio
async def test_image_mismatch_fails_switch(async_session):
    """The recreate reports success but the container still runs the OLD
    image → the switch must fail loudly (rollback + raise), whatever the
    database says."""
    rt_old = await _mk_runtime(async_session, slug="omp-old3")
    rt_new = await _mk_runtime(async_session, slug="cloud-new3")
    agent = await _mk_omp_agent(async_session, runtime_id=rt_old.id)

    expected = pick_image_for_harness("openclaude", rt_new)
    assert expected, "openclaude harness must map to a concrete image"
    old_image = pick_image_for_harness("omp", rt_old)
    assert old_image != expected

    p = _success_patches(running_image=old_image)
    with p[0], p[1], p[2], p[3], p[4]:
        with pytest.raises(SwitchContainerStepFailed) as exc_info:
            await _switch_omp_to_openclaude(async_session, agent, rt_new)

    assert "image verification failed" in str(exc_info.value)
    assert expected in str(exc_info.value)
    await async_session.refresh(agent)
    assert agent.harness == "omp"
    assert agent.runtime_id == rt_old.id


@pytest.mark.asyncio
async def test_image_verification_passes_switch_succeeds(async_session):
    """Control: recreate runs the EXPECTED image → switch commits, no
    exception, verdict success."""
    rt_old = await _mk_runtime(async_session, slug="omp-old4")
    rt_new = await _mk_runtime(async_session, slug="cloud-new4")
    agent = await _mk_omp_agent(async_session, runtime_id=rt_old.id)

    expected = pick_image_for_harness("openclaude", rt_new)
    p = _success_patches(running_image=expected)
    with p[0], p[1], p[2], p[3], p[4]:
        result = await _switch_omp_to_openclaude(async_session, agent, rt_new)

    assert result.image_switched is True
    await async_session.refresh(agent)
    assert agent.harness == "openclaude"
    assert agent.runtime_id == rt_new.id


@pytest.mark.asyncio
async def test_unreadable_image_is_not_a_mismatch(async_session):
    """``docker inspect`` failing (None) is NOT a mismatch — the health probe
    below stays the safety net; the switch proceeds to it instead of failing
    on an unreadable container."""
    rt_old = await _mk_runtime(async_session, slug="omp-old5")
    rt_new = await _mk_runtime(async_session, slug="cloud-new5")
    agent = await _mk_omp_agent(async_session, runtime_id=rt_old.id)

    p = _success_patches(running_image=None)
    with p[0], p[1], p[2], p[3], p[4]:
        result = await _switch_omp_to_openclaude(async_session, agent, rt_new)

    await async_session.refresh(agent)
    assert agent.harness == "openclaude"
    assert result.image_switched is True


# ── 4. Router surfaces the failed switch as 503, not 200 ───────────────────


@pytest.mark.asyncio
async def test_patch_harness_switch_container_failure_returns_503(
    auth_client, async_session
):
    """The operator must get an error verdict from the API — before the fix
    the endpoint returned 200 while the agent sat on a black terminal."""
    rt = await _mk_runtime(async_session, slug="omp-router")
    agent = await _mk_omp_agent(async_session, runtime_id=rt.id)

    with patch(
        "app.services.agent_runtime_switch.switch_agent_runtime",
        AsyncMock(
            side_effect=SwitchContainerStepFailed(
                "Container-Schritt nach Switch fehlgeschlagen "
                "(container recreate failed) — Rollback ausgefuehrt."
            )
        ),
    ):
        resp = await auth_client.patch(
            f"/api/v1/agents/{agent.id}", json={"harness": "openclaude"}
        )

    assert resp.status_code == 503, resp.text
    assert "recreate failed" in resp.json()["detail"]
