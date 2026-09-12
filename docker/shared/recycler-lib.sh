# recycler-lib.sh — shared helper functions for docker/{mc-agent-base,mc-claude-agent}/recycler.sh
#
# Why this exists: the two sibling recycler.sh scripts diverged in May 2026
# (Bug-3-fix landed in only one). The drift-class is real — keep process-inspection
# primitives in ONE place, sourced by both. The two recyclers still own their
# own task-lock / decision logic (which legitimately differs by image), but every
# /proc-poke goes through the helpers below.
#
# Distribution-agnostic: uses /proc/{PID}/{status,stat} which is identical on
# Alpine BusyBox and Debian GNU/procps. Avoids `ps -p $PID` which BusyBox rejects.
#
# POSIX-compliant (works under /bin/sh + bash). No bashisms.

# proc_rss_mb <pid> — print VmRSS as integer MB, or empty if unreadable.
# Returns 0 on success (even if empty), 1 only if pid is missing.
# Empty output ≠ "process is broken"; it usually means kernel thread (no VmRSS line)
# or proc-entry vanished mid-call. Caller decides what empty means.
proc_rss_mb() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    local rss_kb
    rss_kb=$(awk '/^VmRSS:/ { print $2; exit }' "/proc/$pid/status" 2>/dev/null || true)
    [ -z "$rss_kb" ] && return 0
    echo $(( rss_kb / 1024 ))
}

# proc_state <pid> — print one-char process state from /proc/$pid/stat.
# Linux state codes:
#   R running, S sleeping (interruptible), D uninterruptible-sleep,
#   Z zombie, T stopped/traced, X dead, I idle (kernel thread, Linux ≥4)
# Output is exactly one character. Returns "?" if unreadable (process gone, no permission, etc.).
#
# Why parse stat (not status): the State: line in /proc/$pid/status is "R (running)"
# but /proc/$pid/stat field 3 is the single char. The stat layout has the comm-field
# in parens (which can contain spaces) — we strip everything up to the last `)` first.
proc_state() {
    local pid="$1"
    [ -n "$pid" ] || { echo "?"; return 1; }
    local stat
    stat=$(cat "/proc/$pid/stat" 2>/dev/null || true)
    [ -z "$stat" ] && { echo "?"; return 1; }
    # Strip up to and including the last ")" — handles comm-fields with spaces/parens.
    local after
    after=${stat##*\) }
    # Field 1 of $after is now the state char.
    echo "${after%% *}"
}

# proc_alive <pid> — true (exit 0) if process exists AND is NOT zombie/dead, else false (exit 1).
# Use this in place of `kill -0 $PID` when you want to also exclude zombies.
proc_alive() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    [ -d "/proc/$pid" ] || return 1
    local s
    s=$(proc_state "$pid")
    # NOTE: bare `?` in a case pattern is a glob (any single char), which
    # would match every state. Use the bracket-form `[?]` for the literal
    # question-mark we emit when /proc is unreadable.
    case "$s" in
        Z|X|[?]) return 1 ;;
        *)       return 0 ;;
    esac
}

# ── G4 (Paritaets-Audit #521): silent-child liveness ────────────────────────
# Ein langer pytest-/build-Lauf rendert nichts im Pane; der Marker-mtime-
# Sensor sieht "idle", obwohl der Agent arbeitet. CPU-Jiffies des
# Prozessbaums sind der Sensor, der auch stumme Arbeit sieht.

# proc_cpu_jiffies <pid> — print utime+stime (stat fields 14+15) or empty if
# unreadable. After stripping up to the last ")", field 14 is position 12 of
# the remainder (fields 3..13 dropped).
proc_cpu_jiffies() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    local stat after
    stat=$(cat "/proc/$pid/stat" 2>/dev/null || true)
    [ -z "$stat" ] && return 0
    after=${stat##*\) }
    # shellcheck disable=SC2086 — word splitting IS the field parser here.
    set -- $after
    echo $(( ${12:-0} + ${13:-0} ))
}

# subtree_pids <pid> — the pid itself plus all descendants (BFS over
# /proc/*/stat ppid). Empty output on unreadable entries is fine — a vanished
# child between listing and reading just contributes 0.
subtree_pids() {
    local root="$1"
    [ -n "$root" ] || return 0
    local frontier="$root"
    printf '%s\n' "$root"
    while [ -n "$frontier" ]; do
        local next="" p pp
        for p in $frontier; do
            for dir in /proc/[0-9]*; do
                [ -r "$dir/stat" ] || continue
                pp=$(awk '{print $4}' "$dir/stat" 2>/dev/null || true)
                [ "$pp" = "$p" ] || continue
                local c
                c=${dir#/proc/}
                case " $next " in *" $c "*) continue ;; esac
                case " $(printf '%s\n' $frontier) " in *" $c "*) continue ;; esac
                next="$next $c"
                printf '%s\n' "$c"
            done
        done
        frontier="$next"
    done
}

# subtree_cpu_jiffies <pid> — summed utime+stime over the whole subtree.
subtree_cpu_jiffies() {
    local root="$1" total=0 p j
    for p in $(subtree_pids "$root" 2>/dev/null); do
        j=$(proc_cpu_jiffies "$p")
        [ -n "$j" ] && total=$(( total + j ))
    done
    echo "$total"
}

# subtree_busy <pid> — true if the subtree consumed CPU since the LAST call
# for this root (delta over the recycler's own tick interval, not cumulative
# load: a long-since-finished compute must not block recycling forever).
# One state file per root pid. First call primes the baseline -> false.
RECYCLER_BUSY_MIN_JIFFIES="${RECYCLER_BUSY_MIN_JIFFIES:-10}"
_subtree_busy_state() {
    printf '%s/subtree-busy.%s' "${TMPDIR:-/tmp}" "$1"
}

subtree_busy() {
    local pid="$1" state prev now
    [ -n "$pid" ] || return 1
    state=$(_subtree_busy_state "$pid")
    now=$(subtree_cpu_jiffies "$pid")
    prev=$(cat "$state" 2>/dev/null || true)
    printf '%s\n' "$now" > "$state" 2>/dev/null || true
    case "$prev" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ $(( now - prev )) -ge "$RECYCLER_BUSY_MIN_JIFFIES" ]
}
