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

# Ohne diese Definition endet JEDE fehlgeschlagene Assertion mit
# "fail: command not found" (exit 127) statt mit der Meldung, die sagt was
# schiefging — dieselbe Klasse Fehler, die diese Karte behebt.
fail() { echo "FAIL: $1" >&2; exit 1; }

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
# Sabotage-Probe (nicht entfernen): mit dem ALTEN festen 12-Zeilen-Fenster ist
# der abgesendete Nudge im 24-Zeilen-Pane von einem haengengebliebenen nicht zu
# unterscheiden. Schlaegt dieser Block fehl, ist die Anker-Logik tot und C5
# wuerde nur noch zufaellig gruen sein.
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

# ── Bug B1: echtes 24-Zeilen-Pane aus dem Alternate Screen ─────────────────
# Die claude-TUI laeuft im Alternate Screen: `capture-pane -S -2000` liefert
# nur die sichtbaren ~24 Zeilen, KEIN Scrollback. Ein frisch abgesendeter
# Nudge rendert seinen Echo-Abdruck in der unteren Bildschirmhaelfte — mit
# einem festen 12-Zeilen-Feldfenster lag er im "Eingabefeld" und der
# abgesendete Nudge war vom haengengebliebenen nicht zu unterscheiden (beide
# "2"). Fixtures sind echte Panes, nicht synthetische Fuellzeilen.
FIX_DIR="$REPO_ROOT/backend/tests/fixtures/paste"
msg_b1="$FIX_DIR/nudge-message.txt"

# Case C5: abgesendet — Echo im Verlauf (Zeile 14), Composer-Box unten leer.
export TMUX_STUB_PANE_FILE="$FIX_DIR/claude-24-submitted.txt"
out=$(classify_paste_outcome "$msg_b1")
[ "$out" = "0" ] || fail "case C5: abgesendeter Nudge im 24-Zeilen-Pane muss 0 sein, war '$out'"

# Case C6: haengengeblieben — nichts im Verlauf, Text steht IN der Box.
export TMUX_STUB_PANE_FILE="$FIX_DIR/claude-24-unsubmitted.txt"
out=$(classify_paste_outcome "$msg_b1")
[ "$out" = "2" ] || fail "case C6: unabgesendeter Nudge im 24-Zeilen-Pane muss 2 sein, war '$out'"

# Case C7: der Anker haengt am UNTERSTEN `❯`, nicht am ersten. Sonst wuerde
# der Echo-Abdruck im Verlauf die Box-Erkennung nach oben ziehen und C5
# wieder als "2" kippen.
export TMUX_STUB_PANE_FILE="$FIX_DIR/claude-24-submitted.txt"
pane_c7=$(cat "$FIX_DIR/claude-24-submitted.txt")
win=$(_input_field_tail_lines "$pane_c7")
[ "$win" = "3" ] || fail "case C7: Feldfenster im 24-Zeilen-Pane muss 3 sein (Box ab Zeile 22), war '$win'"

# Case C8: explizit gesetztes PASTE_INPUT_TAIL_LINES gewinnt (Ops-Override).
win=$(PASTE_INPUT_TAIL_LINES=9 _input_field_tail_lines "$pane_c7")
[ "$win" = "9" ] || fail "case C8: gesetztes PASTE_INPUT_TAIL_LINES muss gewinnen, war '$win'"

# Case C8b: Pane ohne erkennbare Box darf den poll-Loop nicht killen. poll.sh
# laeuft mit `set -euo pipefail`; grep ohne Treffer gibt 1 zurueck. Der Test
# laeuft in einer eigenen Shell MIT diesen Flags, weil das Smoke-Skript sie
# selbst gesetzt hat und ein Abbruch hier sonst als "Test kaputt" durchginge.
win=$(bash -c 'set -euo pipefail; source "$1"; _input_field_tail_lines "$(printf "a\nb\nc\n")"' _ "$LIB") \
    || fail "case C8b: _input_field_tail_lines bricht unter set -euo pipefail ab, wenn kein Anker im Pane ist"
[ "$win" = "12" ] || fail "case C8b: ohne Anker muss der Default 12 greifen, war '$win'"

# Case C10: der Anker muss auf ECHTEN Panes aller vier Runtimes greifen, nicht
# nur auf den zwei Fixtures oben. Findet er die Composer-Box nicht, faellt er
# stillschweigend auf 12 zurueck — und genau dann ist B1 wieder da, ohne dass
# ein Test rot wird. Erwartung: jedes aufgezeichnete Pane liefert ein Fenster
# kleiner als der Default.
pane_dir="$REPO_ROOT/backend/tests/fixtures/panes"
checked=0
for real in "$pane_dir"/*/idle.txt "$pane_dir"/*/working.txt; do
    [ -f "$real" ] || continue
    win=$(_input_field_tail_lines "$(cat "$real")")
    [ "$win" -gt 0 ] && [ "$win" -lt 12 ] \
        || fail "case C10: ${real#$pane_dir/} ergab Feldfenster '$win' — Composer-Anker nicht gefunden (Fallback auf 12)"
    checked=$((checked + 1))
done
[ "$checked" -ge 6 ] || fail "case C10: nur $checked echte Panes geprueft — Fixtures fehlen, der Test deckt nichts ab"

# Case C9 (Sabotage-Probe): mit dem ALTEN festen 12-Zeilen-Fenster kollabieren
# C5 und C6 auf denselben Wert — genau die Blindheit, die B1 beschreibt. Die
# Probe haelt fest, dass die Anker-Logik der einzige Grund fuer C5 ist.
export TMUX_STUB_PANE_FILE="$FIX_DIR/claude-24-submitted.txt"
out_sub=$(PASTE_INPUT_TAIL_LINES=12 classify_paste_outcome "$msg_b1")
export TMUX_STUB_PANE_FILE="$FIX_DIR/claude-24-unsubmitted.txt"
out_uns=$(PASTE_INPUT_TAIL_LINES=12 classify_paste_outcome "$msg_b1")
[ "$out_sub" = "2" ] && [ "$out_uns" = "2" ] || fail "case C9: Sabotage-Probe erwartet 2/2 mit festem Fenster, war '$out_sub'/'$out_uns' — Fixture oder Logik hat sich verschoben"

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

# ── Bug B3: der laute Pfad ─────────────────────────────────────────────────
# Bleibt der Text auch nach dem zweiten Enter im Feld, darf poll.sh NICHT
# still weitergehen. Verlangt sind zwei Dinge: Kommentar auf die Karte UND
# Statusflanke auf blocked (erst die startet die Lead-Triage). Hier wird
# report_blocker gestubbt und geprueft, dass paste_and_submit es mit der
# richtigen Karte aufruft und 2 zurueckgibt.
export MC_API_URL="http://stub" MC_TOKEN="stub"
PASTE_VERIFY_DELAY_SEC=0
PASTE_RETRY_DELAY_SEC=0
PASTE_MAX_ATTEMPTS=1
READY_TIMEOUT_SEC=0
CURRENT_TASK_ID="stale-task"
CURRENT_BOARD_ID="stale-board"
BLOCKER_LOG=$(mktemp)
report_blocker() { echo "report_blocker task=$1 source=${4:-} detail=$3" >> "$BLOCKER_LOG"; CURRENT_TASK_ID=""; CURRENT_BOARD_ID=""; }
classify_paste_outcome() { echo "2"; }
wait_for_clean_prompt() { PANE_UI_DETECTED="claude"; return 0; }
log() { :; }

msg_e1=$(mktemp)
printf 'Weiter mit der Karte\n' > "$msg_e1"

# Case E1: Dispatch-Pfad — die Eskalation muss die GERADE gepastete Karte
# treffen, nicht die noch in CURRENT_TASK_ID stehende vorherige.
PASTE_ESCALATION_TASK_ID="fresh-task"
PASTE_ESCALATION_BOARD_ID="fresh-board"
rc=0
paste_and_submit "$msg_e1" || rc=$?
[ "$rc" = "2" ] || fail "case E1: paste_and_submit muss 2 zurueckgeben (Karte blockiert), war '$rc'"
grep -q "task=fresh-task" "$BLOCKER_LOG" || fail "case E1: Eskalation traf die falsche Karte: $(cat "$BLOCKER_LOG")"
grep -q "source=poll.sh paste_and_submit" "$BLOCKER_LOG" || fail "case E1: Quellenkennung fehlt: $(cat "$BLOCKER_LOG")"

# Case E2: ohne Escalation-Override faellt es auf die laufende Karte zurueck.
: > "$BLOCKER_LOG"
PASTE_ESCALATION_TASK_ID=""
PASTE_ESCALATION_BOARD_ID=""
CURRENT_TASK_ID="running-task"
CURRENT_BOARD_ID="running-board"
rc=0
paste_and_submit "$msg_e1" || rc=$?
[ "$rc" = "2" ] || fail "case E2: paste_and_submit muss 2 zurueckgeben, war '$rc'"
grep -q "task=running-task" "$BLOCKER_LOG" || fail "case E2: Fallback auf CURRENT_TASK_ID fehlt: $(cat "$BLOCKER_LOG")"

# Case E3: ohne bekannte Karte gibt es nichts zu blockieren — aber still
# weitergegangen wird trotzdem nicht (rc=2, kein report_blocker).
: > "$BLOCKER_LOG"
CURRENT_TASK_ID=""
CURRENT_BOARD_ID=""
rc=0
paste_and_submit "$msg_e1" || rc=$?
[ "$rc" = "2" ] || fail "case E3: paste_and_submit muss auch ohne Karte 2 zurueckgeben, war '$rc'"
[ ! -s "$BLOCKER_LOG" ] || fail "case E3: ohne Karte darf report_blocker nicht laufen: $(cat "$BLOCKER_LOG")"

echo "PASS: all paste-classify smoke tests"
