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
