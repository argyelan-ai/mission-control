#!/usr/bin/env bash
# test_ui_detect.sh — smoke-tests for the runtime-UI detection heuristic
# used by poll.sh to decide whether to send the bracketed-paste end-marker.
#
# Bug 14 (2026-05-13): openclaude breaks on `\e[201~`, claude-cli needs it.
# detect_pane_ui inspects the last 8 lines of a tmux pane capture and
# returns one of "claude", "openclaude", or "" (undetermined).
#
# Sources docker/mc-agent-base/lib/ui-detect.sh and stubs `tmux` to control
# what `tmux capture-pane` returns. Invoked via tests/test_ui_detect.py
# (pytest wrapper) so the whole thing shows up in the normal suite.

set -euo pipefail

LIB="${1:-$(dirname "$0")/../../docker/mc-agent-base/lib/ui-detect.sh}"

if [ ! -f "$LIB" ]; then
    echo "FAIL: lib not found at $LIB" >&2
    exit 2
fi

fail() { echo "FAIL: $1" >&2; exit 1; }

# Stub tmux. Returns the contents of $TMUX_STUB_PANE_FILE on capture-pane.
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

# ── Case 1: claude-cli input box → "claude" + return 0 ────────────────────
claude_pane=$(mktemp)
cat > "$claude_pane" <<'PANE'
> Reading...

╭──────────────────────────────────────────────────────────────────╮
│ > Type your message...                                           │
╰──────────────────────────────────────────────────────────────────╯
  ? for shortcuts                                              ✻ ctx 12%
PANE
export TMUX_STUB_PANE_FILE="$claude_pane"
out=$(detect_pane_ui "test:0") || fail "case1: detect_pane_ui exit non-zero for claude pane"
[ "$out" = "claude" ] || fail "case1: expected 'claude', got '$out'"

# ── Case 2: openclaude empty `❯ ` prompt → "openclaude" + return 0 ────────
openclaude_pane=$(mktemp)
cat > "$openclaude_pane" <<'PANE'
Ready — type /help to begin

────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────
  bypass permissions on (alt+m to cycle)
PANE
export TMUX_STUB_PANE_FILE="$openclaude_pane"
out=$(detect_pane_ui "test:0") || fail "case2: detect_pane_ui exit non-zero for openclaude pane"
[ "$out" = "openclaude" ] || fail "case2: expected 'openclaude', got '$out'"

# ── Case 3: openclaude detected only via bypass-permissions footer ────────
openclaude_footer=$(mktemp)
cat > "$openclaude_footer" <<'PANE'
some text from previous output
more output here
yet more
last bits of stuff
totally random line
penultimate line
final line of output
  bypass permissions on (alt+m to cycle)
PANE
export TMUX_STUB_PANE_FILE="$openclaude_footer"
out=$(detect_pane_ui "test:0") || fail "case3: detect_pane_ui exit non-zero for footer-only openclaude"
[ "$out" = "openclaude" ] || fail "case3: expected 'openclaude' via footer, got '$out'"

# ── Case 4: bare shell (no claude / no openclaude markers) → "" + return 1 ─
bare_pane=$(mktemp)
cat > "$bare_pane" <<'PANE'
agent@mc-agent-test:/home/agent$ ls
file1  file2  file3
agent@mc-agent-test:/home/agent$
PANE
export TMUX_STUB_PANE_FILE="$bare_pane"
if out=$(detect_pane_ui "test:0"); then
    fail "case4: expected non-zero return for bare shell, got '$out'"
fi
[ -z "$out" ] || fail "case4: expected empty output for bare shell, got '$out'"

# ── Case 5: empty pane (tmux capture-pane returned nothing) → "" + return 1 ─
empty_pane=$(mktemp)
: > "$empty_pane"
export TMUX_STUB_PANE_FILE="$empty_pane"
if out=$(detect_pane_ui "test:0"); then
    fail "case5: expected non-zero return for empty pane, got '$out'"
fi
[ -z "$out" ] || fail "case5: expected empty output for empty pane, got '$out'"

# ── Case 6: both claude + openclaude markers visible → "claude" wins ─────
# Reason: box-glyphs are more specific. openclaude never renders `╭─`.
# A pane showing both is a transient claude-cli moment (e.g. paste-buffer
# still on screen) — treating it as claude keeps the end-marker behavior
# safe.
both_pane=$(mktemp)
cat > "$both_pane" <<'PANE'
╭─ claude-cli input ─╮
│ ❯ some prompt      │
╰────────────────────╯
  bypass permissions on (alt+m to cycle)
PANE
export TMUX_STUB_PANE_FILE="$both_pane"
out=$(detect_pane_ui "test:0") || fail "case6: detect_pane_ui exit non-zero with both markers"
[ "$out" = "claude" ] || fail "case6: expected 'claude' (priority over openclaude), got '$out'"

# ── Case 7: openclaude `❯ ` not bare → still detected (footer fallback) ──
# Some openclaude states render `❯ some-running-command` instead of bare
# `❯ `. The bare-prompt regex won't match, but `bypass permissions` always
# sits at the bottom of the openclaude UI. This case asserts the footer-only
# detection still triggers.
openclaude_busy=$(mktemp)
cat > "$openclaude_busy" <<'PANE'
loading something
─────────────────────────────────────────────
❯ exploring the codebase right now
─────────────────────────────────────────────
  bypass permissions on (alt+m to cycle)
PANE
export TMUX_STUB_PANE_FILE="$openclaude_busy"
out=$(detect_pane_ui "test:0") || fail "case7: detect_pane_ui exit non-zero for busy openclaude"
[ "$out" = "openclaude" ] || fail "case7: expected 'openclaude' for busy pane, got '$out'"

# ── Case 8 (live pilot 2026-07-20): PANE_UI_OVERRIDE wins ─────────────────
# claude-cli 2.1.x has no box glyphs and looks exactly like openclaude —
# the image bakes PANE_UI_OVERRIDE, which must short-circuit the heuristic.
export TMUX_STUB_PANE_FILE="$openclaude_busy"   # pane that LOOKS openclaude
out=$(PANE_UI_OVERRIDE=claude detect_pane_ui "test:0") \
    || fail "case8: override call must return 0"
[ "$out" = "claude" ] || fail "case8: PANE_UI_OVERRIDE=claude must win, got '$out'"

# ── Case 9: override empty → heuristic unchanged ──────────────────────────
out=$(PANE_UI_OVERRIDE= detect_pane_ui "test:0") \
    || fail "case9: empty override must fall through to heuristic"
[ "$out" = "openclaude" ] || fail "case9: empty override must use heuristic, got '$out'"

echo "PASS: all 9 detect_pane_ui cases"
