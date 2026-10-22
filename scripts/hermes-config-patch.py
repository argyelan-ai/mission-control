#!/usr/bin/env python3
"""hermes-config-patch.py — apply autonomous-worker config to ~/.hermes/config.yaml.

Idempotent: re-running makes no diff. Uses ruamel.yaml when available
(preserves comments + ordering); falls back to PyYAML.

Model block (ADR-064): from the runtime binding MC writes into Hermes'
agent.env — OPENAI_BASE_URL / OPENAI_MODEL -> model.base_url / model.default,
and MC_CONTEXT_WINDOW -> model.context_length. The window matters as much as
the name: without it Hermes guesses from its own model-name table, and with a
stale name it guessed GLM's 202,752 while the runtime served 1,048,576 (live
22.10.2026 — wrong chat ring AND compaction thresholds sized for the wrong
model). Callers: docker/hermes/entrypoint.sh (native tmux driver) and
scripts/hermes-bridge.py (ACP driver, before every chat-daemon start).

Respects HERMES_HOME (Hermes' own override for its config dir).

Phase 25, ADR-030.
"""
from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path

def _hermes_home() -> Path:
    """Hermes' config dir — HERMES_HOME wins, like in hermes_constants.py."""
    override = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(override).expanduser() if override else Path(os.path.expanduser("~/.hermes"))


CONFIG_PATH = _hermes_home() / "config.yaml"
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
    # hermes-agent spawns MCP servers with a SANITIZED env (_build_safe_env) —
    # neither the TUI's nor the gateway's process env reaches mc-mcp.py. The
    # per-server env block is the only reliable channel. mc-mcp.py reads the
    # agent token from this file at CALL time (survives token rotation + stale
    # long-lived gateways). Live incident 2026-07-12: without this, agent-scoped
    # calls (comments/checklist/finish) failed for days.
    "mcp_servers.mc.env": {
        "MC_AGENT_ENV_FILE": str(
            Path(os.environ.get("HOME_HOST", os.path.expanduser("~")))
            / ".mc" / "agents" / "hermes" / "agent.env"
        ),
    },
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


def _del_dotted(d: dict, dotted_key: str) -> bool:
    keys = dotted_key.split(".")
    for k in keys[:-1]:
        if not isinstance(d, dict) or k not in d:
            return False
        d = d[k]
    if isinstance(d, dict) and keys[-1] in d:
        del d[keys[-1]]
        return True
    return False


#: agent.env key carrying the bound runtime's context window
#: (backend/app/routers/internal.py CONTEXT_WINDOW_ENV).
CONTEXT_WINDOW_ENV = "MC_CONTEXT_WINDOW"


def _context_window_from_env() -> int | None:
    raw = (os.environ.get(CONTEXT_WINDOW_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        if raw:
            print(
                f"WARN: {CONTEXT_WINDOW_ENV}={raw!r} is not an integer — "
                "model.context_length left as is",
                file=sys.stderr,
            )
        return None
    return value if value > 0 else None


def _model_patches_from_env() -> dict:
    """Derive model.* patches from the runtime binding in the env.

    Returns patches only when BOTH base_url and model are present, so a
    hand-written model block (e.g. deliberately cloud) is never clobbered.
    The window joins them (as an int — Hermes rejects "1M"-style strings)
    when MC sent one. ADR-064.
    """
    base_url = os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("OPENAI_MODEL")
    if not (base_url and model):
        return {}
    patches: dict = {
        "model.provider": "custom",
        "model.base_url": base_url,
        "model.default": model,
    }
    window = _context_window_from_env()
    if window is not None:
        patches["model.context_length"] = window
    return patches


def _stale_window_removals(cfg: dict, model_patches: dict) -> list[str]:
    """Keys to DROP: a model.context_length left over from another model.

    When MC switches the model but has no window for the new runtime, the
    old number would describe the PREVIOUS model — worse than none, because
    Hermes trusts model.context_length over its own detection. Only on a
    real model change: with the same model and no window from MC, a
    hand-set value is left alone.
    """
    if not model_patches or "model.context_length" in model_patches:
        return []
    if _get_dotted(cfg, "model.context_length") is None:
        return []
    if _get_dotted(cfg, "model.default") == model_patches.get("model.default"):
        return []
    return ["model.context_length"]


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

    if cfg is None:
        cfg = {}

    changed = []
    model_patches = _model_patches_from_env()
    # Decided BEFORE model.default is overwritten below.
    removals = _stale_window_removals(cfg, model_patches)
    patches = {**AUTONOMOUS_WORKER_PATCHES, **model_patches}
    for dotted, expected in patches.items():
        current = _get_dotted(cfg, dotted)
        if current != expected:
            _set_dotted(cfg, dotted, expected)
            changed.append(f"  {dotted}: {current!r} -> {expected!r}")
    for dotted in removals:
        current = _get_dotted(cfg, dotted)
        if _del_dotted(cfg, dotted):
            changed.append(f"  {dotted}: {current!r} -> (removed, belonged to the previous model)")

    if not changed:
        print(f"OK: {CONFIG_PATH} already has all patches (idempotent no-op)")
        return 0

    if not BACKUP_PATH.exists():
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)
        print(f"Backup written: {BACKUP_PATH}")

    tmp = CONFIG_PATH.with_suffix(".yaml.tmp-25-07")
    # The temp file is created 0600 BEFORE anything is written into it: a
    # plain open() honours the umask (usually 0644) and os.replace() would
    # then hand that wider mode to config.yaml — which holds provider keys.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(tmp, 0o600)  # an existing leftover temp keeps its old mode otherwise
        with os.fdopen(fd, "w") as f:
            if use_ruamel:
                yaml.dump(cfg, f)  # type: ignore
            else:
                import yaml as pyyaml  # type: ignore
                pyyaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    print(f"Patched {CONFIG_PATH} ({len(changed)} keys):")
    for line in changed:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
