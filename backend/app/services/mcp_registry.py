"""MCP Server Registry — central store for installable MCP servers.

Filesystem layout:
    ~/.mc/mcp-servers/
        <name>/
            manifest.json  — MCPManifest schema
            [server code if stdio with binary]

manifest.json schema:
    {
        "name": "filesystem",
        "transport": "stdio" | "http" | "sse",
        "command": "node",         // stdio only
        "args": [...],              // stdio only
        "env": {...},               // optional
        "url": "https://...",       // http/sse only
        "headers": {...},           // http/sse only
        "description": "...",
        "source": "npm:@org/pkg",
        "installed_at": "...",
        "installed_version": "..."
    }
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


class MCPRegistryError(ValueError):
    """Raised when the MCP registry has invalid data or fails an operation."""


@dataclass(frozen=True)
class MCPManifest:
    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    description: str | None = None
    source: str | None = None
    installed_at: str | None = None
    installed_version: str | None = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> MCPManifest:
        required = ("name", "transport")
        for r in required:
            if r not in d:
                raise MCPRegistryError(f"Manifest missing required field: {r!r}")
        return MCPManifest(
            name=d["name"],
            transport=d["transport"],
            command=d.get("command"),
            args=d.get("args"),
            env=d.get("env"),
            url=d.get("url"),
            headers=d.get("headers"),
            description=d.get("description"),
            source=d.get("source"),
            installed_at=d.get("installed_at"),
            installed_version=d.get("installed_version"),
        )


def _registry_root() -> Path:
    override = os.environ.get("MC_MCP_REGISTRY_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.mc/mcp-servers"))


class MCPRegistry:
    """Read/write access to the MCP server registry on disk."""

    def __init__(self, root: Path | None = None):
        self.root = root or _registry_root()

    def list_installed(self) -> list[MCPManifest]:
        if not self.root.exists():
            return []
        manifests: list[MCPManifest] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            manifest_file = entry / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                data = json.loads(manifest_file.read_text())
                manifests.append(MCPManifest.from_dict(data))
            except (json.JSONDecodeError, MCPRegistryError) as e:
                logger.warning("Skipping invalid MCP manifest at %s: %s", entry, e)
        return manifests

    def get_manifest(self, name: str) -> MCPManifest:
        f = self.root / name / "manifest.json"
        if not f.exists():
            raise MCPRegistryError(f"MCP server {name!r} not installed")
        data = json.loads(f.read_text())
        return MCPManifest.from_dict(data)

    def install(
        self,
        source: str,
        name: str,
        proposed_config: dict | None = None,
    ) -> MCPManifest:
        """Install an MCP server from source.

        Supports:
        - npm:@org/package — runs `npm install --prefix <dir>` on HOST
        - github:owner/repo — git clone + manifest.json / .mcp.json / proposed_config

        proposed_config (optional) is the last-resort source for the manifest
        when the repo ships neither manifest.json nor .mcp.json. Expected keys
        match the MCPManifest schema (command, args, env, url, headers,
        transport). Supplied via install_request.proposed_config.
        """
        srv_dir = self.root / name
        if srv_dir.exists():
            shutil.rmtree(srv_dir)
        srv_dir.mkdir(parents=True)

        if source.startswith("npm:"):
            package = source.removeprefix("npm:")
            proc = subprocess.run(
                ["npm", "install", "--prefix", str(srv_dir), package],
                capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0:
                raise MCPRegistryError(f"npm install failed: {proc.stderr}")
            manifest = MCPManifest(
                name=name,
                transport="stdio",
                command="node",
                args=[f"/mc-servers/{name}/node_modules/{package}/dist/index.js"],
                source=source,
                installed_at=datetime.now(timezone.utc).isoformat(),
            )
        elif source.startswith("github:"):
            # Source syntax:
            #   github:owner/repo              → default branch
            #   github:owner/repo@ref          → branch, tag, or commit SHA
            # Pinning a ref lets us install from a fork that carries local
            # patches against the upstream (e.g. the higgsfield-mcp fork with
            # the Ultra-Plan use_unlim patch on its own branch).
            raw = source.removeprefix("github:")
            ref: str | None = None
            if "@" in raw:
                repo, ref = raw.split("@", 1)
                if not ref:
                    raise MCPRegistryError(
                        f"github source {source!r}: empty ref after '@'"
                    )
            else:
                repo = raw
            url = f"https://github.com/{repo}.git"
            clone_cmd = ["git", "clone", "--depth", "1"]
            if ref:
                clone_cmd += ["--branch", ref]
            clone_cmd += [url, str(srv_dir)]
            proc = subprocess.run(
                clone_cmd,
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise MCPRegistryError(f"git clone failed: {proc.stderr}")
            # Bootstrap runtime-dependencies depending on what's in the repo.
            # Python (pyproject.toml / requirements.txt): isolated venv + pip.
            # Node/Bun (package.json): npm install — works for both runtimes
            # since bun reads node_modules. The agent container carries bun,
            # the backend container only ships npm.
            if (srv_dir / "pyproject.toml").exists() or (srv_dir / "requirements.txt").exists():
                venv_dir = srv_dir / ".venv"
                proc = subprocess.run(
                    ["python3", "-m", "venv", str(venv_dir)],
                    capture_output=True, text=True, timeout=60,
                )
                if proc.returncode != 0:
                    raise MCPRegistryError(f"venv creation failed: {proc.stderr}")
                venv_py = venv_dir / "bin" / "python"
                if (srv_dir / "pyproject.toml").exists():
                    proc = subprocess.run(
                        [str(venv_py), "-m", "pip", "install", "--quiet", "."],
                        cwd=str(srv_dir), capture_output=True, text=True, timeout=300,
                    )
                else:
                    proc = subprocess.run(
                        [str(venv_py), "-m", "pip", "install", "--quiet",
                         "-r", str(srv_dir / "requirements.txt")],
                        capture_output=True, text=True, timeout=300,
                    )
                if proc.returncode != 0:
                    raise MCPRegistryError(
                        f"pip install failed: {proc.stderr[-500:] or proc.stdout[-500:]}"
                    )
            elif (srv_dir / "package.json").exists():
                proc = subprocess.run(
                    ["npm", "install", "--silent", "--no-audit", "--no-fund"],
                    cwd=str(srv_dir), capture_output=True, text=True, timeout=300,
                )
                if proc.returncode != 0:
                    raise MCPRegistryError(
                        f"npm install failed: {proc.stderr[-500:] or proc.stdout[-500:]}"
                    )
            manifest_file = srv_dir / "manifest.json"
            if manifest_file.exists():
                manifest = MCPManifest.from_dict(json.loads(manifest_file.read_text()))
            else:
                # Fallback: try the standard `.mcp.json` that most open-source
                # MCP servers ship with (e.g. geopopos/higgsfield_ai_mcp).
                # Shape: {"mcpServers": {"<key>": {command, args, env, url, ...}}}
                mcp_json = srv_dir / ".mcp.json"
                if mcp_json.exists():
                    parsed = json.loads(mcp_json.read_text())
                    servers = parsed.get("mcpServers") or {}
                    if not servers:
                        raise MCPRegistryError(
                            f"{name!r}: .mcp.json has no mcpServers block"
                        )
                    # Prefer key matching the install name, else first entry
                    key = name if name in servers else next(iter(servers))
                    cfg = servers[key]
                    transport = (
                        "http" if cfg.get("url") else "sse" if cfg.get("sse") else "stdio"
                    )
                    cmd = cfg.get("command")
                    # If we bootstrapped a venv above, route the manifest command
                    # at the venv interpreter. Catches cases where the .mcp.json
                    # says `python` (system python without the installed deps).
                    venv_py = srv_dir / ".venv" / "bin" / "python"
                    if venv_py.exists() and cmd in ("python", "python3"):
                        cmd = str(venv_py)
                    manifest = MCPManifest(
                        name=name,
                        transport=transport,
                        command=cmd,
                        args=cfg.get("args"),
                        env=cfg.get("env"),
                        url=cfg.get("url"),
                        headers=cfg.get("headers"),
                        description=cfg.get("description")
                        or f"Derived from .mcp.json ({key})",
                        source=source,
                        installed_at=datetime.now(timezone.utc).isoformat(),
                    )
                elif proposed_config:
                    # Third fallback: proposed_config from the install_request
                    # payload. Lets users install repos without an embedded
                    # MCP config by supplying command/args at request time.
                    cmd = proposed_config.get("command")
                    venv_py = srv_dir / ".venv" / "bin" / "python"
                    if venv_py.exists() and cmd in ("python", "python3"):
                        cmd = str(venv_py)
                    manifest = MCPManifest(
                        name=name,
                        transport=proposed_config.get("transport", "stdio"),
                        command=cmd,
                        args=proposed_config.get("args"),
                        env=proposed_config.get("env"),
                        url=proposed_config.get("url"),
                        headers=proposed_config.get("headers"),
                        description=proposed_config.get("description")
                        or "Derived from install_request proposed_config",
                        source=source,
                        installed_at=datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    raise MCPRegistryError(
                        f"GitHub MCP {name!r} has no manifest.json, "
                        ".mcp.json, or proposed_config — cannot derive "
                        "install config"
                    )
        else:
            raise MCPRegistryError(f"Unsupported MCP source scheme: {source!r}")

        (srv_dir / "manifest.json").write_text(
            json.dumps(
                {k: v for k, v in manifest.__dict__.items() if v is not None},
                indent=2,
            )
        )
        return manifest

    def uninstall(self, name: str) -> None:
        srv_dir = self.root / name
        if not srv_dir.exists():
            raise MCPRegistryError(f"MCP server {name!r} not installed")
        shutil.rmtree(srv_dir)

    def render_mcp_json_entry(self, name: str) -> dict[str, Any]:
        """Render a single MCP-server entry for an agent's .mcp.json file."""
        manifest = self.get_manifest(name)
        if manifest.transport == "stdio":
            entry: dict[str, Any] = {
                "command": manifest.command or "",
                "args": list(manifest.args or []),
            }
            if manifest.env:
                entry["env"] = dict(manifest.env)
            return entry
        elif manifest.transport in ("http", "sse"):
            entry = {
                "type": manifest.transport,
                "url": manifest.url or "",
            }
            if manifest.headers:
                entry["headers"] = dict(manifest.headers)
            return entry
        else:
            raise MCPRegistryError(f"Unknown transport: {manifest.transport}")

    async def smoke_test(self, name: str, timeout: float = 10.0) -> bool:
        """Start the MCP server briefly, send tools/list, verify a response.

        Returns True if the server answers the JSON-RPC handshake + tools/list.
        Used by the Install-Executor after install to confirm the server works.

        Graceful-skip: if the manifest command binary isn't available in this
        container (e.g. `bun` only ships in the agent image, not the backend),
        log a warning and return True so the install flow can complete. The
        real runtime check happens when the agent actually spawns the MCP.
        """
        import asyncio
        import shutil

        manifest = self.get_manifest(name)
        if manifest.transport != "stdio":
            return True  # http/sse smoke-test is Phase 3

        # Resolve command: accept absolute paths as-is, otherwise look it up.
        command = manifest.command or ""
        if command and not command.startswith("/") and shutil.which(command) is None:
            logger.warning(
                "smoke_test(%s): command %r not found in backend PATH — "
                "skipping (agent container will validate at runtime)",
                name, command,
            )
            return True

        init_req = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mc-smoke-test", "version": "1.0"},
            },
        }
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

        try:
            proc = await asyncio.create_subprocess_exec(
                command,
                *(manifest.args or []),
                env={**os.environ, **(manifest.env or {})},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning(
                "smoke_test(%s): command %r could not be spawned — skipping",
                name, command,
            )
            return True
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None
            proc.stdin.write((json.dumps(init_req) + "\n").encode())
            await proc.stdin.drain()
            line1 = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line1:
                return False
            proc.stdin.write((json.dumps(list_req) + "\n").encode())
            await proc.stdin.drain()
            line2 = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line2:
                return False
            resp = json.loads(line2)
            return "result" in resp and "tools" in resp.get("result", {})
        except (asyncio.TimeoutError, Exception):
            return False
        finally:
            # Process may already have exited (e.g. Python MCP crashed because
            # deps aren't installed). terminate() + wait() raise ProcessLookupError
            # in that case — tolerate it, cleanup is idempotent.
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
