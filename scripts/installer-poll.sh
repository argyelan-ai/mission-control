#!/bin/bash
# installer-poll.sh — minimaler Task-Poller für den Host-Installer.
#
# Wird als zweites tmux-Window in der `plugins-shell`-Session gestartet,
# pollt /api/v1/agent/me/poll alle 10s, leitet Task-Prompts an Window 0
# (claude+sonnet) weiter. Read-only — keine Status-Mutationen, der Installer
# (claude-CLI) selbst macht via `mc ack/comment/review` weiter.
#
# Unterschiede zum Container-poll.sh (docker/shared/poll.sh):
# - Kein recycler-Marker
# - Kein `/clear`-State-Tracking (Installer-Tasks sind kurz, Conversation
#   überleben)
# - Kein FIRST_POLL-Recovery (host-process, kein Restart-Cycle)
# - Stagnation/Crashed-Detection ausgelassen — Installer-Sessions sind
#   interaktiv und der Operator sieht den Live-Pane
#
# Env (von cli-bridge gesetzt):
#   MC_API_URL, MC_AGENT_TOKEN

set -u

SESSION="plugins-shell"
TARGET="${SESSION}:0"
TMUX_BIN="${TMUX_BIN:-/opt/homebrew/bin/tmux}"
[ -x "$TMUX_BIN" ] || TMUX_BIN="$(command -v tmux)"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
LAST_TASK_ID=""
LOG_PREFIX="[installer-poll]"

log() {
  echo "$(date +%H:%M:%S) $LOG_PREFIX $*"
}

if [ -z "${MC_AGENT_TOKEN:-}" ] || [ -z "${MC_API_URL:-}" ]; then
  log "ERROR: MC_AGENT_TOKEN or MC_API_URL missing — abort"
  exit 1
fi

log "Started. Polling $MC_API_URL every ${POLL_INTERVAL}s, target=$TARGET"

while true; do
  RESPONSE=$(curl -sf --max-time 8 \
    -H "Authorization: Bearer $MC_AGENT_TOKEN" \
    "$MC_API_URL/api/v1/agent/me/poll" 2>/dev/null || echo '{"state":"error"}')

  STATE=$(echo "$RESPONSE" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('state', 'error'))
except Exception:
    print('error')
" 2>/dev/null || echo "error")

  if [ "$STATE" = "new_task" ]; then
    TASK_ID=$(echo "$RESPONSE" | python3 -c "
import json, sys
print(json.load(sys.stdin)['task']['id'])
" 2>/dev/null)

    if [ -n "$TASK_ID" ] && [ "$TASK_ID" != "$LAST_TASK_ID" ]; then
      PROMPT_FILE="/tmp/installer-task-${TASK_ID}.txt"
      echo "$RESPONSE" | python3 -c "
import json, sys
data = json.load(sys.stdin)
with open('$PROMPT_FILE', 'w') as f:
    f.write(data['task']['prompt'])
" 2>/dev/null

      if [ -f "$PROMPT_FILE" ]; then
        log "Task $TASK_ID dispatching → $TARGET"
        # tmux paste statt send-keys: Multi-Line Prompts ohne Quoting-Hölle
        "$TMUX_BIN" load-buffer "$PROMPT_FILE" 2>/dev/null
        "$TMUX_BIN" paste-buffer -t "$TARGET" 2>/dev/null
        sleep 0.5
        "$TMUX_BIN" send-keys -t "$TARGET" Enter 2>/dev/null
        LAST_TASK_ID="$TASK_ID"
        log "Task $TASK_ID delivered (fire-and-forget)"
      else
        log "WARN: prompt-file write failed for $TASK_ID"
      fi
    fi
  elif [ "$STATE" = "error" ]; then
    : # silent — backend könnte temporär down sein, polling läuft weiter
  fi

  sleep "$POLL_INTERVAL"
done
