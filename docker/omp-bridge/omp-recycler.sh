#!/usr/bin/env bash
# docker/omp-bridge/omp-recycler.sh — Window 2 recycler (ADR-049, supersedes the
# ADR-045 headless-bridge version).
#
# In the native-TUI runtime there are TWO long-lived processes to keep alive:
#   * Window 0 = the native omp TUI      (pgrep: `omp .*--hook`)
#   * Window 1 = the bridge poll driver  (pgrep: `bridge.py`)
#
# Responsibilities (kept deliberately narrow so it never races the bridge):
#   1. bridge.py dead        -> respawn Window 1 (the driver must always poll).
#   2. TUI dead AND idle     -> relaunch Window 0 (so the Sessions pane is never
#                               blank). NEVER while a task is active: during a
#                               task the bridge OWNS TUI relaunch (its watchdog
#                               SIGKILLs+relaunches on a hang), so touching it
#                               here would double-fire.
#   3. bridge RSS pressure   -> respawn Window 1 when idle only.
#
# The task-active lock (bridge.py holds it around each run) is the single
# arbiter: an absent TUI during a task is the bridge's problem, not ours.
set -eu

SESSION="${AGENT_NAME:-omp-agent}"
BRIDGE_PROC="bridge.py"                          # Window 1 — the persistent driver
TUI_PROC="omp .*--hook"                           # Window 0 — the native TUI
LAUNCHER="${OMP_LAUNCHER:-/usr/local/bin/launch-omp.sh}"
DEFAULT_CWD="${OMP_DEFAULT_CWD:-/workspace}"
TASK_LOCK_FILE="${OMP_TASK_LOCK_FILE:-/home/agent/.task-active.lock}"
IDLE_CHECK_INTERVAL="${RECYCLER_IDLE_INTERVAL:-30}"
RSS_LIMIT_MB="${RECYCLER_RSS_LIMIT_MB:-1500}"    # respawn bridge.py above this RSS
RECYCLER_ENABLED="${AGENT_RECYCLER_ENABLED:-true}"

bridge_alive() { pgrep -f "$BRIDGE_PROC" >/dev/null 2>&1; }
tui_alive()    { pgrep -f "$TUI_PROC"    >/dev/null 2>&1; }
task_active()  { [ -f "$TASK_LOCK_FILE" ]; }

bridge_rss_mb() {
    local kb
    kb=$(pgrep -f "$BRIDGE_PROC" | xargs -r ps -o rss= -p 2>/dev/null \
         | awk '{s+=$1} END {print s+0}')
    echo $(( kb / 1024 ))
}

respawn_bridge() {
    echo "[omp-recycler] respawning Window-1 bridge.py (${SESSION}:1)"
    tmux respawn-window -k -t "${SESSION}:1" "exec python3 /opt/omp-bridge/bridge.py --serve" \
        2>/dev/null || true
}

relaunch_tui() {
    echo "[omp-recycler] relaunching idle Window-0 TUI (${SESSION}:0)"
    tmux respawn-window -k -t "${SESSION}:0" "exec ${LAUNCHER} ${DEFAULT_CWD}" \
        2>/dev/null || true
}

while true; do
    if [ "$RECYCLER_ENABLED" = "true" ]; then
        if ! bridge_alive; then
            # The driver is gone (not a between-tasks gap) -> always respawn.
            respawn_bridge
        elif ! task_active; then
            # Idle: safe to touch Window 0. During a task the bridge owns it.
            if ! tui_alive; then
                relaunch_tui
            else
                rss=$(bridge_rss_mb)
                if [ "$rss" -gt "$RSS_LIMIT_MB" ]; then
                    echo "[omp-recycler] bridge.py RSS ${rss}MB > ${RSS_LIMIT_MB}MB and idle -> respawn"
                    respawn_bridge
                fi
            fi
        fi
    fi
    sleep "$IDLE_CHECK_INTERVAL"
done
