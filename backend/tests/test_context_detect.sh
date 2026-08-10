#!/usr/bin/env bash
# test_context_detect.sh — smoke-tests for the harness-aware context%-scraper
# used by poll.sh's heartbeat() (docker/mc-agent-base/lib/context-detect.sh).
#
# CTX-01 Nachzug (2026-08-09, Mark meldete): heartbeat() suchte NUR Claudes
# `ctx: NN`-Format — bei allen anderen Harnesses (Hermes, omp/Sparky/
# openclaude, Kimi) blieb der Kontext-Balken in der UI dauerhaft bei 0%.
# scrape_context_pct() waehlt das Muster ueber PANE_UI_OVERRIDE (im Image
# gebacken) mit Fallback ueber alle Muster.
#
# Fixtures sind ECHTE, live abgegriffene Statuszeilen (siehe Task-Briefing):
#   Hermes:  ` ⚕ deepseek-v4-flash-0731-... │ 21.3K/262.1K │ [█░░░░░░░░░] 8% │ ...`
#   omp:     `╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 8.3%/262K ⟲ ▶───`
#   Claude:  `ctx: NN` / `ctx NN` (pane_title oder Statuszeile)
#   Kimi:    `context: N% (x/1M)` (siehe ui-detect.sh)
#
# Invoked via tests/test_context_detect.py (pytest wrapper) so it shows up in
# the normal suite. Pattern kopiert von test_ui_detect.sh (Bug 14).

set -euo pipefail

LIB="${1:-$(dirname "$0")/../../docker/mc-agent-base/lib/context-detect.sh}"

if [ ! -f "$LIB" ]; then
    echo "FAIL: lib not found at $LIB" >&2
    exit 2
fi

fail() { echo "FAIL: $1" >&2; exit 1; }

# shellcheck source=/dev/null
source "$LIB"

check() {
    # check DESC TEXT OVERRIDE EXPECTED
    local desc="$1" text="$2" override="$3" expected="$4" got
    got=$(PANE_UI_OVERRIDE="$override" scrape_context_pct "$text")
    [ "$got" = "$expected" ] || fail "$desc: expected '$expected', got '$got'"
}

# ── Hermes: fraction + bar-percent, harness noch nicht gebacken (leerer
# PANE_UI_OVERRIDE) → muss ueber den generischen Fallback gefunden werden.
check "hermes with 8% bar" \
    " ⚕ deepseek-v4-flash-0731-... │ 21.3K/262.1K │ [█░░░░░░░░░] 8% │ 12m │ ⏲ 48s │ ✓ 0s │ ⚠ YOLO" \
    "" "8"

# ── Hermes: genuine 0% (not "no value") must come through as 0, not empty.
check "hermes genuine 0 percent" \
    "│ 0/1M │ [░░░░░░░░░░] 0% │" \
    "" "0"

# ── Hermes: fresh session, NO value yet ("--") — must NOT report 0.
check "hermes ctx -- (no value) must stay empty" \
    "│ ctx -- │ [░░░░░░░░░░] -- │" \
    "" ""

# ── omp/Sparky: percent directly before a slash, harness known (openclaude).
check "omp/sparky percent-before-slash" \
    "╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 8.3%/262K ⟲ ▶───" \
    "openclaude" "8"

# ── Hermes bar-percent in isolation (no fraction anywhere in the text) —
# only the dedicated bar-percent pattern can find this, not the fraction
# fallback. Exercises _ctx_hermes_bar specifically.
check "hermes bar-percent without any fraction present" \
    "[█░░░░░░░░░] 8% │ 12m │ ⏲ 48s" \
    "" "8"

# ── omp/Sparky pattern found even when harness is unknown (generic fallback).
check "omp/sparky pattern via fallback (no override)" \
    "╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 8.3%/262K ⟲ ▶───" \
    "" "8"

# ── Claude: existing `ctx NN%` pane_title / statusline format, unchanged.
check "claude ctx NN%" "✻ ctx 12%" "claude" "12"
check "claude ctx: NN" "some text ctx: 45 more text" "claude" "45"

# ── Kimi: `context: NN%` statusline (see ui-detect.sh).
check "kimi context: NN%" "context: 8% (21.3K/262.1K)" "kimi" "8"

# ── Unknown format entirely → no value, never guess 0.
check "unknown format -> empty" "some random shell prompt with no context info" "" ""

# ── Fraction-only fallback (no % rendered anywhere) → computed from the ratio.
check "fraction-only computed (~8%)" "used 21.3K/262.1K total" "" "8"

# ── Values >100 are rejected (sanitize, defense-in-depth).
check "value >100 rejected" "ctx: 150" "claude" ""

# ── Harness-specific pattern wins over a coincidental match for another
# harness elsewhere in the same text (claude picks its own pattern first).
check "claude harness ignores stray percent-before-slash" \
    "ctx: 12  (unrelated 99%/300 noise)" "claude" "12"

echo "PASS: all scrape_context_pct cases"
