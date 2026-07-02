#!/usr/bin/env python3
"""hermes-config-patch.py — apply autonomous-worker config to ~/.hermes/config.yaml.

Idempotent: re-running makes no diff. Uses ruamel.yaml when available
(preserves comments + ordering); falls back to PyYAML.

Phase 25, ADR-030.
"""
from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path

CONFIG_PATH = Path(os.path.expanduser("~/.hermes/config.yaml"))
BACKUP_PATH = CONFIG_PATH.with_suffix(".yaml.bak-pre-25-07")
MC_REPO = Path(os.environ.get("HOME_HOST", os.path.expanduser("~"))) / "Workspace/Projects/mission-control"
VENV_PY = MC_REPO / "backend" / ".venv" / "bin" / "python3"
MC_MCP_SCRIPT = MC_REPO / "scripts" / "mc-mcp.py"

AUTONOMOUS_WORKER_PATCHES: dict = {
    "security.allow_private_urls": True,
    "terminal.env_passthrough": [
        "MC_BASE_URL", "MC_AGENT_TOKEN", "MC_TASK_ID",
        "MC_BOARD_ID", "MC_AGENT_ID",
    ],
    "approvals.timeout": 0,  # 0 = wait indefinitely (entwertet durch --yolo)
    "mcp_servers.mc.command": str(VENV_PY),
    "mcp_servers.mc.args": [str(MC_MCP_SCRIPT)],
}


def _set_dotted(d: dict, dotted_key: str, value) -> None:
    keys = dotted_key.split(".")
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _get_dotted(d: dict, dotted_key: str, default=None):
    keys = dotted_key.split(".")
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def main() -> int:
    if not CONFIG_PATH.exists():
        print(
            f"FATAL: {CONFIG_PATH} does not exist — run `hermes setup` first",
            file=sys.stderr,
        )
        return 2

    # Hermes config is often chmod 600; ensure we can write it.
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except PermissionError:
        pass

    use_ruamel = False
    try:
        from ruamel.yaml import YAML  # type: ignore
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        with CONFIG_PATH.open() as f:
            cfg = yaml.load(f)
        use_ruamel = True
    except ImportError:
        import yaml as pyyaml  # type: ignore
        with CONFIG_PATH.open() as f:
            cfg = pyyaml.safe_load(f)

    changed = []
    for dotted, expected in AUTONOMOUS_WORKER_PATCHES.items():
        current = _get_dotted(cfg, dotted)
        if current != expected:
            _set_dotted(cfg, dotted, expected)
            changed.append(f"  {dotted}: {current!r} -> {expected!r}")

    if not changed:
        print(f"OK: {CONFIG_PATH} already has all patches (idempotent no-op)")
        return 0

    if not BACKUP_PATH.exists():
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)
        print(f"Backup written: {BACKUP_PATH}")

    tmp = CONFIG_PATH.with_suffix(".yaml.tmp-25-07")
    with tmp.open("w") as f:
        if use_ruamel:
            yaml.dump(cfg, f)  # type: ignore
        else:
            import yaml as pyyaml  # type: ignore
            pyyaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)
    print(f"Patched {CONFIG_PATH} ({len(changed)} keys):")
    for line in changed:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
