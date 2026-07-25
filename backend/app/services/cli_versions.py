"""
CLI tool version management.

Tracks the pinned CLI versions baked into agent Docker images
(docker/cli-versions.json, see ADR for CLI-Tool-Updates) and compares them
against the installed images (via `mc.cli.version` Docker labels) and the
latest upstream releases (npm registry / GitHub releases).

Five tools today:
- openclaude: mc-agent-base image, published on npm
- claude: mc-claude-agent image, published on npm (@anthropic-ai/claude-code)
- omp: mc-omp-agent image, published as GitHub releases (can1357/oh-my-pi)
- kimi: mc-kimi-agent image, published as pinned binaries on code.kimi.com
  (latest = plain-text version endpoint, sha256 from the release manifest)
- grok: HOST tool ("host": True) — no Docker image. Installed via Homebrew
  cask `grok-build` on the Mac; version is read over the host cli-bridge
  (`grok --version`), latest from the Homebrew cask API, update = `brew
  upgrade` via the bridge. Host tools skip the image/recreate machinery.
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
    "kimi": {"image": "mc-kimi-agent:latest", "kimi_dist": "https://code.kimi.com/kimi-code"},
    "grok": {"host": True, "binary": "grok", "brew_cask": "grok-build"},
}

_HTTP_TIMEOUT = 15.0
_OMP_ASSET_NAME = "omp-linux-arm64"
_KIMI_ASSET_PLATFORM = "linux-arm64"
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
    docker isn't available, or the label is empty. Host tools have no image —
    their installed version comes from ``installed_version_host``."""
    config = TOOLS.get(tool)
    if config is None or config.get("host"):
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


async def installed_version_host(tool: str) -> str | None:
    """Installed version of a HOST tool ("host": True), read over the host
    cli-bridge (`GET /host-cli/{binary}/version` → runs `<binary> --version`
    on the Mac). Returns None on unknown tool, non-host tool, or any
    bridge/network failure — the update check treats None as "unknown",
    never as an error."""
    config = TOOLS.get(tool)
    if config is None or not config.get("host"):
        return None
    binary = config.get("binary", tool)
    try:
        async with httpx.AsyncClient(
            base_url=settings.free_code_bridge_url, timeout=_HTTP_TIMEOUT
        ) as client:
            resp = await client.get(f"/host-cli/{binary}/version")
            if resp.status_code != 200:
                return None
            return resp.json().get("version") or None
    except httpx.HTTPError:
        return None


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

        if "kimi_dist" in config:
            # code.kimi.com/kimi-code/latest → plain-text version ("0.29.1",
            # via 302 to the CDN); the release manifest carries per-platform
            # sha256 checksums — official pins, no TOFU download needed.
            base = config["kimi_dist"]
            resp = await client.get(f"{base}/latest", follow_redirects=True)
            resp.raise_for_status()
            version = resp.text.strip()
            sha256 = None
            m_resp = await client.get(
                f"{base}/binaries/{version}/manifest.json", follow_redirects=True
            )
            if m_resp.status_code == 200:
                platform = m_resp.json().get("platforms", {}).get(_KIMI_ASSET_PLATFORM, {})
                sha256 = platform.get("checksum")
            return {"version": version, "sha256": sha256}

        if config.get("host") and "brew_cask" in config:
            resp = await client.get(
                f"https://formulae.brew.sh/api/cask/{config['brew_cask']}.json"
            )
            resp.raise_for_status()
            return {"version": str(resp.json()["version"]), "sha256": None}

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
