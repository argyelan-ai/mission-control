#!/usr/bin/env bash
# docker/omp-bridge/omp-recycler.sh — Window 2 recycler (ADR-045).
#
# FORK of docker/mc-agent-base/recycler.sh. The upstream recycler pgreps a
# PERSISTENT Window-0 process (openclaude) and respawns it when dead; under
# omp's one-shot model there is legitimately no persistent omp process, so a
# byte-identical recycler would read every between-tasks gap as a crash.
#
# The TWO changes vs upstream:
#   1. PROCESS_NAME tracks the long-lived DRIVER — bridge.py — NEVER the
#      short-lived `omp` subprocess.
#   2. Liveness / idle gate = "is bridge.py alive?" AND ".task-active.lock
#      absent". An absent omp subprocess is NEVER a crash; an active omp run
#      (lock held) is NEVER idle-killed.
set -eu

SESSION="${AGENT_NAME:-omp-agent}"
PROCESS_NAME="bridge.py"                         # the persistent supervisor, not omp
TASK_LOCK_FILE="${OMP_TASK_LOCK_FILE:-/home/agent/.task-active.lock}"
IDLE_CHECK_INTERVAL="${RECYCLER_IDLE_INTERVAL:-30}"
RSS_LIMIT_MB="${RECYCLER_RSS_LIMIT_MB:-1500}"    # respawn bridge.py above this RSS
RECYCLER_ENABLED="${AGENT_RECYCLER_ENABLED:-true}"

proc_alive() { pgrep -f "$PROCESS_NAME" >/dev/null 2>&1; }
task_active() { [ -f "$TASK_LOCK_FILE" ]; }

bridge_rss_mb() {
    # Sum RSS (KB) of all bridge.py pids -> MB. 0 when none.
    local kb
    kb=$(pgrep -f "$PROCESS_NAME" | xargs -r ps -o rss= -p 2>/dev/null \
         | awk '{s+=$1} END {print s+0}')
    echo $(( kb / 1024 ))
}

respawn_driver() {
    # Kill + relaunch Window 0 (the bridge). -k kills the existing pane process
    # first; the pane command re-execs bridge.py --serve which re-prints
    # OMP_BRIDGE_READY once its poll loop is back up.
    echo "[omp-recycler] respawning Window-0 bridge.py (${SESSION}:0)"
    tmux respawn-pane -k -t "${SESSION}:0" "exec python3 /opt/omp-bridge/bridge.py --serve" \
        2>/dev/null || tmux respawn-window -k -t "${SESSION}:0" 2>/dev/null || true
}

while true; do
    if [ "$RECYCLER_ENABLED" = "true" ]; then
        if ! proc_alive; then
            # The SUPERVISOR is gone (not a between-tasks omp gap) -> real respawn.
            respawn_driver
        elif ! task_active; then
            # Idle (no active omp run): only recycle on RSS pressure. Never
            # idle-kill while the lock is held — that would SIGKILL a live omp.
            rss=$(bridge_rss_mb)
            if [ "$rss" -gt "$RSS_LIMIT_MB" ]; then
                echo "[omp-recycler] bridge.py RSS ${rss}MB > ${RSS_LIMIT_MB}MB and idle -> respawn"
                respawn_driver
            fi
        fi
    fi
    sleep "$IDLE_CHECK_INTERVAL"
done
