#!/usr/bin/env bash
# test_paste_verify.sh — smoke-tests for the post-paste verification heuristic.
#
# Sources docker/mc-agent-base/lib/paste-verify.sh and stubs `tmux` to
# control what `tmux capture-pane` returns. Asserts that verify_paste_landed
# returns 0 when the first-line fingerprint of FILE shows up in the stubbed
# pane and 1 when it does not. Invoked via tests/test_paste_verify.py
# (pytest wrapper) so the whole thing shows up in the normal suite.

set -euo pipefail

LIB="${1:-$(dirname "$0")/../../docker/mc-agent-base/lib/paste-verify.sh}"

if [ ! -f "$LIB" ]; then
    echo "FAIL: lib not found at $LIB" >&2
    exit 2
fi

fail() { echo "FAIL: $1" >&2; exit 1; }

# Stub tmux with a small dispatcher. Looks at $TMUX_STUB_PANE_FILE (a file
# whose contents we hand back when `tmux capture-pane` is called).
TMUX_STUB_DIR=$(mktemp -d)
trap 'rm -rf "$TMUX_STUB_DIR"' EXIT

cat > "$TMUX_STUB_DIR/tmux" <<'STUB'
#!/usr/bin/env bash
# Args: subcommand and flags. We only handle `capture-pane -p ...`.
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
export SESSION_NAME="testsession"

# shellcheck source=/dev/null
source "$LIB"

# ── Case 1: empty file → optimistic 0 ──────────────────────────────────────
empty=$(mktemp)
: > "$empty"
unset TMUX_STUB_PANE_FILE
if ! verify_paste_landed "$empty"; then
    fail "case1: empty file should return 0"
fi

# ── Case 2: blank-line-only file → optimistic 0 ────────────────────────────
blank=$(mktemp)
printf '\n\n\n' > "$blank"
unset TMUX_STUB_PANE_FILE
if ! verify_paste_landed "$blank"; then
    fail "case2: blank-only file should return 0"
fi

# ── Case 3: fingerprint present in pane → 0 ────────────────────────────────
prompt=$(mktemp)
printf '# Task 1234: write a test\nrest of the prompt\n' > "$prompt"
pane=$(mktemp)
printf '╭─ openclaude ─╮\n│ # Task 1234: write a test\n│ rest of the prompt\n╰\n' > "$pane"
export TMUX_STUB_PANE_FILE="$pane"
if ! verify_paste_landed "$prompt"; then
    fail "case3: fingerprint present should return 0"
fi

# ── Case 4: fingerprint missing from pane → 1 ──────────────────────────────
pane_missing=$(mktemp)
printf '╭─ openclaude ─╮\n│ totally different content\n╰\n' > "$pane_missing"
export TMUX_STUB_PANE_FILE="$pane_missing"
if verify_paste_landed "$prompt"; then
    fail "case4: missing fingerprint should return 1"
fi

# ── Case 5: leading blank lines, real content on line 3 → use line 3 ──────
prompt_lead=$(mktemp)
printf '\n\n# Task abc-9999: hello there\nmore\n' > "$prompt_lead"
pane_match=$(mktemp)
printf '╭─ openclaude ─╮\n│ # Task abc-9999: hello there\n╰\n' > "$pane_match"
export TMUX_STUB_PANE_FILE="$pane_match"
if ! verify_paste_landed "$prompt_lead"; then
    fail "case5: should skip blank lines and match third line"
fi

# ── Case 6: long first line, clipped fingerprint still matches first chars ──
prompt_long=$(mktemp)
# 200-char line — fingerprint should be the first 40 (default).
printf '%.0sX' {1..200} > "$prompt_long"
printf '\n' >> "$prompt_long"
pane_long=$(mktemp)
# Show only the first 40 chars in the pane (claude wrapped the rest).
head -c 40 "$prompt_long" > "$pane_long"
printf '\n' >> "$pane_long"
export TMUX_STUB_PANE_FILE="$pane_long"
if ! verify_paste_landed "$prompt_long"; then
    fail "case6: clipped 40-char fingerprint should match"
fi

# ── Case 7: PASTE_FINGERPRINT_LEN override respected ───────────────────────
prompt_short=$(mktemp)
printf 'ABCDEFGH 12345\n' > "$prompt_short"
pane_short=$(mktemp)
printf '╭─\n│ ABCDE rest cut off\n╰\n' > "$pane_short"
export TMUX_STUB_PANE_FILE="$pane_short"
PASTE_FINGERPRINT_LEN=5 verify_paste_landed "$prompt_short" || \
    fail "case7: with PASTE_FINGERPRINT_LEN=5, only first 5 chars must match"

# ── Case 8 (Bug 12 fix): only half-fingerprint visible → still 0 ──────────
# claude wraps long input lines or inserts box-border glyphs in the middle.
# The full fingerprint then misses, but a 50%-prefix matches. New
# progressive shrinking must catch this and return 0.
prompt_wrap=$(mktemp)
printf 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\n' > "$prompt_wrap"
# full = 40 chars "AAAA...A"; pane shows only first 20 (claude wrapped).
pane_wrap=$(mktemp)
printf '╭─\n│ AAAAAAAAAAAAAAAAAAAA wrapped...\n╰\n' > "$pane_wrap"
export TMUX_STUB_PANE_FILE="$pane_wrap"
# Short probe-loop so the test isn't slow.
PASTE_PROBE_ATTEMPTS=1 PASTE_PROBE_INTERVAL_SEC=0 verify_paste_landed "$prompt_wrap" \
    || fail "case8: 50%-prefix match should return 0"

# ── Case 9 (Bug 12 fix): probe-loop tunables respected ────────────────────
# With PASTE_PROBE_ATTEMPTS=1 we only get one probe; verify_paste_landed
# must not crash and must return 1 for a definite miss in that single shot.
pane_clear_miss=$(mktemp)
printf 'something completely different\n' > "$pane_clear_miss"
export TMUX_STUB_PANE_FILE="$pane_clear_miss"
prompt_miss=$(mktemp)
printf 'UNIQUE-FINGERPRINT-XYZ-1234567890\n' > "$prompt_miss"
if PASTE_PROBE_ATTEMPTS=1 PASTE_PROBE_INTERVAL_SEC=0 verify_paste_landed "$prompt_miss"; then
    fail "case9: with no match anywhere, PASTE_PROBE_ATTEMPTS=1 must still return 1"
fi

echo "PASS: all 9 verify_paste_landed cases"
