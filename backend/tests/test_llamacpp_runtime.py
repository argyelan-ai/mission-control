"""Tests for the llamacpp_docker runtime_type.

llamacpp_docker rides the same four lifecycle chains as vllm_docker
(state/start/stop/restart via SSH + docker). These tests pin the three places
where it deliberately differs — default health path, start-verification, and
label-scoped stop/restart — plus its membership in the type sets that decide
whether an agent may bind it at all.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.models.runtime import Runtime
from app.services import runtime_manager
from app.services.agent_runtime_switch import _PROBEABLE_RUNTIME_TYPES
from app.services.compose_renderer import OPENCLAUDE_IMAGE, pick_image_for_runtime
from app.services.harness_compat import _OPENAI_TYPES, is_compatible, runtime_protocol
from app.services.task_runner import _SLOW_RUNTIME_TYPES


LLAMACPP_RT = {
    "id": "llamacpp-small",
    "slug": "llamacpp-small",
    "display_name": "llama.cpp Small",
    "runtime_type": "llamacpp_docker",
    "endpoint": "http://192.0.2.10:8080/v1",
    "healthcheck_path": None,
    "container_name": "mc-llamacpp-small",
}


def _rt(**overrides) -> dict:
    return {**LLAMACPP_RT, **overrides}


# ── Type-set membership (parametrised so a future engine is checked too) ──────
# Every docker-hosted OpenAI engine must appear in all four sets. Parametrising
# over DOCKER_ENGINE_TYPES means adding a fifth engine to that tuple without
# registering it here fails loudly instead of silently losing agent binding,
# watcher probing or the slow-runtime timeout.
@pytest.mark.parametrize("rt_type", runtime_manager.DOCKER_ENGINE_TYPES)
def test_docker_engine_types_are_registered_everywhere(rt_type):
    assert rt_type in _OPENAI_TYPES
    assert rt_type in _PROBEABLE_RUNTIME_TYPES
    assert rt_type in _SLOW_RUNTIME_TYPES
    assert runtime_protocol(Runtime(
        slug=f"{rt_type}-x", display_name="X", runtime_type=rt_type,
        endpoint="http://192.0.2.10:8080/v1",
    )) == "openai"


def test_llamacpp_is_a_docker_engine_type():
    assert "llamacpp_docker" in runtime_manager.DOCKER_ENGINE_TYPES


@pytest.mark.parametrize("rt_type", runtime_manager.DOCKER_ENGINE_TYPES)
def test_docker_engines_bind_the_openclaude_agent_image(rt_type):
    """The engine image (llama.cpp/vLLM) is NOT what this picks — it returns the
    AGENT container image, and an OpenAI-protocol engine needs the shim."""
    rt = Runtime(
        slug=f"{rt_type}-x", display_name="X", runtime_type=rt_type,
        endpoint="http://192.0.2.10:8080/v1", enabled=True,
    )
    assert pick_image_for_runtime(rt) == OPENCLAUDE_IMAGE


@pytest.mark.parametrize("harness,expected", [("openclaude", True), ("omp", True), ("claude", False)])
def test_llamacpp_harness_compatibility(harness, expected):
    rt = Runtime(
        slug="llamacpp-small", display_name="X", runtime_type="llamacpp_docker",
        endpoint="http://192.0.2.10:8080/v1",
    )
    assert is_compatible(harness, rt) is expected


# ── State ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_state_defaults_to_health_path_when_row_leaves_it_unset():
    """llama-server's /health is 200 only once the model is loaded; the shared
    /v1/models default must not leak into a llamacpp row."""
    probe = AsyncMock(return_value=True)
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("running", "", 0))), \
         patch.object(runtime_manager, "_probe_http", new=probe):
        state = await runtime_manager.get_runtime_state(_rt())
    assert state["state"] == "ready"
    probe.assert_awaited_once_with("http://192.0.2.10:8080/v1", "/health")


@pytest.mark.asyncio
async def test_state_explicit_healthcheck_path_wins_over_default():
    probe = AsyncMock(return_value=True)
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("running", "", 0))), \
         patch.object(runtime_manager, "_probe_http", new=probe):
        await runtime_manager.get_runtime_state(_rt(healthcheck_path="/v1/models"))
    probe.assert_awaited_once_with("http://192.0.2.10:8080/v1", "/v1/models")


@pytest.mark.asyncio
async def test_state_warming_when_container_up_but_http_dead():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("running", "", 0))), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=False)):
        state = await runtime_manager.get_runtime_state(_rt())
    assert state["state"] == "warming"
    assert state["http_reachable"] is False


@pytest.mark.asyncio
async def test_state_stopped_when_container_missing():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("not_found", "", 0))):
        state = await runtime_manager.get_runtime_state(_rt())
    assert state["state"] == "stopped"


@pytest.mark.asyncio
async def test_state_failed_on_ssh_error():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=OSError("ssh down"))):
        state = await runtime_manager.get_runtime_state(_rt())
    assert state["state"] == "failed"
    assert state["container_status"] == "ssh_error"


# ── Start ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_uses_docker_start_when_container_exists():
    ssh = AsyncMock(return_value=("running", "", 0))
    with patch.object(runtime_manager, "_ssh_run", new=ssh):
        result = await runtime_manager.start_runtime(_rt())
    assert result["ok"] is True
    assert "mc-llamacpp-small" in result["message"]
    assert any("docker start mc-llamacpp-small" in c.args[0] for c in ssh.await_args_list)


@pytest.mark.asyncio
async def test_start_via_launch_command_verifies_llama_server_process():
    """Success path: container appears AND llama-server runs inside it."""
    rt = _rt(container_name="", launch_command="docker run ... llama.cpp:server")
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))), \
         patch.object(runtime_manager, "verify_spark_container_started",
                      new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "verify_llamacpp_process_started",
                      new=AsyncMock(return_value="serving")) as verify_llama, \
         patch.object(runtime_manager, "verify_spark_vllm_process_started",
                      new=AsyncMock(return_value="absent")) as verify_vllm:
        result = await runtime_manager.start_runtime(rt)
    assert result["ok"] is True
    verify_llama.assert_awaited_once()
    # The vllm check matches on "vllm serve" and must never gate a llamacpp start.
    verify_vllm.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_fails_when_container_up_but_server_dead():
    """The failure mode reproduced locally: a bad -hf spec makes llama-server
    exit in <1s while the container has already been created."""
    rt = _rt(container_name="", launch_command="docker run ... llama.cpp:server")
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))), \
         patch.object(runtime_manager, "verify_spark_container_started",
                      new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "verify_llamacpp_process_started",
                      new=AsyncMock(return_value="absent")):
        result = await runtime_manager.start_runtime(rt)
    assert result["ok"] is False
    assert "llama-server" in result["message"]


@pytest.mark.asyncio
async def test_start_fails_when_no_container_appears():
    rt = _rt(container_name="", launch_command="docker run ... llama.cpp:server")
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))), \
         patch.object(runtime_manager, "verify_spark_container_started",
                      new=AsyncMock(return_value=False)):
        result = await runtime_manager.start_runtime(rt)
    assert result["ok"] is False
    assert "mc.runtime.slug" in result["message"]


@pytest.mark.asyncio
async def test_start_without_container_or_launch_command_fails():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))):
        result = await runtime_manager.start_runtime(_rt(container_name="", launch_command=""))
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_verify_llamacpp_process_started_reads_docker_top():
    """Container id found by label, llama-server visible in `docker top`."""
    async def fake_ssh(cmd, **kwargs):
        if "docker ps -q" in cmd:
            return ("abc123", "", 0)
        if "docker top" in cmd:
            return (
                "UID PID PPID C STIME TTY TIME CMD\n"
                "root 1 0 0 20:33 ? 00:00:04 /app/llama-server -hf X --port 8080\n",
                "", 0,
            )
        return ("", "", 1)

    with patch.object(runtime_manager, "_verify_poll_interval", 0), \
         patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        assert await runtime_manager.verify_llamacpp_process_started("llamacpp-small") == "serving"


@pytest.mark.asyncio
async def test_verify_llamacpp_process_started_false_when_process_gone():
    async def fake_ssh(cmd, **kwargs):
        if "docker ps -q" in cmd:
            return ("abc123", "", 0)
        if "docker top" in cmd:
            return ("UID PID PPID C STIME TTY TIME CMD\nroot 1 0 0 ? sleep infinity\n", "", 0)
        return ("", "", 1)

    with patch.object(runtime_manager, "_verify_poll_interval", 0), \
         patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        assert await runtime_manager.verify_llamacpp_process_started(
            "llamacpp-small", timeout=0.01
        ) == "absent"


# ── Stop ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stop_uses_docker_stop_on_known_container():
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", new=ssh):
        result = await runtime_manager.stop_runtime(_rt())
    assert result["ok"] is True
    assert any("docker stop" in c.args[0] for c in ssh.await_args_list)


@pytest.mark.asyncio
async def test_stop_without_container_name_never_evicts_the_neighbouring_vllm():
    """The eviction sweep also kills sparkrun_*_solo / vllm_node. A llama.cpp
    runtime shares the box on purpose, so it must stop by its own label only."""
    ssh = AsyncMock(return_value=("abc123", "", 0))
    with patch.object(runtime_manager, "_ssh_run", new=ssh), \
         patch.object(runtime_manager, "evict_spark_runtime_containers",
                      new=AsyncMock(return_value={"ok": True, "message": "evicted", "stopped": []})) as evict:
        result = await runtime_manager.stop_runtime(_rt(container_name=""))
    assert result["ok"] is True
    evict.assert_not_awaited()
    issued = " ".join(c.args[0] for c in ssh.await_args_list)
    assert "label=mc.runtime.slug=llamacpp-small" in issued
    assert "sparkrun_" not in issued
    assert "vllm_node" not in issued


@pytest.mark.asyncio
async def test_stop_by_label_reports_ok_when_nothing_runs():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))):
        result = await runtime_manager.stop_llamacpp_containers_by_label("llamacpp-small")
    assert result["ok"] is True
    assert result["stopped"] == []


@pytest.mark.asyncio
async def test_stop_by_label_surfaces_docker_failure():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "daemon down", 1))):
        result = await runtime_manager.stop_llamacpp_containers_by_label("llamacpp-small")
    assert result["ok"] is False
    assert "daemon down" in result["message"]


# ── Restart ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_restart_uses_docker_restart_on_known_container():
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", new=ssh):
        result = await runtime_manager.restart_runtime(_rt())
    assert result["ok"] is True
    assert any("docker restart mc-llamacpp-small" in c.args[0] for c in ssh.await_args_list)


@pytest.mark.asyncio
async def test_restart_without_container_name_discovers_by_label_only():
    ssh = AsyncMock(return_value=("abc123", "", 0))
    with patch.object(runtime_manager, "_ssh_run", new=ssh), \
         patch.object(runtime_manager, "_running_solo_containers",
                      new=AsyncMock(return_value=["sparkrun_deadbeef_solo"])) as solo:
        result = await runtime_manager.restart_runtime(_rt(container_name=""))
    assert result["ok"] is True
    solo.assert_not_awaited()
    assert any("docker restart abc123" in c.args[0] for c in ssh.await_args_list)


@pytest.mark.asyncio
async def test_restart_without_any_container_fails_honestly():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))):
        result = await runtime_manager.restart_runtime(_rt(container_name=""))
    assert result["ok"] is False
    assert "Kein laufender" in result["message"]


# ── Recipe-switch gate stays closed ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_recipe_switch_rejects_llamacpp(async_session, auth_client):
    """sparkrun recipes only exist for vLLM — a llamacpp row must 422, not get a
    launch_command that would start vLLM under it."""
    rt = Runtime(
        slug="llamacpp-gate", display_name="llama.cpp Gate",
        runtime_type="llamacpp_docker", endpoint="http://192.0.2.10:8080/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()

    resp = await auth_client.post(
        "/api/v1/runtimes/llamacpp-gate/switch-recipe",
        json={"recipe": "@example/some-nvfp4-vllm"},
    )
    assert resp.status_code == 422
    assert "vllm_docker" in resp.json()["detail"]


# ── Seed template ────────────────────────────────────────────────────────────
def test_seed_ships_a_disabled_llamacpp_template():
    entries = [rt for rt in runtime_manager.load_registry()
               if rt["runtime_type"] == "llamacpp_docker"]
    assert entries, "expected a llamacpp_docker example in config/runtimes.json"
    for entry in entries:
        assert entry["enabled"] is False, "the template must never seed enabled"
        # Lifecycle finds the container by label — a template without it teaches
        # the wrong pattern and start-verification would fail on every copy.
        assert f"mc.runtime.slug={entry['id']}" in (entry.get("launch_command") or "")
