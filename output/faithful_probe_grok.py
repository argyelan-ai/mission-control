#!/usr/bin/env python3
"""Faithful probe (Rex/Lead): real tmux, real pane — does MC_CONTEXT_ENV_PATH
arrive at the grok pane, and does it survive a session restart?

Measures BOTH levels the lead demanded, for the grok writer:
  1. tmux show-environment -t <session> MC_CONTEXT_ENV_PATH
  2. in the pane itself:  echo $MC_CONTEXT_ENV_PATH   (send-keys + capture-pane)
The fake grok TUI is an interactive /bin/sh, so the echo runs INSIDE the pane
process tree exactly like the agent's own `mc` calls would.

Topology detail: a dummy session guarantees the tmux SERVER already runs before
the bridge's new-session — otherwise the first client would seed the server env
(belt 1) and hide exactly the bug Rex's probe found. After each phase the probe
reports PASS/FAIL per level.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("grok_bridge", REPO / "scripts" / "grok-bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules["grok_bridge"] = bridge
spec.loader.exec_module(bridge)

sock = f"mc-probe-{uuid.uuid4().hex[:8]}"
tmp = Path(tempfile.mkdtemp(prefix="mc-grok-probe-"))
wrap = tmp / "tmux-wrap"
wrap.write_text(f"#!/bin/sh\nexec tmux -L {sock} \"$@\"\n")
wrap.chmod(0o755)
fake_grok = tmp / "fake-grok"
fake_grok.write_text("#!/bin/sh\necho '❯ ready'\nexec /bin/sh -i\n")
fake_grok.chmod(0o755)
(tmp / "agent.env").write_text("MC_BASE_URL=http://localhost:8000\n")
ctx = tmp / ".mc" / "agents" / "grok" / "mc-context.env"
ctx.parent.mkdir(parents=True, exist_ok=True)

bridge.TMUX_BIN = str(wrap)
bridge.GROK_BIN = str(fake_grok)
bridge.ENV_FILE = tmp / "agent.env"
bridge.WORKSPACE = tmp / "workspace"
bridge.LOG_DIR = tmp / "logs"
bridge.SESSION = "grok-probe"
bridge.MC_CONTEXT_ENV_PATH = str(ctx)
_orig_write = bridge.write_task_context_env
bridge.write_task_context_env = lambda task, path=str(ctx): _orig_write(task, path)

def tmux(*args):
    return subprocess.run([str(wrap), *args], capture_output=True, text=True, timeout=15)

def session_env():
    r = tmux("show-environment", "-t", bridge.SESSION, "MC_CONTEXT_ENV_PATH")
    out = r.stdout.strip()
    return out.split("=", 1)[1] if r.returncode == 0 and "=" in out else "<NOT SET>"

def in_pane_echo():
    for _ in range(5):  # wait for the interactive shell prompt, then echo
        tmux("send-keys", "-t", bridge.SESSION, "-l", "echo PANE=$MC_CONTEXT_ENV_PATH")
        tmux("send-keys", "-t", bridge.SESSION, "Enter")
        time.sleep(1.5)
        for line in bridge.capture_pane().splitlines():
            if line.startswith("PANE="):
                v = line.split("=", 1)[1].strip()
                if v:
                    return v
    return "<NOT SET>"

def phase(name):
    print(f"--- {name} ---")
    started = bridge.start_grok_session()
    print("start_grok_session:", started["status"])
    bridge.deliver_task_context({"id": "probe-task", "board_id": "probe-board", "dispatch_attempt_id": "probe-att"})
    env1, env2 = session_env(), in_pane_echo()
    expect = str(ctx)
    print("show-environment -t session MC_CONTEXT_ENV_PATH:", env1)
    print("echo $MC_CONTEXT_ENV_PATH (in pane):            ", env2)
    ok = env1 == expect and env2 == expect
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok

subprocess.run([str(wrap), "new-session", "-d", "-s", "dummy", "sleep 120"], check=True)
results = [phase("PHASE 1 — fresh session + deliver_task_context")]
tmux("kill-session", "-t", bridge.SESSION)
results.append(phase("PHASE 2 — after session RESTART (new session, same server)"))
tmux("kill-server")
print("--- SUMMARY ---")
print("all phases PASS" if all(results) else "AT LEAST ONE PHASE FAILED")
sys.exit(0 if all(results) else 1)
