"""Tests for the vLLM Add-Flow (Phase 14): list_vllm_containers, add_vllm_runtime,
_derive_vllm_endpoint, and the corresponding /vllm/discover + /vllm router endpoints.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services import runtime_manager


# ── Helpers ───────────────────────────────────────────────────────────────


@pytest.fixture
def empty_registry(tmp_path: Path, monkeypatch):
    """Point _REGISTRY_PATH to an empty registry in tmp_path."""
    registry_file = tmp_path / "runtimes.json"
    registry_file.write_text("[]\n")
    monkeypatch.setattr(runtime_manager, "_REGISTRY_PATH", registry_file)
    return registry_file


@pytest.fixture
def seeded_registry(tmp_path: Path, monkeypatch):
    """Registry with one pre-existing vllm_docker entry."""
    registry_file = tmp_path / "runtimes.json"
    seed = [
        {
            "id": "qwen36-vllm",
            "display_name": "Qwen 3.6 vLLM",
            "runtime_type": "vllm_docker",
            "container_name": "mc-qwen36-vllm",
            "endpoint": "http://192.0.2.10:8003/v1",
            "ui_order": 5,
        }
    ]
    registry_file.write_text(json.dumps(seed, indent=2) + "\n")
    monkeypatch.setattr(runtime_manager, "_REGISTRY_PATH", registry_file)
    return registry_file


# ── add_vllm_runtime ──────────────────────────────────────────────────────


def test_add_vllm_runtime_creates_entry(empty_registry):
    rt = runtime_manager.add_vllm_runtime(
        container_name="mc-qwen36-vllm",
        display_name="Qwen 3.6",
        endpoint="http://192.0.2.10:8003/v1",
        role_tags=["coder"],
    )
    assert rt["id"] == "qwen36-vllm"
    assert rt["runtime_type"] == "vllm_docker"
    assert rt["container_name"] == "mc-qwen36-vllm"
    assert rt["endpoint"] == "http://192.0.2.10:8003/v1"
    assert rt["role_tags"] == ["coder"]
    assert rt["supports_tools"] is True
    assert rt["supports_streaming"] is True
    assert rt["enabled"] is True
    assert rt["healthcheck_path"] == "/v1/models"

    persisted = json.loads(empty_registry.read_text())
    assert len(persisted) == 1
    assert persisted[0]["container_name"] == "mc-qwen36-vllm"


def test_add_vllm_runtime_idempotent(empty_registry):
    runtime_manager.add_vllm_runtime("mc-foo-vllm", "Foo", "http://x:1/v1")
    second = runtime_manager.add_vllm_runtime("mc-foo-vllm", "Foo Again", "http://y:2/v1")
    persisted = json.loads(empty_registry.read_text())
    assert len(persisted) == 1
    assert second["display_name"] == "Foo"  # original preserved


def test_add_vllm_runtime_unique_slug(empty_registry):
    """Two containers whose slugs collide get a -2 suffix on the second."""
    runtime_manager.add_vllm_runtime("mc-qwen36-vllm", "First", "http://x:1/v1")
    second = runtime_manager.add_vllm_runtime("qwen36-vllm", "Second", "http://x:2/v1")
    assert second["id"] == "qwen36-vllm-2"
    persisted = json.loads(empty_registry.read_text())
    assert len(persisted) == 2


def test_add_vllm_runtime_no_mc_prefix(empty_registry):
    """container_name without 'mc-' prefix becomes the slug as-is."""
    rt = runtime_manager.add_vllm_runtime("custom-vllm", "Custom", "http://x:1/v1")
    assert rt["id"] == "custom-vllm"


# ── _derive_vllm_endpoint ─────────────────────────────────────────────────


def test_derive_vllm_endpoint_extracts_port(monkeypatch):
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "192.0.2.10")
    assert (
        runtime_manager._derive_vllm_endpoint("0.0.0.0:8003->8000/tcp")
        == "http://192.0.2.10:8003/v1"
    )
    # Multiple mappings (IPv4 + IPv6) — pick first matching.
    assert (
        runtime_manager._derive_vllm_endpoint("0.0.0.0:8003->8000/tcp, [::]:8003->8000/tcp")
        == "http://192.0.2.10:8003/v1"
    )
    # Non-matching internal port → empty.
    assert runtime_manager._derive_vllm_endpoint("0.0.0.0:9090->9090/tcp") == ""
    # Empty.
    assert runtime_manager._derive_vllm_endpoint("") == ""


# ── list_vllm_containers ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_vllm_containers_filters_non_vllm(empty_registry, monkeypatch):
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "192.0.2.10")
    # Mock docker ps output with mixed images.
    docker_output = "\n".join([
        json.dumps({"Names": "mc-qwen36-vllm", "Image": "vllm/vllm-openai:v0.6.3", "Ports": "0.0.0.0:8003->8000/tcp", "State": "running"}),
        json.dumps({"Names": "lms-server", "Image": "lmstudio/lms:latest", "Ports": "0.0.0.0:1234->1234/tcp", "State": "running"}),
        json.dumps({"Names": "mc-qwen-vllm", "Image": "vllm/vllm-openai:v0.6.0", "Ports": "0.0.0.0:8001->8000/tcp", "State": "running"}),
        json.dumps({"Names": "redis", "Image": "redis:7", "Ports": "", "State": "running"}),
    ])
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=(docker_output, "", 0))):
        containers = await runtime_manager.list_vllm_containers()
    names = [c["container_name"] for c in containers]
    assert names == ["mc-qwen36-vllm", "mc-qwen-vllm"]
    assert all(c["is_registered"] is False for c in containers)
    assert containers[0]["endpoint"] == "http://192.0.2.10:8003/v1"


@pytest.mark.asyncio
async def test_list_vllm_containers_marks_registered(seeded_registry, monkeypatch):
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "192.0.2.10")
    docker_output = json.dumps({
        "Names": "mc-qwen36-vllm",
        "Image": "vllm/vllm-openai:v0.6.3",
        "Ports": "0.0.0.0:8003->8000/tcp",
        "State": "running",
    })
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=(docker_output, "", 0))):
        containers = await runtime_manager.list_vllm_containers()
    assert len(containers) == 1
    assert containers[0]["is_registered"] is True
    assert containers[0]["registered_id"] == "qwen36-vllm"


@pytest.mark.asyncio
async def test_list_vllm_containers_handles_ssh_failure(empty_registry):
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=RuntimeError("ssh down"))):
        containers = await runtime_manager.list_vllm_containers()
    assert containers == []


@pytest.mark.asyncio
async def test_list_vllm_containers_skips_wrapper_without_vllm_process(empty_registry, monkeypatch):
    """sparkrun-eugr-vllm style wrapper: 'vllm' in image name, but no port
    binding and no vllm serve process inside the container — must be skipped.
    Regression guard for the May 2026 incident where the CUDA sleeper
    container showed up in Discovery without a URL.
    """
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "192.0.2.10")
    docker_ps_output = json.dumps({
        "Names": "sparkrun_1299888bb0f6_solo",
        "Image": "sparkrun-eugr-vllm",
        "Ports": "",  # host network → no port mapping
        "State": "running",
    })

    async def fake_ssh(cmd: str, **_kw):  # **_kw: host= kwarg (ADR-048)
        if cmd.startswith("docker ps"):
            return (docker_ps_output, "", 0)
        # docker top — return only a sleep process, no vllm
        return ("bash -c sleep infinity\n", "", 0)

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        containers = await runtime_manager.list_vllm_containers()
    assert containers == []


@pytest.mark.asyncio
async def test_list_vllm_containers_finds_host_network_vllm_via_docker_top(empty_registry, monkeypatch):
    """A real vllm server in a host-network container has no Ports in docker ps,
    but the vllm process is visible via docker top. The endpoint should be
    reconstructed from the --port argument in the cmdline.
    """
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "192.0.2.10")
    docker_ps_output = json.dumps({
        "Names": "real-vllm-host-net",
        "Image": "vllm/vllm-openai:v0.6.3",
        "Ports": "",
        "State": "running",
    })

    async def fake_ssh(cmd: str, **_kw):  # **_kw: host= kwarg (ADR-048)
        if cmd.startswith("docker ps"):
            return (docker_ps_output, "", 0)
        # docker top — return a vllm serve process on port 8005
        return (
            "/usr/bin/python3 /usr/local/bin/vllm serve Qwen/Qwen3.6-35B-A3B-FP8 "
            "--host 0.0.0.0 --port 8005 --trust-remote-code\n",
            "", 0,
        )

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        containers = await runtime_manager.list_vllm_containers()
    assert len(containers) == 1
    assert containers[0]["container_name"] == "real-vllm-host-net"
    assert containers[0]["endpoint"] == "http://192.0.2.10:8005/v1"


# ── _container_runs_vllm_server ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_container_runs_vllm_server_detects_serve_process(monkeypatch):
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "10.0.0.5")
    top_out = "python3 /usr/local/bin/vllm serve Qwen/Q --host 0.0.0.0 --port 8007 --tp 1"
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=(top_out, "", 0))):
        is_vllm, endpoint = await runtime_manager._container_runs_vllm_server("any")
    assert is_vllm is True
    assert endpoint == "http://10.0.0.5:8007/v1"


@pytest.mark.asyncio
async def test_container_runs_vllm_server_defaults_to_8000_when_no_port_flag(monkeypatch):
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "10.0.0.5")
    top_out = "python3 /usr/local/bin/vllm serve Qwen/Q --host 0.0.0.0"
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=(top_out, "", 0))):
        is_vllm, endpoint = await runtime_manager._container_runs_vllm_server("any")
    assert is_vllm is True
    assert endpoint == "http://10.0.0.5:8000/v1"


@pytest.mark.asyncio
async def test_container_runs_vllm_server_returns_false_for_sleeper():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("bash -c sleep infinity", "", 0))):
        is_vllm, endpoint = await runtime_manager._container_runs_vllm_server("sleeper")
    assert is_vllm is False
    assert endpoint == ""


@pytest.mark.asyncio
async def test_container_runs_vllm_server_handles_ssh_failure():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=RuntimeError("nope"))):
        is_vllm, endpoint = await runtime_manager._container_runs_vllm_server("x")
    assert is_vllm is False
    assert endpoint == ""


# ── Router endpoints ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_vllm_endpoint_creates_runtime(auth_client, empty_registry):
    """POST /api/v1/runtimes/vllm with valid body creates entry and returns 200."""
    with patch.object(runtime_manager, "get_runtime_state", new=AsyncMock(
        return_value={"state": "stopped", "http_reachable": False, "container_status": "not_found"}
    )):
        resp = await auth_client.post("/api/v1/runtimes/vllm", json={
            "container_name": "mc-qwen36-vllm",
            "display_name": "Qwen 3.6",
            "endpoint": "http://192.0.2.10:8003/v1",
            "role_tags": ["coder"],
        })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["container_name"] == "mc-qwen36-vllm"
    assert data["runtime_type"] == "vllm_docker"
    assert data["state"] == "stopped"
    persisted = json.loads(empty_registry.read_text())
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_post_vllm_endpoint_rejects_invalid_container_name(auth_client, empty_registry):
    """Validator rejects shell-injection-style container names."""
    resp = await auth_client.post("/api/v1/runtimes/vllm", json={
        "container_name": "foo; rm -rf /",
        "display_name": "Bad",
        "endpoint": "http://x:1/v1",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_vllm_discover_returns_containers(auth_client, empty_registry, monkeypatch):
    monkeypatch.setattr(runtime_manager.settings, "dgx_ssh_host", "192.0.2.10")
    docker_output = json.dumps({
        "Names": "mc-qwen36-vllm",
        "Image": "vllm/vllm-openai:v0.6.3",
        "Ports": "0.0.0.0:8003->8000/tcp",
        "State": "running",
    })
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=(docker_output, "", 0))):
        resp = await auth_client.get("/api/v1/runtimes/vllm/discover")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "containers" in data
    assert len(data["containers"]) == 1
    assert data["containers"][0]["container_name"] == "mc-qwen36-vllm"
    assert data["containers"][0]["is_registered"] is False
