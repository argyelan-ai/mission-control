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
# (OPENAI_BASE_URL / OPENAI_MODEL were just sourced above). Idempotent; ADR-060.
PATCH_SCRIPT="${HOME}/Workspace/Projects/mission-control/scripts/hermes-config-patch.py"
if [ -f "$PATCH_SCRIPT" ]; then
  python3 "$PATCH_SCRIPT" || echo "[entrypoint] WARN: hermes-config-patch rc=$?" >&2
fi

# Defensive: kill any prior session so we always boot clean
"$TMUX_BIN" kill-session -t "$SESSION" 2>/dev/null || true

# Spawn tmux with Hermes in a watchdog while-true loop (matches Boss pattern).
# --yolo bypasses dangerous-command approval prompts: Hermes runs as an
# unattended MC worker, so an interactive approval prompt would hang the
# session forever (Phase 25, ADR-030). Do not drop this flag.
"$TMUX_BIN" new-session -d -s "$SESSION" -x 220 -y 50 \
  "while true; do $HERMES_BIN --yolo; echo '[hermes] exited rc='\$'?, restarting in 5s'; sleep 5; done"

# Tee the pane to a log file for scrollback replay (host-pty-bridge consumption)
"$TMUX_BIN" pipe-pane -o -t "$SESSION":0 "cat >> $LOG_DIR/hermes.log"

# Block so launchd treats this as a long-running service.
# 30s watchdog: if tmux session dies, exit non-zero and let launchd restart us.
while "$TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; do
  sleep 30
done
echo "[entrypoint] tmux session $SESSION died — exiting for launchd restart" >&2
exit 1
