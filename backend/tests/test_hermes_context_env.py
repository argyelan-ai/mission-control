"""W5 (2026-09-13): per-agent context env for the hermes host agent.

Hermes writes its MC context through its own `mc` CLI (ack/recover persist
TASK_ID/BOARD_ID/X_DISPATCH_ATTEMPT_ID, mc_cli/commands.py). Without a
per-agent path those writes landed in the SHARED /tmp/mc-context.env —
"last writer wins" set TASK_ID for every host agent (13.09. incident:
Hermes' card id 4c9bb492 repointed other agents' `mc` calls; a progress
comment landed on Hermes' card).

Under test:
  1. hermes-bridge resolves its own default path (~/.mc/agents/hermes/
     mc-context.env); an explicit MC_CONTEXT_ENV_PATH in the environment wins.
  2. load_env_from_file injects that default into EVERY subprocess env
     (entrypoint spawn, tmux commands) — explicit value still wins.
  3. docker/hermes/entrypoint.sh re-asserts the default inside the tmux
     watchdog loop: tmux windows inherit env from the tmux SERVER, not from
     the spawning client (grok lesson, live incident 2026-07-12) — without
     the in-loop export a pre-existing server would strip the variable.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "hermes-bridge.py"
ENTRYPOINT = REPO_ROOT / "docker" / "hermes" / "entrypoint.sh"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("hermes_bridge_ctx", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["hermes_bridge_ctx"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bridge_default_path_is_agent_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    monkeypatch.delenv("MC_CONTEXT_ENV_PATH", raising=False)
    mod = _load_bridge()
    assert mod.WORKSPACE == tmp_path / ".mc" / "agents" / "hermes"
    assert mod.MC_CONTEXT_ENV_PATH == str(tmp_path / ".mc" / "agents" / "hermes" / "mc-context.env")


def test_bridge_explicit_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", "/custom/mc-context.env")
    mod = _load_bridge()
    assert mod.MC_CONTEXT_ENV_PATH == "/custom/mc-context.env"


def test_load_env_from_file_injects_per_agent_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    monkeypatch.delenv("MC_CONTEXT_ENV_PATH", raising=False)
    mod = _load_bridge()
    env = mod.load_env_from_file(tmp_path / "missing-agent.env")
    assert env["MC_CONTEXT_ENV_PATH"] == str(
        tmp_path / ".mc" / "agents" / "hermes" / "mc-context.env"
    )


def test_load_env_from_file_explicit_value_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", "/explicit/mc-context.env")
    mod = _load_bridge()
    env = mod.load_env_from_file(tmp_path / "missing-agent.env")
    assert env["MC_CONTEXT_ENV_PATH"] == "/explicit/mc-context.env"


def test_entrypoint_watchdog_reasserts_per_agent_default():
    """The tmux watchdog loop re-sources agent.env every restart — agent.env
    does NOT carry MC_CONTEXT_ENV_PATH, so the loop itself must default it.
    Without this, a tmux server that predates the variable hands windows an
    env WITHOUT the per-agent path and hermes' `mc` silently falls back to
    the shared /tmp file (the exact bug W5 removes)."""
    txt = ENTRYPOINT.read_text()
    loop_idx = txt.index("while true; do set -a; . $ENV_FILE")
    loop = txt[loop_idx:txt.index("--yolo", loop_idx)]
    assert "${MC_CONTEXT_ENV_PATH:=$AGENT_DIR/mc-context.env}" in loop
    assert "export MC_CONTEXT_ENV_PATH" in loop
    # The default must be asserted INSIDE the loop, after the env source.
    assert loop.index(". $ENV_FILE") < loop.index("MC_CONTEXT_ENV_PATH")
