"""
CLI tool version management.

Tracks the pinned CLI versions baked into agent Docker images
(docker/cli-versions.json, see ADR for CLI-Tool-Updates) and compares them
against the installed images (via `mc.cli.version` Docker labels) and the
latest upstream releases (npm registry / GitHub releases).

Three tools today:
- openclaude: mc-agent-base image, published on npm
- claude: mc-claude-agent image, published on npm (@anthropic-ai/claude-code)
- omp: mc-omp-agent image, published as GitHub releases (can1357/oh-my-pi)
"""

import json
import os
import subprocess
from pathlib import Path

import httpx

from app.config import settings

TOOLS: dict[str, dict] = {
    "openclaude": {"image": "mc-agent-base:latest", "npm": "@gitlawb/openclaude"},
    "claude": {"image": "mc-claude-agent:latest", "npm": "@anthropic-ai/claude-code"},
    "omp": {"image": "mc-omp-agent:latest", "github_repo": "can1357/oh-my-pi"},
}

_HTTP_TIMEOUT = 15.0
_OMP_ASSET_NAME = "omp-linux-arm64"
_DOCKER_TIMEOUT = 10


def _manifest_path() -> Path:
    return Path(settings.mc_repo_path) / "docker" / "cli-versions.json"


def _write_manifest(manifest: dict) -> None:
    """Atomic write: tmp file + os.replace so a crash mid-write never
    leaves cli-versions.json truncated or half-written."""
    path = _manifest_path()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def read_manifest() -> dict:
    with open(_manifest_path(), encoding="utf-8") as f:
        return json.load(f)


def bump_manifest(tool: str, version: str, sha256: str | None = None) -> dict:
    """Sets tool's version (+ optional sha256) in the manifest. Returns the
    OLD entry (or {} if the tool was not present) so a caller can roll back
    via restore_manifest_entry() if the follow-up image build fails."""
    manifest = read_manifest()
    old_entry = manifest.get(tool, {})

    new_entry: dict = {"version": version}
    if sha256 is not None:
        new_entry["sha256"] = sha256
    manifest[tool] = new_entry

    _write_manifest(manifest)
    return old_entry


def restore_manifest_entry(tool: str, entry: dict) -> None:
    """Rolls a tool's manifest entry back to a previously captured value.
    An empty entry ({}) means the tool did not exist before and is removed."""
    manifest = read_manifest()
    if entry:
        manifest[tool] = entry
    else:
        manifest.pop(tool, None)
    _write_manifest(manifest)


def installed_version(tool: str) -> str | None:
    """Reads the `mc.cli.version` Docker label off the locally built image.
    Returns None if the tool is unknown, the image doesn't exist locally,
    docker isn't available, or the label is empty."""
    config = TOOLS.get(tool)
    if config is None:
        return None

    cmd = [
        "docker", "image", "inspect",
        "--format", '{{index .Config.Labels "mc.cli.version"}}',
        config["image"],
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_DOCKER_TIMEOUT
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if proc.returncode != 0:
        return None

    value = proc.stdout.strip()
    return value or None


async def fetch_latest(tool: str) -> dict:
    """Queries the upstream source of truth for a tool's latest release.
    Returns {"version": str, "sha256": str | None}."""
    config = TOOLS.get(tool)
    if config is None:
        raise ValueError(f"unknown cli tool: {tool}")

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        if "npm" in config:
            resp = await client.get(
                f"https://registry.npmjs.org/{config['npm']}/latest"
            )
            resp.raise_for_status()
            return {"version": resp.json()["version"], "sha256": None}

        resp = await client.get(
            f"https://api.github.com/repos/{config['github_repo']}/releases/latest"
        )
        resp.raise_for_status()
        data = resp.json()
        version = data["tag_name"].lstrip("v")

        sha256 = None
        for asset in data.get("assets", []):
            if asset.get("name") == _OMP_ASSET_NAME:
                digest = asset.get("digest")
                if digest and digest.startswith("sha256:"):
                    sha256 = digest.removeprefix("sha256:")
                break

        return {"version": version, "sha256": sha256}
