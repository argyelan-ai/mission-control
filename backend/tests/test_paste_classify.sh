#!/usr/bin/env bash
# test_paste_classify.sh — smoke-tests for the interrupted-nudge fix.
#
# Two behaviours introduced 2026-09-12 (live fleet: four manual Enter presses
# in one day after Esc interrupts):
#
#   1. pane_in_interrupted_dialog  — paste_and_submit must treat the claude
#      post-interrupt dialog (`Interrupted` / `What should Claude do
#      instead?`) as NOT a clean prompt, so the nudge paste waits until the
#      dialog is gone. Old wait_for_clean_prompt only ran detect_pane_ui,
#      which matches inside the dialog (box glyphs / `❯` are visible) and
#      released the paste too early — the Enter landed in the dialog, the
#      text stayed unsubmitted in the input box.
#
#   2. classify_paste_outcome — three-way verification distinguishing
#        "0" submitted (fingerprint in scrollback outside the input tail)
#        "2" sitting in the input box, unsubmitted (fingerprint ONLY in tail)
#        "1" never arrived (fingerprint nowhere)
#      The old binary verify_paste_landed reported case 2 as "fingerprint not
#      visible", sending readers to the wrong place.
#
# Sources docker/mc-agent-base/lib/paste-verify.sh and the shared poll.sh
# functions (POLL_SH_SOURCE_ONLY=1), stubs `tmux` via a PATH shim. Invoked
# via tests/test_paste_classify.py (pytest wrapper) so CI runs it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LIB="$REPO_ROOT/docker/mc-agent-base/lib/paste-verify.sh"
POLL="$REPO_ROOT/docker/shared/poll.sh"
TMUX_STUB_DIR=$(mktemp -d)
trap 'rm -rf "$TMUX_STUB_DIR"' EXIT

cat > "$TMUX_STUB_DIR/tmux" <<'STUB'
#!/usr/bin/env bash
# Stub tmux: hands back $TMUX_STUB_PANE_FILE for capture-pane (honoring the
# -S -N history window like real tmux), records send-keys in $TMUX_KEYS_LOG.
if [ "${1:-}" = "capture-pane" ]; then
    # Honor the -S -N history window like real tmux: emit only the last N
    # lines. Without this, a scrolled-out dialog at the top of the fixture
    # would still be "visible" and case P2 could not test tail-window logic.
    stub_win=""
    stub_prev=""
    for stub_a in "$@"; do
        if [ "$stub_prev" = "-S" ]; then stub_win="${stub_a#-}"; fi
        stub_prev="$stub_a"
    done
    if [ -n "${TMUX_STUB_PANE_FILE:-}" ] && [ -f "$TMUX_STUB_PANE_FILE" ]; then
        if [ -n "$stub_win" ]; then
            tail -n "$stub_win" "$TMUX_STUB_PANE_FILE"
        else
            cat "$TMUX_STUB_PANE_FILE"
        fi
    fi
    exit 0
fi
if [ "${1:-}" = "send-keys" ]; then
    [ -n "${TMUX_KEYS_LOG:-}" ] && echo "send-keys $*" >> "$TMUX_KEYS_LOG"
    exit 0
fi
exit 0
STUB
chmod +x "$TMUX_STUB_DIR/tmux"
export PATH="$TMUX_STUB_DIR:$PATH"

export SESSION_NAME="testsession"
export PASTE_FINGERPRINT_LEN=40

# ── classify_paste_outcome ──────────────────────────────────────────────────
# shellcheck source=/dev/null
source "$LIB"

# Case C1: fingerprint only inside the input tail → "2" (sitting in the box)
pane_c1=$(mktemp)
printf 'earlier scrollback line\n' > "$pane_c1"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do echo "filler $i" >> "$pane_c1"; done
echo "Fix the watchdog retry logic in poll.sh and rerun the failing" >> "$pane_c1"
export TMUX_STUB_PANE_FILE="$pane_c1"
msg_c1=$(mktemp)
printf 'Fix the watchdog retry logic in poll.sh and rerun the failing tests now\n' > "$msg_c1"
out=$(classify_paste_outcome "$msg_c1")
[ "$out" = "2" ] || fail "case C1: fingerprint only in input tail must classify 2, got '$out'"

# Case C2: fingerprint in the scrollback above the tail → "0" (submitted)
pane_c2=$(mktemp)
printf 'Fix the watchdog retry logic in poll.sh and rerun the failing\n' > "$pane_c2"
echo "✻ Cogitated for 3s" >> "$pane_c2"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do echo "other $i" >> "$pane_c2"; done
export TMUX_STUB_PANE_FILE="$pane_c2"
out=$(classify_paste_outcome "$msg_c1")
[ "$out" = "0" ] || fail "case C2: fingerprint in scrollback must classify 0, got '$out'"

# Case C3: fingerprint nowhere → "1" (never arrived)
pane_c3=$(mktemp)
printf 'totally unrelated pane content\n' > "$pane_c3"
for i in 1 2 3; do echo "filler $i" >> "$pane_c3"; done
export TMUX_STUB_PANE_FILE="$pane_c3"
out=$(classify_paste_outcome "$msg_c1")
[ "$out" = "1" ] || fail "case C3: missing fingerprint must classify 1, got '$out'"

# Case C4: collapse-marker growth → "0" even without plain fingerprint
pane_c4=$(mktemp)
for i in 1 2 3; do echo "history $i" >> "$pane_c4"; done
echo "[Pasted text #1 +5 lines]" >> "$pane_c4"
for i in 1 2 3 4 5 6 7 8 9; do echo "box filler $i" >> "$pane_c4"; done
export TMUX_STUB_PANE_FILE="$pane_c4"
PASTE_PRE_COLLAPSE_COUNT=0
msg_c4=$(mktemp)
printf 'Uniquely different first line entirely unrelated to pane\n' > "$msg_c4"
out=$(classify_paste_outcome "$msg_c4")
[ "$out" = "0" ] || fail "case C4: collapse-marker growth must classify 0, got '$out'"

# ── pane_in_interrupted_dialog (sourced from poll.sh, functions only) ──────
# poll.sh sources $POLL_LIB_DIR/{turn-state,ui-detect,context-detect}.sh even in
# SOURCE_ONLY mode. Point POLL_LIB_DIR at the REAL mc-agent-base lib (so
# paste_and_submit finds classify_paste_outcome) with turn-state/ui-detect/
# context-detect stubbed — this test only exercises pane/tmux heuristics.
POLL_LIB_DIR="$TMUX_STUB_DIR/lib"
mkdir -p "$POLL_LIB_DIR"
cp "$REPO_ROOT/docker/mc-agent-base/lib/paste-verify.sh" "$POLL_LIB_DIR/paste-verify.sh"
for _lib in turn-state ui-detect context-detect; do
    : > "$POLL_LIB_DIR/$_lib.sh"
done
POLL_SH_SOURCE_ONLY=1 source "$POLL"

# Case P1: post-interrupt dialog in the tail → 0 (true): NOT a clean prompt
pane_p1=$(mktemp)
printf '✻ Cogitated for 12s\nInterrupted · What should Claude do instead?\n❯ \n' > "$pane_p1"
export TMUX_STUB_PANE_FILE="$pane_p1"
if ! pane_in_interrupted_dialog "${SESSION_NAME}:0"; then
    fail "case P1: Interrupted dialog in tail must be detected"
fi

# Case P2: old dialog scrolled OUT of the tail → 1 (false): paste may proceed
pane_p2=$(mktemp)
printf 'Interrupted · What should Claude do instead?\n✻ Turn done\n' > "$pane_p2"
for i in $(seq 1 20); do echo "later line $i" >> "$pane_p2"; done
echo "❯ " >> "$pane_p2"
export TMUX_STUB_PANE_FILE="$pane_p2"
if pane_in_interrupted_dialog "${SESSION_NAME}:0"; then
    fail "case P2: dialog scrolled out of tail must NOT block the paste"
fi

# Case P3: normal idle pane → 1 (false)
pane_p3=$(mktemp)
printf '────\n❯ \n────\n  bypass permissions on\n' > "$pane_p3"
export TMUX_STUB_PANE_FILE="$pane_p3"
if pane_in_interrupted_dialog "${SESSION_NAME}:0"; then
    fail "case P3: normal idle pane must not be flagged as interrupted dialog"
fi

echo "PASS: all paste-classify smoke tests"
