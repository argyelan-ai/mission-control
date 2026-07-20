#!/usr/bin/env bash
# test_turn_state.sh — smoke-tests for detect_turn_state (lib/turn-state.sh).
#
# Live pilot finding 2026-07-20: claude-cli 2.1.x renders the idle prompt
# as `❯` + NO-BREAK SPACE (U+00A0). The `^❯ *$` idle check only knew plain
# spaces, so idle panes classified as "working" forever (the post-turn
# "Cogitated for Ns" line matches the working markers) and the comm_v2
# message gate never opened. turn-state.sh now normalizes NBSP before
# matching. Invoked via tests/test_turn_state.py (pytest wrapper).

set -euo pipefail

LIB="${1:-$(dirname "$0")/../../docker/mc-agent-base/lib/turn-state.sh}"
[ -f "$LIB" ] || { echo "FAIL: lib not found at $LIB" >&2; exit 2; }

fail() { echo "FAIL: $1" >&2; exit 1; }

TMUX_STUB_DIR=$(mktemp -d)
trap 'rm -rf "$TMUX_STUB_DIR"' EXIT
cat > "$TMUX_STUB_DIR/tmux" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = "capture-pane" ]; then
    if [ -n "${TMUX_STUB_PANE_FILE:-}" ] && [ -f "$TMUX_STUB_PANE_FILE" ]; then
        cat "$TMUX_STUB_PANE_FILE"
    fi
    exit 0
fi
exit 0
STUB
chmod +x "$TMUX_STUB_DIR/tmux"
export PATH="$TMUX_STUB_DIR:$PATH"

# shellcheck source=/dev/null
source "$LIB"

# ── Case 1: claude 2.1.x idle — ❯ + NBSP prompt, stale "Cogitated" above ──
pane_nbsp=$(mktemp)
printf 'some earlier output\n\xe2\x9c\xbb Cogitated for 21s\n\n\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\n\xe2\x9d\xaf\xc2\xa0\n\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\n  bypass permissions on\n' > "$pane_nbsp"
export TMUX_STUB_PANE_FILE="$pane_nbsp"
out=$(detect_turn_state testsession)
[ "$out" = "idle" ] || fail "case1: NBSP-prompt + stale Cogitated must be idle, got '$out'"

# ── Case 2: plain-space prompt stays idle (regression guard) ──────────────
pane_plain=$(mktemp)
printf 'output\n────\n❯ \n────\n  bypass permissions on\n' > "$pane_plain"
export TMUX_STUB_PANE_FILE="$pane_plain"
out=$(detect_turn_state testsession)
[ "$out" = "idle" ] || fail "case2: plain-space prompt must be idle, got '$out'"

# ── Case 3: genuinely working (esc to interrupt, no bare prompt) ──────────
pane_work=$(mktemp)
printf 'output\n✻ Brewing… (10s · esc to interrupt)\n' > "$pane_work"
export TMUX_STUB_PANE_FILE="$pane_work"
out=$(detect_turn_state testsession)
[ "$out" = "working" ] || fail "case3: esc-to-interrupt pane must be working, got '$out'"

# ── Case 4: crashed marker wins ───────────────────────────────────────────
pane_crash=$(mktemp)
printf 'API Error: fetch failed\n❯ \n' > "$pane_crash"
export TMUX_STUB_PANE_FILE="$pane_crash"
out=$(detect_turn_state testsession)
[ "$out" = "crashed" ] || fail "case4: API error must be crashed, got '$out'"

# ── Case 5 (live pilot 2026-07-20, 2. Iteration): Ghost-Text idle ─────────
# claude-cli 2.1.x fills the idle input box with prompt suggestions /
# pending-wakeup text ("❯ Antwort abwarten…") — never a bare prompt. With
# the statusline visible and NO "esc to interrupt", the turn is over: idle.
# The stale "✻ Cogitated for 15s" summary above must NOT count as working.
pane_ghost=$(mktemp)
printf 'prose from last turn\n\xe2\x9c\xbb Cogitated for 15s\n\n\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\n\xe2\x9d\xaf Antwort abwarten, dann quoten\n\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\n  \xe2\x8f\xb5\xe2\x8f\xb5 bypass permissions on (shift+tab to cycle)\n' > "$pane_ghost"
export TMUX_STUB_PANE_FILE="$pane_ghost"
out=$(detect_turn_state testsession)
[ "$out" = "idle" ] || fail "case5: ghost-text prompt + statusline w/o esc-to-interrupt must be idle, got '$out'"

# ── Case 6: live spinner with ellipsis (no esc-to-interrupt visible) ──────
pane_spin=$(mktemp)
printf 'output\n\xe2\x9c\xbb Waddling\xe2\x80\xa6 (31s \xc2\xb7 \xe2\x86\x93 1.6k tokens)\n' > "$pane_spin"
export TMUX_STUB_PANE_FILE="$pane_spin"
out=$(detect_turn_state testsession)
[ "$out" = "working" ] || fail "case6: live spinner with ellipsis must be working, got '$out'"

echo "PASS: all 6 detect_turn_state cases"
