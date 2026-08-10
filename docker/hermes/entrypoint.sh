#!/bin/bash
# entrypoint.sh — runs Hermes binary in tmux 'hermes-worker' with watchdog loop.
# Auto-launched by hermes-bridge.py (Phase 24). Pattern source: Boss entrypoint.
set -eu

AGENT_DIR="${HOME}/.mc/agents/hermes"
ENV_FILE="$AGENT_DIR/agent.env"
SESSION="hermes-worker"
LOG_DIR="$AGENT_DIR/logs"
HERMES_BIN="${HOME}/.local/bin/hermes"
TMUX_BIN="$(which tmux || echo /opt/homebrew/bin/tmux)"

mkdir -p "$LOG_DIR"

# Load agent.env into the current shell so tmux inherits MC_AGENT_TOKEN etc.
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
else
  echo "[entrypoint] WARN: $ENV_FILE missing — Hermes will start without MC env" >&2
fi

# Render model provider into ~/.hermes/config.yaml from the runtime binding
# (OPENAI_BASE_URL / OPENAI_MODEL were just sourced above). Idempotent; ADR-064.
#
# Repo root: MC_REPO_PATH (same convention as kimi-host/boss-host
# entrypoints — the checkout may live anywhere, may have any folder name;
# hardcoding Mark's path here made this unusable for any other install).
REPO_ROOT="${MC_REPO_PATH:-$HOME/Workspace/Projects/mission-control}"
PATCH_SCRIPT="$REPO_ROOT/scripts/hermes-config-patch.py"
# Prefer the backend venv's python3 — the SAME interpreter already relied on
# for the mcp_servers.mc.command entry this same patcher writes, so it is
# known to have PyYAML/ruamel.yaml installed. Plain `python3` resolved
# through launchd's PATH is not guaranteed to be the same interpreter (or to
# have those packages), and a failure there used to be invisible: it was
# discarded to DEVNULL by the bridge's Popen call, not just under-logged
# here (fixed in hermes-bridge.py — see ENTRYPOINT_LOG).
PATCH_PYTHON="$REPO_ROOT/backend/.venv/bin/python3"
if [ ! -x "$PATCH_PYTHON" ]; then
  PATCH_PYTHON="python3"
fi
if [ -f "$PATCH_SCRIPT" ]; then
  "$PATCH_PYTHON" "$PATCH_SCRIPT" || echo "[entrypoint] WARN: hermes-config-patch rc=$? (interpreter: $PATCH_PYTHON)" >&2
else
  echo "[entrypoint] WARN: hermes-config-patch.py not found at $PATCH_SCRIPT (MC_REPO_PATH=${MC_REPO_PATH:-unset}) — config.yaml NOT synced" >&2
fi

# Defensive: kill any prior session so we always boot clean
"$TMUX_BIN" kill-session -t "$SESSION" 2>/dev/null || true

# Spawn tmux with Hermes in a watchdog while-true loop (matches Boss pattern).
# --yolo bypasses dangerous-command approval prompts: Hermes runs as an
# unattended MC worker, so an interactive approval prompt would hang the
# session forever (Phase 25, ADR-030). Do not drop this flag.
#
# The loop (re-)sources agent.env ITSELF: tmux windows inherit env from the
# tmux SERVER, not from the client running new-session — the `set -a` block
# above never reaches the window process when the server already exists
# (grok lesson, see grok-bridge _grok_launch_shell_cmd). Live incident
# 2026-07-12: hermes ran 5 days with a 4.4KB quote-mangled MC_AGENT_TOKEN —
# mc comment/finish failed, tasks hung in review. In-loop sourcing also
# refreshes a rotated token on every watchdog restart.
# MC_API_URL: the mc CLI reads MC_API_URL (not agent.env's MC_BASE_URL) and
# would silently fall back to its localhost default — correct on this host,
# but only by accident. Export it explicitly (agent.env wins if it ever
# carries the key) so agent-side `mc inbox` calls in nudge mode are
# deterministic. Twin of grok-bridge's _grok_launch_shell_cmd export.
"$TMUX_BIN" new-session -d -s "$SESSION" -x 220 -y 50 \
  "while true; do set -a; . $ENV_FILE; set +a; : \"\${MC_API_URL:=http://localhost:8000}\"; export MC_API_URL; $HERMES_BIN --yolo; echo '[hermes] exited rc='\$'?, restarting in 5s'; sleep 5; done"

# Web-terminal scroll: forward wheel events to the native Hermes TUI so mouse
# scroll walks the OUTPUT, not Hermes' input history. Every cli-bridge agent
# sets `mouse on`; Hermes was the one session without it, so xterm mapped the
# wheel to arrow keys in the alt-screen TUI (reported by Mark). Session-scoped
# (-t "$SESSION") so other host tmux sessions are untouched.
"$TMUX_BIN" set-option -t "$SESSION" mouse on 2>/dev/null || true

# Tee the pane to a log file for scrollback replay (host-pty-bridge consumption)
"$TMUX_BIN" pipe-pane -o -t "$SESSION":0 "cat >> $LOG_DIR/hermes.log"

# Block so launchd treats this as a long-running service.
# 30s watchdog: if tmux session dies, exit non-zero and let launchd restart us.
while "$TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; do
  sleep 30
done
echo "[entrypoint] tmux session $SESSION died — exiting for launchd restart" >&2
exit 1
