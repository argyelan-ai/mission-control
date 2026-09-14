# sigforward.sh — graceful shutdown for container PID-1 entrypoints.
#
# Sourced by ALL FOUR container entrypoints (mc-agent-base, mc-claude-agent,
# mc-kimi-agent, omp-bridge) right after SESSION is set. POSIX-sh compatible
# (mc-agent-base runs busybox ash; the rest run bash).
#
# Problem (measured, 2026-09-14): Linux does NOT deliver SIGTERM to PID 1
# unless a handler is installed (/proc/1/status SigCgt lacked bit 0x4000), so
# every `docker stop` rode out the stop_grace_period (PR #573: 20s) and ended
# in SIGKILL on the whole tree — Exit 137, no mc finish, no transcript flush,
# no lock release (1217/1343 ms at StopTimeout=1).
#
# What this installs:
#   trap TERM/INT → mc_sigforward: forward TERM to every tmux pane process
#   GROUP (tmux sets each pane's pgid == pane_pid, so one kill reaches the
#   restart-loop shell AND its running child: poll.sh, claude/omp/kimi,
#   recycler), wait a BOUNDED grace for the panes to die, then kill-server
#   and exit 143/130. Without the bound a stuck child would turn a hard
#   stop into a hung one — worse than today.
#
#   mc_sleep_wait: the interruptible sleep every PID-1 watchdog loop MUST
#   use. A foreground external `sleep 30` DEFERS the trap until it completes
#   (POSIX: traps run after the current command) — up to 30s, longer than
#   the 20s stop_grace_period, i.e. SIGKILL wins. `wait` IS interrupted by
#   a trapped signal, so the handler runs immediately.
#
# Knob: MC_STOP_GRACE (seconds, default 10) — must stay below the compose
# stop_grace_period (20s, PR #573) so the trap, not SIGKILL, wins the race.

MC_STOP_GRACE="${MC_STOP_GRACE:-10}"

mc_sigforward() {
    _sig="$1"
    _code="$2"
    echo "[entrypoint] SIG$_sig — forwarding to tmux windows (grace ${MC_STOP_GRACE}s)..."
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        _deadline=$(( $(date +%s) + MC_STOP_GRACE ))
        while [ "$(date +%s)" -lt "$_deadline" ]; do
            _alive=""
            # Re-list panes every round: the recycler can respawn windows
            # mid-shutdown, and a pane created after the first TERM wave
            # would otherwise only die at kill-server (hard HUP).
            for _pid in $(tmux list-panes -s -t "$SESSION" -F '#{pane_pid}' 2>/dev/null); do
                if kill -0 "$_pid" 2>/dev/null; then
                    _alive=1
                    # Direct hit (works everywhere) + process-group hit
                    # (pane pgid == pane_pid: reaches the running child
                    # inside the restart-loop shell). Either failing is
                    # fine — the process may have just exited.
                    kill -TERM "$_pid" 2>/dev/null || true
                    kill -TERM "-$_pid" 2>/dev/null || true
                fi
            done
            if [ -z "$_alive" ]; then
                break
            fi
            sleep 0.25
        done
    fi
    tmux kill-server 2>/dev/null || true
    echo "[entrypoint] shutdown complete (exit $_code)"
    exit "$_code"
}

mc_sleep_wait() {
    sleep "$1" &
    # Interrupted by a trapped signal (handler exits from inside the trap,
    # so the rc below is never observed in that path).
    wait "$!" 2>/dev/null || true
}

trap 'mc_sigforward TERM 143' TERM
trap 'mc_sigforward INT 130' INT
