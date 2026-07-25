"""
Tests for app.services.cli_versions: manifest read/bump/rollback,
installed_version (docker image inspect), fetch_latest (npm / GitHub).
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.services import cli_versions


MANIFEST = {
    "openclaude": {"version": "0.7.0"},
    "claude": {"version": "2.1.201"},
    "omp": {
        "version": "16.2.13",
        "sha256": "7cc62ef691d38c837141d7040c0730ed9c66da16a2d77ae5ccec025c099ea89d",
    },
}


@pytest.fixture
def manifest_repo(tmp_path, monkeypatch):
    """Fakes settings.mc_repo_path to a tmp dir with docker/cli-versions.json."""
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    manifest_path = docker_dir / "cli-versions.json"
    manifest_path.write_text(json.dumps(MANIFEST, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(cli_versions.settings, "mc_repo_path", str(tmp_path))
    return manifest_path


# ── TOOLS registry ────────────────────────────────────────────────────────


def test_tools_registry_has_all_tools():
    assert set(cli_versions.TOOLS.keys()) == {"openclaude", "claude", "omp", "kimi", "grok"}
    assert cli_versions.TOOLS["kimi"]["image"] == "mc-kimi-agent:latest"
    assert cli_versions.TOOLS["kimi"]["kimi_dist"] == "https://code.kimi.com/kimi-code"
    # grok ist ein Host-Tool: kein Image, brew-Cask + Binary auf dem Mac.
    assert cli_versions.TOOLS["grok"]["host"] is True
    assert "image" not in cli_versions.TOOLS["grok"]
    assert cli_versions.TOOLS["grok"]["brew_cask"] == "grok-build"
    assert cli_versions.TOOLS["openclaude"]["image"] == "mc-agent-base:latest"
    assert cli_versions.TOOLS["openclaude"]["npm"] == "@gitlawb/openclaude"
    assert cli_versions.TOOLS["claude"]["image"] == "mc-claude-agent:latest"
    assert cli_versions.TOOLS["claude"]["npm"] == "@anthropic-ai/claude-code"
    assert cli_versions.TOOLS["omp"]["image"] == "mc-omp-agent:latest"
    assert cli_versions.TOOLS["omp"]["github_repo"] == "can1357/oh-my-pi"


# ── read_manifest ─────────────────────────────────────────────────────────


def test_read_manifest_returns_parsed_json(manifest_repo):
    manifest = cli_versions.read_manifest()
    assert manifest == MANIFEST


def test_read_manifest_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_versions.settings, "mc_repo_path", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        cli_versions.read_manifest()


# ── bump_manifest ─────────────────────────────────────────────────────────


def test_bump_manifest_writes_new_version_and_returns_old_entry(manifest_repo):
    old_entry = cli_versions.bump_manifest("claude", "2.1.210")

    assert old_entry == {"version": "2.1.201"}

    manifest = json.loads(manifest_repo.read_text(encoding="utf-8"))
    assert manifest["claude"] == {"version": "2.1.210"}
    # untouched entries survive
    assert manifest["openclaude"] == MANIFEST["openclaude"]
    assert manifest["omp"] == MANIFEST["omp"]


def test_bump_manifest_with_sha256(manifest_repo):
    old_entry = cli_versions.bump_manifest("omp", "16.3.7", sha256="abc123")

    assert old_entry == MANIFEST["omp"]

    manifest = json.loads(manifest_repo.read_text(encoding="utf-8"))
    assert manifest["omp"] == {"version": "16.3.7", "sha256": "abc123"}


def test_bump_manifest_new_tool_has_no_old_entry(manifest_repo):
    old_entry = cli_versions.bump_manifest("newtool", "1.0.0")
    assert old_entry == {}

    manifest = json.loads(manifest_repo.read_text(encoding="utf-8"))
    assert manifest["newtool"] == {"version": "1.0.0"}


def test_bump_manifest_is_atomic_no_tmp_file_left(manifest_repo):
    cli_versions.bump_manifest("claude", "2.1.210")
    leftover = list(manifest_repo.parent.glob("*.tmp"))
    assert leftover == []


# ── restore_manifest_entry ────────────────────────────────────────────────


def test_restore_manifest_entry_reverts_bump(manifest_repo):
    old_entry = cli_versions.bump_manifest("claude", "2.1.210")
    cli_versions.restore_manifest_entry("claude", old_entry)

    manifest = json.loads(manifest_repo.read_text(encoding="utf-8"))
    assert manifest["claude"] == MANIFEST["claude"]


def test_restore_manifest_entry_removes_tool_if_entry_empty(manifest_repo):
    cli_versions.bump_manifest("newtool", "1.0.0")
    cli_versions.restore_manifest_entry("newtool", {})

    manifest = json.loads(manifest_repo.read_text(encoding="utf-8"))
    assert "newtool" not in manifest


# ── installed_version ─────────────────────────────────────────────────────


def test_installed_version_parses_label_from_docker_inspect():
    fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="2.1.201\n", stderr="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        result = cli_versions.installed_version("claude")

    assert result == "2.1.201"
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["docker", "image", "inspect"]
    assert "mc-claude-agent:latest" in cmd


def test_installed_version_returns_none_on_docker_error():
    fake_proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="Error: No such image"
    )
    with patch("subprocess.run", return_value=fake_proc):
        result = cli_versions.installed_version("claude")

    assert result is None


def test_installed_version_returns_none_on_empty_label():
    fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="\n", stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        result = cli_versions.installed_version("claude")

    assert result is None


def test_installed_version_returns_none_when_docker_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        result = cli_versions.installed_version("claude")

    assert result is None


def test_installed_version_returns_none_on_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10)):
        result = cli_versions.installed_version("claude")

    assert result is None


def test_installed_version_unknown_tool_returns_none():
    assert cli_versions.installed_version("nope") is None


# ── fetch_latest (npm) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_latest_npm_returns_version_no_sha(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/@anthropic-ai/claude-code/latest"
        return httpx.Response(200, json={"version": "2.1.210"})

    transport = httpx.MockTransport(handler)

    original_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(cli_versions.httpx, "AsyncClient", fake_async_client)

    result = await cli_versions.fetch_latest("claude")

    assert result == {"version": "2.1.210", "sha256": None}


# ── fetch_latest (github) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_latest_github_returns_version_and_digest(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "can1357/oh-my-pi/releases/latest" in str(request.url)
        return httpx.Response(
            200,
            json={
                "tag_name": "v16.3.6",
                "assets": [
                    {"name": "omp-darwin-arm64", "digest": "sha256:deadbeef"},
                    {
                        "name": "omp-linux-arm64",
                        "digest": "sha256:32a29b9b742ee67b9ee37411dc1c05ef3746c1a7e06ddc02d3dd4f3f2fa5015a",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)

    original_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(cli_versions.httpx, "AsyncClient", fake_async_client)

    result = await cli_versions.fetch_latest("omp")

    assert result == {
        "version": "16.3.6",
        "sha256": "32a29b9b742ee67b9ee37411dc1c05ef3746c1a7e06ddc02d3dd4f3f2fa5015a",
    }


@pytest.mark.asyncio
async def test_fetch_latest_github_no_matching_asset_returns_none_sha(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tag_name": "v16.3.6", "assets": []})

    transport = httpx.MockTransport(handler)

    original_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(cli_versions.httpx, "AsyncClient", fake_async_client)

    result = await cli_versions.fetch_latest("omp")

    assert result == {"version": "16.3.6", "sha256": None}


@pytest.mark.asyncio
async def test_fetch_latest_unknown_tool_raises(monkeypatch):
    with pytest.raises(ValueError):
        await cli_versions.fetch_latest("nope")
