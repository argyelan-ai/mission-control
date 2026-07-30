#!/bin/bash
# entrypoint.sh — Boss-Host PID 1 (gestartet von launchd, com.openclaw.boss).
#
# Erstellt tmux-Session 'boss-host' mit zwei Windows:
#   Window 0 'claude' — start-claude.sh in Loop (auto-restart bei Crash, 5s Backoff)
#   Window 1 'poll'   — der GETEILTE poll.sh (docker/shared/poll.sh) im comm_v2-
#                       Nudge-Modus, HTTP-Poll an localhost:8000
#
# comm_v2 (2026-07-27): Boss laeuft jetzt auf der geteilten poll.sh statt einer
# eigenen Kopie — dieselbe Nudge+Pull-Zustellung wie die Docker-Fleet und der
# kimi-host. Die Host-Besonderheiten (kein /home/agent, tmux-Session heisst
# 'boss-host' obwohl agent.env AGENT_NAME=boss setzt, native claude statt
# openclaude) werden ausschliesslich per Env-Override unten gesetzt — die
# poll.sh selbst bleibt unveraendert. Vorbild: docker/kimi-host/entrypoint.sh.
#
# Watchdog: alle 30s prueft ob tmux-Session noch lebt; wenn nicht → neustart.
# Pendant zum Container entrypoint.sh, aber ohne Bootstrap (Token kommt aus
# agent.env, das von B1 angelegt wurde).

set -eu

SESSION="boss-host"
BASE="$HOME/.mc/agents/boss-host"

# Stabiler Agent-Checkout statt Operator-Checkout (Problemklasse "haengt am
# zufaelligen Branch", 3. Inkarnation 2026-07-30): poll.sh, Adapter-Libs und
# Boss' Arbeitsverzeichnis kommen aus einem dedizierten, auf origin/main
# gepinnten Checkout (~/.mc/checkouts/mission-control, konfigurierbar via
# MC_CHECKOUT_PATH/MC_CHECKOUT_REF) — nie aus dem Checkout, in dem der
# Operator gerade entwickelt. Bootstrap-Henne/Ei: beim allerersten Start
# existiert der Checkout noch nicht, darum liegt ensure-checkout.sh im
# Operator-Checkout (MC_REPO_PATH) und rendert/klont den stabilen.
ENSURE="${MC_REPO_PATH:-$HOME/Workspace/Projects/mission-control}/docker/shared/ensure-checkout.sh"
if [ -x "$ENSURE" ] || [ -f "$ENSURE" ]; then
    REPO="$(bash "$ENSURE")"
else
    # Aeltere Installation ohne ensure-checkout.sh: alter Pfad als Fallback.
    REPO="${MC_REPO_PATH:-$HOME/Workspace/Projects/mission-control}"
fi
POLL_SH="$REPO/docker/shared/poll.sh"
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

# ── comm_v2 Host-Overrides fuer die geteilte poll.sh ──────────────────────────
# Werden VOR dem tmux-Server-Start exportiert; der frische Server (kill-server
# unten) erbt sie und vererbt sie an beide Windows.
#   AGENT_NAME=boss-host  — SESSION_NAME in poll.sh MUSS auf die tmux-Session
#     'boss-host' zeigen. agent.env setzt AGENT_NAME=boss (Backend-Identitaet
#     laeuft ueber MC_TOKEN, nicht ueber AGENT_NAME) → hier ueberschreiben,
#     sonst zielen alle capture-pane/paste-buffer auf eine leere Session 'boss'.
#   PANE_UI_OVERRIDE=claude — Boss faehrt das native Anthropic-`claude`, das den
#     bracketed-paste-End-Marker BRAUCHT (Bug 14). Ohne Override wuerde die
#     Heuristik claude-cli 2.1.x als openclaude fehldeuten (ui-detect.sh).
#   Pfade → $BASE statt /home/agent (existiert auf dem Host nicht).
#   POLL_LIB_DIR → Repo-Checkout (die TCK-geprueften Adapter-Libs), keine Host-Kopie.
#   MSG_DELIVERY_MODE=nudge — Fleet-Standard: nur ein Weckruf, Inhalt via `mc inbox`.
export AGENT_NAME="boss-host"
export MC_API_URL="${MC_API_URL:-http://localhost:8000}"
export PANE_UI_OVERRIDE="claude"
export MSG_DELIVERY_MODE="${MSG_DELIVERY_MODE:-nudge}"
export TASK_LOCK_FILE="$BASE/.task-active.lock"
export TURN_SIGNAL_FILE="$BASE/.turn-signal"
export MSG_QUEUE_DIR="$BASE/.msg-queue"
export MSG_ACK_DIR="$BASE/.msg-acked"
export NUDGE_STATE_FILE="$BASE/.msg-nudge-state"
export TASK_PROMPT_FILE="$BASE/.current-task-prompt.txt"
export COMMENTS_PROMPT_FILE="$BASE/.new-comments-prompt.txt"
export POLL_LIB_DIR="$REPO/docker/mc-agent-base/lib"

# Vorhandenen Server auf diesem Socket komplett killen (nicht nur die Session):
# ein aus einem frueheren Boot ueberlebender tmux-Server haette die ALTEN Env-
# Vars global gecacht und wuerde sie neuen Windows vererben — die Overrides oben
# griffen dann nicht. kill-server erzwingt einen frischen Server mit dieser Env.
tmux -S "$TMUX_SOCKET" kill-server 2>/dev/null || true

# tmux-Konfig (mouse off → xterm.js Browser-Selection funktioniert nativ)
TMUX_CONF="$BASE/.tmux.conf"
cat > "$TMUX_CONF" <<'TMUX_EOF'
set -g mouse off
set -g aggressive-resize on
set -g history-limit 50000
set -g default-terminal "xterm-256color"
TMUX_EOF

start_tmux() {
    # Window 0: claude in Auto-Restart-Loop (KEIN tee — destroys PTY).
    # -c "$REPO": claude startet IM stabilen Checkout (statt cwd / oder dem
    # Operator-Checkout) — Statusline zeigt main, relative Pfade treffen den
    # deployten Code-Stand.
    tmux -S "$TMUX_SOCKET" -f "$TMUX_CONF" new-session -d -s "$SESSION" -n "claude" -c "$REPO" \
        "while true; do $BASE/start-claude.sh; echo '[entrypoint] claude exited, restart in 5s...'; sleep 5; done"

    # mouse on → Sessions web terminal scrolls output, not input history (matches
    # every other agent). Session-scoped on Boss's dedicated tmux socket.
    tmux -S "$TMUX_SOCKET" set-option -t "$SESSION" mouse on 2>/dev/null || true

    # Window 1: geteilter poll.sh (docker/shared/poll.sh) in Auto-Restart-Loop (kein tee)
    tmux -S "$TMUX_SOCKET" new-window -t "$SESSION:1" -n "poll" -c "$REPO" \
        "while true; do bash '$POLL_SH'; echo '[entrypoint] poll.sh exited, restart in 5s...'; sleep 5; done"

    # tmux-natives Pane-Logging (PTY bleibt erhalten)
    tmux -S "$TMUX_SOCKET" pipe-pane -o -t "$SESSION:0" "cat >> $LOG_DIR/claude.log"
    tmux -S "$TMUX_SOCKET" pipe-pane -o -t "$SESSION:1" "cat >> $LOG_DIR/poll.log"

    # User landet auf Window 0 (claude)
    tmux -S "$TMUX_SOCKET" select-window -t "$SESSION:0"
}

start_tmux

# Einzelnes Fenster nachstarten (fuer Watchdog — Window-weise statt Session-weise)
restart_poll_window() {
    tmux -S "$TMUX_SOCKET" new-window -t "$SESSION:1" -n "poll" -c "$REPO" \
        "while true; do bash '$POLL_SH'; echo '[entrypoint] poll.sh exited, restart in 5s...'; sleep 5; done"
    tmux -S "$TMUX_SOCKET" pipe-pane -o -t "$SESSION:1" "cat >> $LOG_DIR/poll.log"
    echo "[watchdog] poll window (1) neugestartet"
}

restart_claude_window() {
    tmux -S "$TMUX_SOCKET" new-window -t "$SESSION:0" -n "claude" -c "$REPO" \
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
