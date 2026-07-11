#!/bin/bash
# entrypoint.sh — Boss-Host PID 1 (gestartet von launchd, com.openclaw.boss).
#
# Erstellt tmux-Session 'boss-host' mit zwei Windows:
#   Window 0 'claude' — start-claude.sh in Loop (auto-restart bei Crash, 5s Backoff)
#   Window 1 'poll'   — poll.sh in Loop (HTTP-Poll an localhost:8000)
#
# Watchdog: alle 30s prueft ob tmux-Session noch lebt; wenn nicht → neustart.
# Pendant zum Container entrypoint.sh, aber ohne Bootstrap (Token kommt aus
# agent.env, das von B1 angelegt wurde).

set -eu

SESSION="boss-host"
BASE="$HOME/.mc/agents/boss-host"
LOG_DIR="$BASE/logs"
TMUX_SOCKET="$BASE/.tmux.sock"

mkdir -p "$LOG_DIR"

# agent.env in entrypoint-Scope sourcen, damit tmux/claude/poll alle die
# MC_API_URL + MC_AGENT_TOKEN env-Vars erben. Ohne das muss claude im
# laufenden Task manuell `source agent.env` aufrufen (siehe E2 Smoke-Test).
ENV_FILE="$BASE/agent.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Vorhandene Session defensiv killen
tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true

# tmux-Konfig (mouse off → xterm.js Browser-Selection funktioniert nativ)
TMUX_CONF="$BASE/.tmux.conf"
cat > "$TMUX_CONF" <<'TMUX_EOF'
set -g mouse off
set -g aggressive-resize on
set -g history-limit 50000
set -g default-terminal "xterm-256color"
TMUX_EOF

start_tmux() {
    # Window 0: claude in Auto-Restart-Loop (KEIN tee — destroys PTY)
    tmux -S "$TMUX_SOCKET" -f "$TMUX_CONF" new-session -d -s "$SESSION" -n "claude" \
        "while true; do $BASE/start-claude.sh; echo '[entrypoint] claude exited, restart in 5s...'; sleep 5; done"

    # mouse on → Sessions web terminal scrolls output, not input history (matches
    # every other agent). Session-scoped on Boss's dedicated tmux socket.
    tmux -S "$TMUX_SOCKET" set-option -t "$SESSION" mouse on 2>/dev/null || true

    # Window 1: poll.sh in Auto-Restart-Loop (kein tee)
    tmux -S "$TMUX_SOCKET" new-window -t "$SESSION:1" -n "poll" \
        "while true; do bash $BASE/poll.sh; echo '[entrypoint] poll.sh exited, restart in 5s...'; sleep 5; done"

    # tmux-natives Pane-Logging (PTY bleibt erhalten)
    tmux -S "$TMUX_SOCKET" pipe-pane -o -t "$SESSION:0" "cat >> $LOG_DIR/claude.log"
    tmux -S "$TMUX_SOCKET" pipe-pane -o -t "$SESSION:1" "cat >> $LOG_DIR/poll.log"

    # User landet auf Window 0 (claude)
    tmux -S "$TMUX_SOCKET" select-window -t "$SESSION:0"
}

start_tmux

# Einzelnes Fenster nachstarten (fuer Watchdog — Window-weise statt Session-weise)
restart_poll_window() {
    tmux -S "$TMUX_SOCKET" new-window -t "$SESSION:1" -n "poll" \
        "while true; do bash $BASE/poll.sh; echo '[entrypoint] poll.sh exited, restart in 5s...'; sleep 5; done"
    tmux -S "$TMUX_SOCKET" pipe-pane -o -t "$SESSION:1" "cat >> $LOG_DIR/poll.log"
    echo "[watchdog] poll window (1) neugestartet"
}

restart_claude_window() {
    tmux -S "$TMUX_SOCKET" new-window -t "$SESSION:0" -n "claude" \
        "while true; do $BASE/start-claude.sh; echo '[entrypoint] claude exited, restart in 5s...'; sleep 5; done"
    tmux -S "$TMUX_SOCKET" pipe-pane -o -t "$SESSION:0" "cat >> $LOG_DIR/claude.log"
    echo "[watchdog] claude window (0) neugestartet"
}

# PID 1 Watchdog: tmux-Server UND einzelne Windows am Leben halten.
# Session-Check allein reicht nicht — wenn nur Window 1 (poll) stirbt,
# bleibt die Session bestehen, aber Tasks haengen (kein Dispatch).
while true; do
    sleep 30
    if ! tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null; then
        echo "[watchdog] tmux session '$SESSION' weg — neustart"
        start_tmux
        continue
    fi
    # Window-weiser Check: listet aktive Windows, sucht Indizes 0 + 1
    WINDOWS=$(tmux -S "$TMUX_SOCKET" list-windows -t "$SESSION" -F '#{window_index}' 2>/dev/null | tr '\n' ' ')
    case " $WINDOWS " in
        *" 0 "*) ;;
        *) restart_claude_window ;;
    esac
    case " $WINDOWS " in
        *" 1 "*) ;;
        *) restart_poll_window ;;
    esac
done
