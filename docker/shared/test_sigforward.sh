#!/usr/bin/env bash
# Behavior + sabotage tests for docker/shared/sigforward.sh (PID-1 trap).
#
# The card's acceptance criteria are behavioral, so each test drives a REAL
# trap installation in a real shell (the PID-1 stand-in) with a real tmux
# session, then sends the signal — no mocks for the machinery under test.
# Needs only bash + tmux (no Docker daemon), same pattern as
# docker/cdp-browser/test_healthcheck.sh.
#
# Sabotage probes (card DoD: "je Test eine eigene Sabotage-Probe, je eine
# Stelle entwaffnen") — each test is run twice: once green against the real
# sigforward.sh, once EXPECTED-TO-FAIL with exactly one spot disarmed:
#
#   forward : mc_sigforward overridden to exit without forwarding
#             → pane children survive a TERM'd entrypoint (the original bug)
#   bound   : MC_STOP_GRACE overridden to 30s → the bounded wait stops
#             bounding; an immortal pane turns a hard stop into a hung one
#   sleep   : mc_sleep_wait overridden with bare `sleep` → the trap is
#             deferred until the foreground sleep completes (30s > 20s grace)
#   trap    : the `trap` registration lines stripped from the file →
#             /proc/<pid>/status SigCgt loses bit 0x4000 (SIGTERM) — the
#             measured kernel behavior that started this card
#
# exit code 143 = 128+SIGTERM, 130 = 128+SIGINT.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SIGFORWARD="${SIGFORWARD_BIN:-$HERE/sigforward.sh}"

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL + 1)); }

SESSION=""
DRIVER=""
PANEPID=""

cleanup() {
    [ -n "${DRIVER:-}" ] && kill "$DRIVER" 2>/dev/null
    tmux kill-server 2>/dev/null
}
trap cleanup EXIT

# wait_exit PID DEADLINE_S — poll for DRIVER to exit; sets RC and ELAPSED.
# Returns 255 when the deadline hits (process refused to exit).
wait_exit() {
    local pid="$1" deadline_s="$2" start end
    start=$(date +%s)
    end=$((start + deadline_s))
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$(date +%s)" -ge "$end" ]; then
            RC=255
            ELAPSED=$(( $(date +%s) - start ))
            return 255
        fi
        sleep 0.1
    done
    wait "$pid"
    RC=$?
    ELAPSED=$(( $(date +%s) - start ))
    return 0
}

# start_driver SIGFORWARD_PATH PANE_CMD EXTRA_DRIVER_CODE
#   PANE_CMD runs as the tmux pane's restart-loop shell (pane pgid == pane_pid
#   like in the real fleet). EXTRA_DRIVER_CODE runs in the entrypoint stand-in
#   AFTER sourcing sigforward.sh — that is where a sabotage override goes.
start_driver() {
    local sig="$1" pane_cmd="$2" extra="$3"
    tmux kill-server 2>/dev/null
    SESSION="tsftest$$"
    tmux new-session -d -s "$SESSION" "$pane_cmd"
    PANEPID="$(tmux list-panes -s -t "$SESSION" -F '#{pane_pid}' | head -1)"
    if [ -z "$PANEPID" ]; then
        fail "start_driver: no pane pid for session $SESSION"
        return 1
    fi
    # The entrypoint stand-in: PID-1 watchdog shape (trap + mc_sleep_wait loop).
    (
        SESSION="$SESSION"
        # shellcheck disable=SC1090
        . "$sig"
        eval "$extra"
        while :; do mc_sleep_wait 30; done
    ) &
    DRIVER=$!
    sleep 0.5 # let the traps install
}

pane_alive() { kill -0 "$PANEPID" 2>/dev/null; }

# ── Test 1: TERM reaches the tmux windows, entrypoint exits 143 ──────────────
test_term_forwards_to_pane_and_exits_143() {
    start_driver "$SIGFORWARD" 'while true; do sleep 30; done' ""
    kill -TERM "$DRIVER"
    if ! wait_exit "$DRIVER" 6; then
        fail "term-forward: entrypoint did not exit within 6s"
        return 1
    fi
    [ "$RC" -eq 143 ] || { fail "term-forward: expected exit 143, got $RC"; return 1; }
    if pane_alive; then
        fail "term-forward: pane process survived the forwarded TERM"
        return 1
    fi
    pass "term-forward: pane killed, entrypoint exit 143 (${ELAPSED}s)"
}

# Sabotage forward: handler exits but forwards nothing → pane must survive.
sabotage_term_forward() {
    start_driver "$SIGFORWARD" 'while true; do sleep 30; done' \
        'mc_sigforward() { exit 143; }'
    kill -TERM "$DRIVER"
    wait_exit "$DRIVER" 6
    if [ "$RC" -eq 143 ] && ! pane_alive; then
        fail "sabotage-forward: probe did NOT fail — test is vacuous"
        return 1
    fi
    pass "sabotage-forward: disarmed forwarding makes the test fail (rc=$RC, pane_alive=$(pane_alive && echo yes || echo no))"
}

# ── Test 2: the wait is BOUNDED — an immortal pane cannot hang the stop ─────
test_wait_is_bounded_with_stuck_pane() {
    # Pane shell ignores TERM (simulates a wedged child): the deadline, not
    # pane death, must end the wait.
    start_driver "$SIGFORWARD" "trap '' TERM; while true; do sleep 30; done" \
        'MC_STOP_GRACE=2'
    kill -TERM "$DRIVER"
    if ! wait_exit "$DRIVER" 6; then
        fail "bounded: entrypoint still running after 6s (MC_STOP_GRACE=2 + margin)"
        return 1
    fi
    [ "$RC" -eq 143 ] || { fail "bounded: expected exit 143, got $RC"; return 1; }
    [ "$ELAPSED" -le 5 ] || { fail "bounded: took ${ELAPSED}s, expected <= 5s"; return 1; }
    pass "bounded: stuck pane, exit 143 after ${ELAPSED}s (< grace deadline)"
}

# Sabotage bound: MC_STOP_GRACE=30 with a 6s deadline → no exit within bound.
sabotage_wait_bounded() {
    start_driver "$SIGFORWARD" "trap '' TERM; while true; do sleep 30; done" \
        'MC_STOP_GRACE=30'
    kill -TERM "$DRIVER"
    if wait_exit "$DRIVER" 6 && [ "$RC" -eq 143 ]; then
        fail "sabotage-bound: probe exited within 6s — test is vacuous"
        return 1
    fi
    pass "sabotage-bound: unbounded grace (30s) makes the test fail (rc=$RC after deadline)"
}

# ── Test 3: watchdog sleep is interruptible — trap fires DURING the sleep ────
test_watchdog_sleep_interruptible() {
    # 30s watchdog sleep, like the real entrypoints. The handler must run
    # immediately, not after the sleep completes (30s > 20s compose grace).
    start_driver "$SIGFORWARD" 'while true; do sleep 30; done' ""
    kill -TERM "$DRIVER"
    if ! wait_exit "$DRIVER" 5; then
        fail "sleep-wait: trap deferred by foreground sleep, no exit within 5s"
        return 1
    fi
    [ "$RC" -eq 143 ] || { fail "sleep-wait: expected exit 143, got $RC"; return 1; }
    pass "sleep-wait: trap interrupted the 30s sleep after ${ELAPSED}s"
}

# Sabotage sleep: bare `sleep` (the pre-fix watchdog shape) defers the trap.
sabotage_watchdog_sleep() {
    start_driver "$SIGFORWARD" 'while true; do sleep 30; done' \
        'mc_sleep_wait() { sleep "$1"; }'
    kill -TERM "$DRIVER"
    if wait_exit "$DRIVER" 5 && [ "$RC" -eq 143 ]; then
        fail "sabotage-sleep: probe exited within 5s despite bare sleep — test is vacuous"
        return 1
    fi
    pass "sabotage-sleep: bare sleep defers the trap, test fails as designed (rc=$RC)"
}

# ── Test 4: SigCgt carries the SIGTERM bit 0x4000 (the card's kernel metric) ─
test_sigcgt_has_term_bit() {
    start_driver "$SIGFORWARD" 'while true; do sleep 30; done' ""
    local sigcgt
    sigcgt="$(awk '/^SigCgt:/ {print $2}' "/proc/$DRIVER/status")"
    local term_bit int_bit
    term_bit=$(( ( 0x$sigcgt & 0x4000 ) != 0 )) # SIGTERM, bit 15
    int_bit=$(( ( 0x$sigcgt & 0x2 ) != 0 ))     # SIGINT, bit 1
    if [ "$term_bit" -ne 1 ] || [ "$int_bit" -ne 1 ]; then
        fail "sigcgt: SigCgt=0x$sigcgt — TERM bit 0x4000=$term_bit, INT bit 0x2=$int_bit"
        return 1
    fi
    pass "sigcgt: SigCgt=0x$sigcgt — kernel now DELIVERS TERM/INT (0x4000+0x2 set)"
}

# Sabotage trap: registration lines stripped → SigCgt loses both bits.
sabotage_sigcgt() {
    local stripped
    stripped="$(mktemp)"
    grep -v "^trap " "$SIGFORWARD" > "$stripped"
    start_driver "$stripped" 'while true; do sleep 30; done' ""
    local sigcgt term_bit
    sigcgt="$(awk '/^SigCgt:/ {print $2}' "/proc/$DRIVER/status")"
    term_bit=$(( ( 0x$sigcgt & 0x4000 ) != 0 ))
    rm -f "$stripped"
    if [ "$term_bit" -eq 1 ]; then
        fail "sabotage-trap: probe still shows bit 0x4000 — test is vacuous"
        return 1
    fi
    pass "sabotage-trap: without trap lines SigCgt=0x$sigcgt lacks 0x4000 (the original defect), test fails as designed"
}

test_term_forwards_to_pane_and_exits_143  && sabotage_term_forward
test_wait_is_bounded_with_stuck_pane      && sabotage_wait_bounded
test_watchdog_sleep_interruptible         && sabotage_watchdog_sleep
test_sigcgt_has_term_bit                  && sabotage_sigcgt

echo
echo "sigforward tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
