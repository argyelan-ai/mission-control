#!/usr/bin/env bash
# test-poll-health-check.sh — Mock-basierte Verifikation fuer
# scripts/poll-health-check.sh (Task 3b9d3a00: Telegram-Schluesselnamen
# angleichen).
#
# WARUM ein Mock-Test statt gegen echtes Telegram/Boss-Host: dieses Script
# laeuft in einem Deployer-Agent-Container ohne Zugriff auf den echten Host,
# echte Secrets oder echtes Netz zum Telegram Bot-API (bewusst so — siehe
# Task-Kommentare). Der Mock ersetzt NUR `curl` durch ein Stub-Binary auf dem
# PATH; die getestete Logik ist exakt scripts/poll-health-check.sh,
# unveraendert, byte-fuer-byte wie sie unter launchd laeuft. Was der Mock
# NICHT beweist: dass der echte Telegram-Bot-Token auf dem Host funktioniert
# — das war bereits vor diesem Fix live bestaetigt (PR #546 Wirkbeweis,
# Healthcheck rot -> Restart), nur eben unter dem falschen Schluesselnamen.
#
# Bisher gab es fuer poll-health-check.sh KEINEN Testharness (anders als
# docker-health-restart.sh mit test-docker-health-restart.sh) — dieses File
# ist neu.
#
# Verwendung:
#   bash scripts/launchd/test-poll-health-check.sh
#
# Exit 0 = alle Szenarien PASS, Exit 1 = mindestens ein FAIL.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/../poll-health-check.sh"
TEST_ROOT="$(mktemp -d)"
BIN_DIR="$TEST_ROOT/bin"
TEST_HOME="$TEST_ROOT/home"
NOTIFY_LOG="$TEST_ROOT/notify.log"

POLL_LOG_DIR="$TEST_HOME/.mc/agents/boss-host/logs"
ENV_DIR="$TEST_HOME/Workspace/Projects/mission-control"

mkdir -p "$BIN_DIR" "$POLL_LOG_DIR" "$ENV_DIR" "$TEST_HOME/.mc"
: > "$NOTIFY_LOG"

# --- .env-Varianten -----------------------------------------------------
# 1) Nur die Fallback-Keys (= realer Host-Zustand, Task 3b9d3a00): die
#    Reports-Bot-Keys existieren dort gar nicht.
FALLBACK_ENV="$ENV_DIR/.env.fallback-only"
cat > "$FALLBACK_ENV" <<EOF
TELEGRAM_BOT_TOKEN=fake-fallback-token
TELEGRAM_CHAT_ID=fake-fallback-chat
EOF

# 2) Beide Paare vorhanden -- Reports-Bot muss weiterhin Vorrang haben
#    (zwei Bots, bewusst getrennt, siehe docs/setup/telegram.md).
BOTH_ENV="$ENV_DIR/.env.both"
cat > "$BOTH_ENV" <<EOF
TELEGRAM_REPORTS_BOT_TOKEN=fake-reports-token
TELEGRAM_REPORTS_CHAT_ID=fake-reports-chat
TELEGRAM_BOT_TOKEN=fake-fallback-token
TELEGRAM_CHAT_ID=fake-fallback-chat
EOF

# 3) Gar keine Telegram-Keys -- Gegenprobe: muss weiterhin LAUT loggen und
#    dabei nicht abstuerzen (heutiger Zustand darf nicht verlorengehen).
NO_KEYS_ENV="$ENV_DIR/.env.no-keys"
cat > "$NO_KEYS_ENV" <<EOF
SOME_OTHER_KEY=irrelevant
EOF

# --- Mock curl: nur fuer die Telegram-sendMessage-Calls im Script relevant --
cat > "$BIN_DIR/curl" <<MOCKCURL
#!/usr/bin/env bash
NOTIFY_LOG="$NOTIFY_LOG"
{
    echo "===CURL-CALL==="
    printf '%s\n' "\$*"
} >> "\$NOTIFY_LOG"
echo '{"ok":true,"result":{"message_id":1}}'
exit 0
MOCKCURL
chmod +x "$BIN_DIR/curl"

# --- Mock awk: NUR eine Container-Umgebungsluecke, keine Aenderung am
# getesteten Skript. poll-health-check.sh:139 nutzt Intervall-Regex
# (`{19}` fuer den ISO-Timestamp) in seinem einzigen awk-Aufruf -- das
# funktioniert mit gawk/BSD-awk (Produktions-Mac) klaglos, aber der
# Agent-Container hat nur mawk ohne Intervall-Support (getestet:
# `echo abc123 | awk '/[0-9]{3}/{print}'` liefert mit mawk NICHTS). Dieser
# Shim erkennt NUR die eine Aufrufsignatur (`-v cutoff=`) und bildet exakt
# dieselbe Zeitfenster-Filterlogik per Python nach (das volle
# Interval-Regex-Featureset); jeder andere awk-Aufruf geht unveraendert an
# den echten System-awk durch. scripts/poll-health-check.sh selbst bleibt
# byte-fuer-byte unangetastet.
cat > "$BIN_DIR/awk" <<'MOCKAWK'
#!/usr/bin/env bash
cutoff=""
for a in "$@"; do
    case "$a" in
        cutoff=*) cutoff="${a#cutoff=}" ;;
    esac
done
# poll-health-check.sh ruft immer `awk -v cutoff=... '<prog>' "$POLL_LOG"` --
# das letzte Argument ist der Dateipfad, kein Stdin-Aufruf. Stdin nicht
# anfassen, sonst haengt der Mock (kein EOF, kein Redirect vom Aufrufer).
last_arg="${@: -1}"
if [ -n "$cutoff" ] && [ -f "$last_arg" ]; then
    python3 -c '
import sys, re
cutoff, path = sys.argv[1], sys.argv[2]
in_window = False
pat = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]")
with open(path) as f:
    for line in f:
        m = pat.match(line)
        if m:
            in_window = m.group(1) >= cutoff
        if in_window:
            sys.stdout.write(line)
' "$cutoff" "$last_arg"
else
    exec /usr/bin/awk "$@"
fi
MOCKAWK
chmod +x "$BIN_DIR/awk"

# --- Test-Helpers ------------------------------------------------------
FAILURES=0
PASS_COUNT=0
pass() { printf 'PASS  %s\n' "$1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$desc (=$actual)"
    else
        fail "$desc (erwartet '$expected', war '$actual')"
    fi
}

notify_count() {
    grep -c '^===CURL-CALL===$' "$NOTIFY_LOG" 2>/dev/null
}

log_line_count() {
    grep -cF "$1" "$TEST_HOME/.mc/poll-health.log" 2>/dev/null
}

write_poll_log() {
    local err_line="$1"
    local ts
    ts="$(date '+%Y-%m-%dT%H:%M:%S')"
    printf '[%s] %s\n' "$ts" "$err_line" > "$POLL_LOG_DIR/poll.log"
}

# `set -euo pipefail` im getesteten Skript + eigenes STATE_FILE pro Run:
# jedes Szenario faengt mit frischem State/Log an, sonst greift der
# Alert-Cooldown (1x/Stunde/Pattern) zwischen den Szenarien.
run_check() {
    rm -f "$TEST_HOME/.mc/poll-health-state" "$TEST_HOME/.mc/poll-health.log"
    HOME="$TEST_HOME" \
    PATH="$BIN_DIR:$PATH" \
    SLACK_WEBHOOK_FILE="$TEST_ROOT/no-such-webhook-file" \
    bash "$SCRIPT_PATH"
}

echo "== Test-Root: $TEST_ROOT =="
echo

# =============================================================================
# Szenario 1 — Wirkbeweis Fallback: nur TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
# sind in der .env vorhanden (realer Host-Zustand). Der Waechter muss trotzdem
# zustellen -- mit den RICHTIGEN Werten beim (Mock-)Telegram ankommen, nicht
# nur den Code-Zweig betreten.
# =============================================================================
echo "--- Szenario 1: Wirkbeweis Fallback (nur Command-Bot-Keys vorhanden) ---"
cp "$FALLBACK_ENV" "$ENV_DIR/.env"
write_poll_log "poll.sh: line 210: mc: command not found"

run_check >/dev/null 2>&1
RC_S1=$?

assert_eq "Szenario1: Skript laeuft mit Fallback-Keys sauber durch (exit 0)" "0" "$RC_S1"
assert_eq "Szenario1: genau 1 Meldung ueber den Fallback-Pfad zugestellt" "1" "$(notify_count)"
LAST_MARKER_LINE=$(grep -n '^===CURL-CALL===$' "$NOTIFY_LOG" | tail -n 1 | cut -d: -f1)
LAST_CURL_CALL=$(tail -n "+$LAST_MARKER_LINE" "$NOTIFY_LOG")
echo "$LAST_CURL_CALL" | grep -q "botfake-fallback-token/sendMessage" \
    && pass "Szenario1: curl-Aufruf nutzt den Fallback-TOKEN aus TELEGRAM_BOT_TOKEN" \
    || fail "Szenario1: curl-Aufruf nutzt NICHT den erwarteten Fallback-TOKEN"
echo "$LAST_CURL_CALL" | grep -q "chat_id=fake-fallback-chat" \
    && pass "Szenario1: curl-Aufruf nutzt die Fallback-CHAT_ID aus TELEGRAM_CHAT_ID" \
    || fail "Szenario1: curl-Aufruf nutzt NICHT die erwartete Fallback-CHAT_ID"
[ "$(log_line_count "ALERT gesendet")" -ge 1 ] \
    && pass "Szenario1: 'ALERT gesendet' im Log -- Zustellung bestaetigt, nicht nur Code-Pfad betreten" \
    || fail "Szenario1: kein 'ALERT gesendet' im Log"
rm -f "$ENV_DIR/.env"
echo

# =============================================================================
# Szenario 2 — Vorrang: sind BEIDE Schluesselpaare vorhanden, gewinnt weiter
# der dedizierte Reports-Bot (zwei Bots, bewusst getrennt) -- der Fallback
# darf ein vollstaendig konfiguriertes Reports-Bot-Paar nicht verdraengen.
# =============================================================================
echo "--- Szenario 2: Reports-Bot hat Vorrang vor dem Fallback, wenn beide da sind ---"
cp "$BOTH_ENV" "$ENV_DIR/.env"
write_poll_log "poll.sh: line 210: mc: command not found"

run_check >/dev/null 2>&1
RC_S2=$?

assert_eq "Szenario2: Skript laeuft sauber durch (exit 0)" "0" "$RC_S2"
LAST_MARKER_LINE=$(grep -n '^===CURL-CALL===$' "$NOTIFY_LOG" | tail -n 1 | cut -d: -f1)
LAST_CURL_CALL=$(tail -n "+$LAST_MARKER_LINE" "$NOTIFY_LOG")
echo "$LAST_CURL_CALL" | grep -q "botfake-reports-token/sendMessage" \
    && pass "Szenario2: curl-Aufruf nutzt den REPORTS-TOKEN, nicht den Fallback" \
    || fail "Szenario2: curl-Aufruf nutzt NICHT den erwarteten REPORTS-TOKEN"
echo "$LAST_CURL_CALL" | grep -q "chat_id=fake-reports-chat" \
    && pass "Szenario2: curl-Aufruf nutzt die REPORTS-CHAT_ID, nicht den Fallback" \
    || fail "Szenario2: curl-Aufruf nutzt NICHT die erwartete REPORTS-CHAT_ID"
rm -f "$ENV_DIR/.env"
echo

# =============================================================================
# Szenario 3 — Gegenprobe: fehlen ALLE Telegram-Keys (weder REPORTS_* noch
# die Fallback-Keys) und ist kein Slack-Webhook konfiguriert, muss weiterhin
# LAUT geloggt werden und nichts darf abstuerzen -- vor allem darf das Skript
# unter `set -euo pipefail` nicht schon beim `grep` auf die fehlenden Keys
# lautlos sterben (das war vor diesem Fix ein echtes, eigenstaendiges Risiko:
# ohne `|| true` liefert `grep` bei komplett fehlender Zeile exit 1, und
# pipefail+set -e beenden das Skript VOR dem ersten log()-Aufruf).
# =============================================================================
echo "--- Szenario 3: Gegenprobe -- gar keine Telegram-Keys, kein Slack-Webhook ---"
cp "$NO_KEYS_ENV" "$ENV_DIR/.env"
write_poll_log "poll.sh: line 210: mc: command not found"
NOTIFY_BEFORE_S3="$(notify_count)"

run_check >/dev/null 2>&1
RC_S3=$?

assert_eq "Szenario3: Skript beendet sich sauber (exit 0), kein set -e Abbruch" "0" "$RC_S3"
assert_eq "Szenario3: kein curl-Aufruf -- weder REPORTS_* noch Fallback-Keys vorhanden" "$NOTIFY_BEFORE_S3" "$(notify_count)"
[ "$(log_line_count "WARNING: weder Telegram-Env noch Slack-Webhook-Datei vorhanden")" -ge 1 ] \
    && pass "Szenario3: WARNUNG geloggt -- weiterhin laut, nicht still" \
    || fail "Szenario3: keine WARNUNG geloggt -- Gegenprobe waere stillschweigend fehlgeschlagen"
rm -f "$ENV_DIR/.env"
echo

# =============================================================================
echo "== Ergebnis: $PASS_COUNT PASS, $FAILURES FAIL =="
echo "== Logs/State fuer manuelle Inspektion: $TEST_ROOT =="

if [ "$FAILURES" -gt 0 ]; then
    exit 1
fi
exit 0
