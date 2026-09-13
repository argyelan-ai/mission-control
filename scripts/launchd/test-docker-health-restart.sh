#!/usr/bin/env bash
# test-docker-health-restart.sh — Mock-basierte Verifikation fuer
# scripts/docker-health-restart.sh (Task 6a5ea249: Host-Waechter).
#
# WARUM ein Mock-Test statt gegen echten Docker/launchctl: Dieses Script
# wurde von einem Deployer-Agent-Container geschrieben, der selbst weder
# `docker` noch `launchctl` noch Host-SSH-Zugriff hat (bewusst so — siehe
# Task-Kommentare). Der Mock ersetzt NUR `docker` und `curl` durch
# Stub-Binaries auf dem PATH; die getestete Logik ist exakt
# scripts/docker-health-restart.sh, unveraendert, byte-fuer-byte wie sie
# unter launchd laeuft. Was der Mock NICHT beweist: dass `/usr/local/bin/docker`
# auf dem echten Mac Mini existiert, dass launchd das Script tatsaechlich
# alle 60s feuert, und dass der echte Telegram-Bot-Token funktioniert — das
# muss nach der Host-Installation (siehe README.md) einmal live gegengeprueft
# werden.
#
# Verwendung:
#   bash scripts/launchd/test-docker-health-restart.sh
#
# Exit 0 = alle Szenarien PASS, Exit 1 = mindestens ein FAIL.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/../docker-health-restart.sh"
TEST_ROOT="$(mktemp -d)"
BIN_DIR="$TEST_ROOT/bin"
TEST_HOME="$TEST_ROOT/home"
REGISTRY="$TEST_ROOT/registry.tsv"
HEALTH_DIR="$TEST_ROOT/health"
RESTART_LOG="$TEST_ROOT/restart.log"
NOTIFY_LOG="$TEST_ROOT/notify.log"
FAKE_ENV="$TEST_ROOT/fake.env"
CURL_FAIL_FLAG="$TEST_ROOT/curl-should-fail"

mkdir -p "$BIN_DIR" "$TEST_HOME" "$HEALTH_DIR"
: > "$REGISTRY"
: > "$RESTART_LOG"
: > "$NOTIFY_LOG"

cat > "$FAKE_ENV" <<EOF
TELEGRAM_REPORTS_BOT_TOKEN=fake-test-token
TELEGRAM_REPORTS_CHAT_ID=fake-test-chat
EOF

# --- Kaputte .env fuer Blocker-1-Probe: TOKEN-Key fehlt komplett (Umbenennung/
# Auskommentieren/Tippfehler) — CHAT_ID ist da, das genuegt laut Review schon,
# um den alten Waechter lautlos sterben zu lassen.
BROKEN_ENV_MISSING_TOKEN="$TEST_ROOT/broken-missing-token.env"
cat > "$BROKEN_ENV_MISSING_TOKEN" <<EOF
TELEGRAM_REPORTS_CHAT_ID=fake-test-chat
EOF

# --- Mock docker: ps/inspect/restart gegen $REGISTRY + $HEALTH_DIR ---------
cat > "$BIN_DIR/docker" <<MOCKDOCKER
#!/usr/bin/env bash
REGISTRY="$REGISTRY"
HEALTH_DIR="$HEALTH_DIR"
RESTART_LOG="$RESTART_LOG"

if [ "\$1" = "ps" ]; then
    svc=""
    prev=""
    for a in "\$@"; do
        if [ "\$prev" = "--filter" ]; then
            svc="\${a#label=com.docker.compose.service=}"
        fi
        prev="\$a"
    done
    awk -F'\t' -v svc="\$svc" '\$1==svc{print \$2}' "\$REGISTRY"
    exit 0
fi

if [ "\$1" = "inspect" ]; then
    container=""
    for a in "\$@"; do container="\$a"; done  # letztes Arg, bash-3.2-kompatibel (kein \${@: -1})
    if [ -f "\$HEALTH_DIR/\$container" ]; then
        cat "\$HEALTH_DIR/\$container"
    else
        echo "missing"
    fi
    exit 0
fi

if [ "\$1" = "restart" ]; then
    container="\$2"
    printf '%s\t%s\n' "\$(date +%s)" "\$container" >> "\$RESTART_LOG"
    exit 0
fi

echo "mock-docker: unhandled args: \$*" >&2
exit 1
MOCKDOCKER
chmod +x "$BIN_DIR/docker"

# --- Mock curl: nur fuer die Telegram-notify()-Calls im Script relevant ----
# Steuerbar per CURL_FAIL_FLAG-Datei (Blocker-2-Probe: simuliert einen
# Telegram-Ausfall/Netzhaenger ohne echtes Netz anzufassen) — `touch`/`rm`
# schaltet den Fehlerfall zur Laufzeit um, ohne den Mock neu zu erzeugen.
cat > "$BIN_DIR/curl" <<MOCKCURL
#!/usr/bin/env bash
NOTIFY_LOG="$NOTIFY_LOG"
FAIL_FLAG="$CURL_FAIL_FLAG"
{
    echo "===CURL-CALL==="
    printf 'ts=%s\n' "\$(date +%s)"
    printf '%s\n' "\$*"
} >> "\$NOTIFY_LOG"
if [ -f "\$FAIL_FLAG" ]; then
    echo "mock-curl: simulierter Netzfehler (Blocker-2-Probe)" >&2
    exit 7
fi
echo '{"ok":true,"result":{"message_id":1}}'
exit 0
MOCKCURL
chmod +x "$BIN_DIR/curl"

# --- Test-Helpers -----------------------------------------------------------
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

register_container() {
    local svc="$1" container="$2" health="$3"
    printf '%s\t%s\n' "$svc" "$container" >> "$REGISTRY"
    echo "$health" > "$HEALTH_DIR/$container"
}

set_health() {
    echo "$2" > "$HEALTH_DIR/$1"
}

restart_count_for() {
    # grep -c gibt bei 0 Treffern bereits "0" aus UND exit-code 1 zurueck —
    # ein zusaetzliches "|| echo 0" wuerde eine zweite "0"-Zeile anhaengen.
    grep -cF "$(printf '\t%s' "$1")" "$RESTART_LOG" 2>/dev/null
}

notify_count() {
    # Jede Notify-Nachricht ist selbst mehrzeilig (HTML mit Leerzeilen) —
    # wc -l auf dem Rohlog wuerde Zeilen INNERHALB einer Nachricht mitzaehlen.
    # Der Mock-curl schreibt daher einen expliziten Trenner pro Aufruf. Zaehlt
    # JEDEN curl-Aufruf (auch fehlgeschlagene, Blocker-2-Proben) — fuer
    # "wirklich zugestellt" die Log-Zeilen "...-MELDUNG gesendet" verwenden.
    grep -c '^===CURL-CALL===$' "$NOTIFY_LOG" 2>/dev/null
}

log_line_count() {
    grep -cF "$1" "$TEST_HOME/.mc/docker-health-restart.log" 2>/dev/null
}

# Entprellung (Debounce-Ticks, Recovery-Ticks) und die kaputte .env sind
# bewusst NICHT im run_tick-Default, sondern per CUR_DEBOUNCE/CUR_HEALTHY_CLEAR
# auf "1" (= sofort wirksam, das alte Vor-Runde-2-Verhalten) vorbelegt — die
# bestehenden Szenarien 1-5 pruefen andere Eigenschaften und sollen dafuer
# nicht extra Ticks fuer die neue Entprellung mitschleppen. Szenario 8/9
# testen die Entprellung selbst explizit mit den Produktions-Defaults (2/3).
run_tick() {
    HOME="$TEST_HOME" \
    PATH="$BIN_DIR:$PATH" \
    MC_ENV_FILE="${CUR_ENV_FILE:-$FAKE_ENV}" \
    MC_HEALTH_ALLOWLIST="${CUR_ALLOWLIST:-cdp-browser playwright-mcp}" \
    MC_HEALTH_MAX_ATTEMPTS="${CUR_MAX_ATTEMPTS:-3}" \
    MC_HEALTH_BACKOFF_BASE_SECONDS="${CUR_BACKOFF:-120}" \
    MC_HEALTH_DEBOUNCE_TICKS="${CUR_DEBOUNCE:-1}" \
    MC_HEALTH_HEALTHY_CLEAR_TICKS="${CUR_HEALTHY_CLEAR:-1}" \
    MC_HEALTH_MAX_GIVEUP_NOTIFY_ATTEMPTS="${CUR_MAX_GIVEUP_NOTIFY:-5}" \
    bash "$SCRIPT_PATH"
}

echo "== Test-Root: $TEST_ROOT =="
echo

# =============================================================================
# Szenario 1 — Wirkbeweis per Sabotage: unhealthy -> Restart + genau 1 Meldung
# =============================================================================
echo "--- Szenario 1: Sabotage-Wirkbeweis ---"
register_container "cdp-browser" "cdp-browser-mock-1" "healthy"

SABOTAGE_TS=$(date '+%Y-%m-%dT%H:%M:%S')
run_tick >/dev/null 2>&1
assert_eq "Szenario1: gesunder Container beim ersten Tick nicht neugestartet" "0" "$(restart_count_for cdp-browser-mock-1)"

# Sabotage: Health auf unhealthy setzen (entspricht z.B. haengender CDP-Verbindung)
set_health "cdp-browser-mock-1" "unhealthy"
run_tick >/dev/null 2>&1
RESTART_TS=$(date '+%Y-%m-%dT%H:%M:%S')

assert_eq "Szenario1: Restart nach Sabotage ausgeloest" "1" "$(restart_count_for cdp-browser-mock-1)"
assert_eq "Szenario1: genau 1 Meldung nach dem ersten unhealthy-Tick" "1" "$(notify_count)"
echo "  Sabotage-Zeitpunkt : $SABOTAGE_TS"
echo "  Restart-Zeitpunkt  : $RESTART_TS  (< 2min, selber Tick — Intervall 60s deckt die DoD-Vorgabe)"
grep -q "Docker-Health-Restart" "$NOTIFY_LOG" && pass "Szenario1: Notify-Payload enthaelt erwarteten Titel" || fail "Szenario1: Notify-Payload fehlt/falsch"

# Weitere Ticks bei weiterhin unhealthy (aber < MAX_ATTEMPTS) duerfen NICHT
# nochmal melden (nur einmalig pro Incident-Start).
run_tick >/dev/null 2>&1
assert_eq "Szenario1: kein Spam bei erneutem unhealthy-Tick (noch < MAX_ATTEMPTS, Backoff aktiv)" "1" "$(notify_count)"

# Recovery: Container wird wieder gesund -> State reset, keine weitere Meldung
set_health "cdp-browser-mock-1" "healthy"
run_tick >/dev/null 2>&1
assert_eq "Szenario1: nach Recovery keine weitere Meldung" "1" "$(notify_count)"
[ ! -f "$TEST_HOME/.mc/docker-health-restart-state/cdp-browser.state" ] \
    && pass "Szenario1: Incident-State nach Recovery geloescht" \
    || fail "Szenario1: Incident-State haette geloescht sein muessen"
echo

# =============================================================================
# Szenario 2 — Gegenprobe: gesunde Services bleiben ueber mehrere Ticks unberuehrt
# =============================================================================
echo "--- Szenario 2: Gegenprobe gesunde Services ---"
register_container "playwright-mcp" "playwright-mcp-mock-1" "healthy"
for i in 1 2 3 4 5; do
    run_tick >/dev/null 2>&1
done
assert_eq "Szenario2: gesunder Service nach 5 Ticks nie neugestartet" "0" "$(restart_count_for playwright-mcp-mock-1)"
BASELINE_NOTIFY="$(notify_count)"
assert_eq "Szenario2: keine neuen Meldungen durch gesunde Services" "1" "$BASELINE_NOTIFY"
echo

# =============================================================================
# Szenario 3 — Gegenprobe Agenten: mc-agent-* wird NIE angefasst, selbst wenn
# es (fehlerhaft) in der Allowlist steht — der gefaehrlichste Fall.
# =============================================================================
echo "--- Szenario 3: Gegenprobe mc-agent-* Hard-Guard ---"
register_container "mc-agent-hermes" "mc-agent-hermes-mock-1" "unhealthy"
CUR_ALLOWLIST="cdp-browser playwright-mcp mc-agent-hermes"  # Fehlkonfiguration simuliert
run_tick >/dev/null 2>&1
CUR_ALLOWLIST=""  # zurueck auf Default fuer folgende Szenarien

assert_eq "Szenario3: mc-agent-* NICHT neugestartet trotz Allowlist-Eintrag" "0" "$(restart_count_for mc-agent-hermes-mock-1)"
assert_eq "Szenario3: keine zusaetzliche Meldung durch den geblockten Agenten" "$BASELINE_NOTIFY" "$(notify_count)"
grep -q "SICHERHEITSVERSTOSS" "$TEST_HOME/.mc/docker-health-restart.log" \
    && pass "Szenario3: Sicherheitsverstoss wurde geloggt (Allowlist-Fehlkonfiguration sichtbar)" \
    || fail "Szenario3: kein Log-Eintrag fuer den geblockten mc-agent-* Fall"
echo

# =============================================================================
# Szenario 4 — Backoff: bleibt dauerhaft unhealthy -> nach MAX_ATTEMPTS Stopp
# + genau 1 zusaetzliche Meldung, danach Stille (keine Endlosschleife).
# =============================================================================
echo "--- Szenario 4: Backoff + Give-up ---"
register_container "test-stuck-svc" "test-stuck-svc-mock-1" "unhealthy"
CUR_ALLOWLIST="test-stuck-svc"
CUR_MAX_ATTEMPTS=3
CUR_BACKOFF=1   # 1s statt 120s Produktionswert — testet dieselbe Logik in Echtzeit ohne Wartezeit

NOTIFY_BEFORE_S4="$(notify_count)"

run_tick >/dev/null 2>&1   # Versuch 1 (sofort, kein Backoff vor erstem Versuch)
sleep 1.2
run_tick >/dev/null 2>&1   # Versuch 2 (Backoff 1*2^0=1s bereits verstrichen)
sleep 2.2
run_tick >/dev/null 2>&1   # Versuch 3 (Backoff 1*2^1=2s bereits verstrichen)
run_tick >/dev/null 2>&1   # attempts=3=MAX -> Give-up-Tick (kein Backoff-Wait noetig, Zweig kommt vor Backoff-Check)

assert_eq "Szenario4: genau MAX_ATTEMPTS(3) Restart-Versuche" "3" "$(restart_count_for test-stuck-svc-mock-1)"
assert_eq "Szenario4: genau 2 Meldungen fuer diesen Incident (Start + Give-up)" "$((NOTIFY_BEFORE_S4 + 2))" "$(notify_count)"

# Weitere 5 Ticks nach Give-up: absolute Stille, keine Endlosschleife
for i in 1 2 3 4 5; do
    run_tick >/dev/null 2>&1
done
assert_eq "Szenario4: nach Give-up keine weiteren Restarts (5 weitere Ticks)" "3" "$(restart_count_for test-stuck-svc-mock-1)"
assert_eq "Szenario4: nach Give-up keine weiteren Meldungen (5 weitere Ticks)" "$((NOTIFY_BEFORE_S4 + 2))" "$(notify_count)"
grep -q "aufgegeben" "$NOTIFY_LOG" \
    && pass "Szenario4: Give-up-Meldung enthaelt erwarteten Text" \
    || fail "Szenario4: Give-up-Meldung fehlt/falscher Text"
echo

# =============================================================================
# Szenario 5 — Statischer Check: kein docker.sock in irgendeinem Container
# =============================================================================
echo "--- Szenario 5: docker.sock Check (repo-weit) ---"
COMPOSE_FILE="$SCRIPT_DIR/../../docker-compose.yml"
if [ -f "$COMPOSE_FILE" ]; then
    # Service-Kontext mitfuehren (awk: letzter "  <name>:" Top-Level-Key vor
    # dem docker.sock-Treffer) statt reiner Zeilen-Textsuche — eine Kommentar-
    # zeile ("sondern via DOCKER_HOST mit diesem Proxy") ODER die Volume-Zeile
    # selbst enthalten das Wort "docker-socket-proxy" naemlich NICHT woertlich.
    # Nur echte Volume-Mount-Zeilen zaehlen ("- /var/run/docker.sock:...") —
    # nicht Kommentarzeilen, die docker.sock nur in Prosa erwaehnen (z.B. der
    # erklaerende Kommentar UEBER dem docker-socket-proxy-Service, der sonst
    # faelschlich dem textuell vorherigen Service zugeordnet wuerde).
    SOCK_SERVICES=$(awk '
        /^  [a-zA-Z0-9_-]+:$/ { svc = $1 }
        /^[ \t]*-[ \t]*\/var\/run\/docker\.sock:/ { print svc }
    ' "$COMPOSE_FILE" | sort -u)
    if [ "$SOCK_SERVICES" = "docker-socket-proxy:" ]; then
        pass "Szenario5: einziger docker.sock-Bezug bleibt der bestehende docker-socket-proxy (ro, gefiltert) — kein neuer Mount"
    else
        fail "Szenario5: unerwartete Services mit docker.sock-Bezug: $SOCK_SERVICES"
    fi
else
    fail "Szenario5: docker-compose.yml nicht gefunden unter $COMPOSE_FILE"
fi
# Bare-Substring-Check waere hier falsch-positiv: das Script ERKLAERT in
# Kommentaren bewusst, WARUM es docker.sock vermeidet. Relevant ist nur, ob
# der eigentliche Socket-Pfad referenziert/gemountet wird.
grep -q '/var/run/docker\.sock' "$SCRIPT_DIR/../docker-health-restart.sh" \
    && fail "Szenario5: docker-health-restart.sh referenziert den docker.sock-Pfad direkt" \
    || pass "Szenario5: docker-health-restart.sh mountet/referenziert /var/run/docker.sock NICHT (nutzt die docker-CLI direkt auf dem Host)"
echo

# =============================================================================
# Szenario 6 — Sabotage Blocker 1: fehlender Telegram-Key toetet den Waechter
# nicht mehr lautlos (scripts/docker-health-restart.sh:117-124).
# =============================================================================
echo "--- Szenario 6: Sabotage Blocker 1 (fehlender Telegram-Key) ---"
register_container "b1-svc" "b1-svc-mock-1" "unhealthy"
CUR_ALLOWLIST="b1-svc"
CUR_ENV_FILE="$BROKEN_ENV_MISSING_TOKEN"
NOTIFY_BEFORE_S6="$(notify_count)"

run_tick >/dev/null 2>&1
RC_S6=$?
CUR_ALLOWLIST=""
CUR_ENV_FILE=""

assert_eq "Szenario6: Skript beendet sich trotz fehlendem TOKEN-Key mit exit 0 (kein set -e Abbruch)" "0" "$RC_S6"
assert_eq "Szenario6: Restart findet trotzdem statt (unabhaengig vom Telegram-Key)" "1" "$(restart_count_for b1-svc-mock-1)"
assert_eq "Szenario6: kein curl-Aufruf (REPORTS_TOKEN leer -> notify() faengt frueher ab)" "$NOTIFY_BEFORE_S6" "$(notify_count)"
[ "$(log_line_count "WARNING: kein Telegram-Alert moeglich")" -ge 1 ] \
    && pass "Szenario6: WARNING-Log-Zeile vorhanden statt lautlosem Abbruch" \
    || fail "Szenario6: keine WARNING-Zeile -- Waechter waere vor Blocker-1-Fix lautlos gestorben"
[ "$(log_line_count "UNHEALTHY b1-svc")" -ge 1 ] \
    && pass "Szenario6: normale Tick-Verarbeitung (UNHEALTHY-Log) lief trotz fehlendem Key weiter" \
    || fail "Szenario6: keine UNHEALTHY-Log-Zeile -- Skript ist vermutlich vor dem ersten log() abgebrochen (Blocker-1-Regression)"
echo

# =============================================================================
# Szenario 7 — Sabotage Blocker 2: Aufgeben-Meldung gilt nicht als zugestellt,
# wenn Telegram beim Aufgeben-Tick fehlschlaegt (scripts/docker-health-restart.sh:293-311).
# =============================================================================
echo "--- Szenario 7: Sabotage Blocker 2 (Telegram faellt beim Aufgeben aus) ---"
register_container "b2-svc" "b2-svc-mock-1" "unhealthy"
CUR_ALLOWLIST="b2-svc"
CUR_MAX_ATTEMPTS=3
CUR_BACKOFF=1

run_tick >/dev/null 2>&1   # Versuch 1
sleep 1.2
run_tick >/dev/null 2>&1   # Versuch 2
sleep 2.2
run_tick >/dev/null 2>&1   # Versuch 3 (attempts=3=MAX erreicht, naechster Tick ist der Aufgeben-Tick)

RESTARTS_BEFORE_S7_GIVEUP="$(restart_count_for b2-svc-mock-1)"
touch "$CURL_FAIL_FLAG"    # Telegram "faellt aus" -- ab jetzt schlaegt jeder notify()-Call fehl
run_tick >/dev/null 2>&1   # Aufgeben-Tick #1, Telegram kaputt

assert_eq "Szenario7: kein zusaetzlicher Restart-Versuch beim Aufgeben-Tick" "$RESTARTS_BEFORE_S7_GIVEUP" "$(restart_count_for b2-svc-mock-1)"
[ "$(log_line_count "Telegram-Alert fehlgeschlagen")" -ge 1 ] \
    && pass "Szenario7: Telegram-Fehler wurde geloggt" \
    || fail "Szenario7: kein Log-Eintrag fuer den Telegram-Fehler"
assert_eq "Szenario7: Aufgeben-Meldung gilt NICHT als gesendet, solange Telegram kaputt ist" "0" "$(log_line_count "GIVE-UP-MELDUNG gesendet fuer b2-svc")"

run_tick >/dev/null 2>&1   # Aufgeben-Tick #2, Telegram weiterhin kaputt -- muss erneut versuchen, nicht aufgeben
[ "$(log_line_count "GIVE-UP-MELDUNG fuer b2-svc fehlgeschlagen")" -ge 1 ] \
    && pass "Szenario7: fehlgeschlagene Aufgeben-Meldung wird erneut versucht (nicht als erledigt verbucht)" \
    || fail "Szenario7: kein Retry-Log-Eintrag fuer die fehlgeschlagene Aufgeben-Meldung"
assert_eq "Szenario7: nach 2 Telegram-Fehlversuchen immer noch keine zugestellte Aufgeben-Meldung" "0" "$(log_line_count "GIVE-UP-MELDUNG gesendet fuer b2-svc")"

rm -f "$CURL_FAIL_FLAG"   # Telegram wieder heil
run_tick >/dev/null 2>&1   # Aufgeben-Tick #3, jetzt sollte sie ankommen
assert_eq "Szenario7: sobald Telegram wieder erreichbar ist, wird die Aufgeben-Meldung genau einmal nachgeholt" "1" "$(log_line_count "GIVE-UP-MELDUNG gesendet fuer b2-svc")"

RESTARTS_AFTER_S7_SUCCESS="$(restart_count_for b2-svc-mock-1)"
run_tick >/dev/null 2>&1
run_tick >/dev/null 2>&1
assert_eq "Szenario7: given_up haelt jetzt -- keine weiteren Restarts nach erfolgreicher Aufgeben-Meldung" "$RESTARTS_AFTER_S7_SUCCESS" "$(restart_count_for b2-svc-mock-1)"
assert_eq "Szenario7: given_up haelt jetzt -- keine zweite Aufgeben-Meldung" "1" "$(log_line_count "GIVE-UP-MELDUNG gesendet fuer b2-svc")"

CUR_ALLOWLIST=""
CUR_MAX_ATTEMPTS=""
CUR_BACKOFF=""
echo

# =============================================================================
# Szenario 8 — Entprellung: ein einzelner unhealthy-Tick loest KEINEN Restart
# aus, zwei aufeinanderfolgende schon (scripts/docker-health-restart.sh:272-279).
# =============================================================================
echo "--- Szenario 8: Entprellung (Debounce) ---"
register_container "debounce-svc" "debounce-svc-mock-1" "healthy"
CUR_ALLOWLIST="debounce-svc"
CUR_DEBOUNCE=2
run_tick >/dev/null 2>&1   # Baseline-Tick, gesund

set_health "debounce-svc-mock-1" "unhealthy"
RESTARTS_BEFORE_S8="$(restart_count_for debounce-svc-mock-1)"
NOTIFY_BEFORE_S8="$(notify_count)"

run_tick >/dev/null 2>&1   # 1. unhealthy-Tick in Folge
assert_eq "Szenario8: EIN unhealthy-Tick loest KEINEN Restart aus" "$RESTARTS_BEFORE_S8" "$(restart_count_for debounce-svc-mock-1)"
assert_eq "Szenario8: EIN unhealthy-Tick loest KEINE Meldung aus" "$NOTIFY_BEFORE_S8" "$(notify_count)"
[ "$(log_line_count "DEBOUNCE debounce-svc")" -ge 1 ] \
    && pass "Szenario8: DEBOUNCE-Log-Zeile beim ersten unhealthy-Tick" \
    || fail "Szenario8: keine DEBOUNCE-Log-Zeile beim ersten unhealthy-Tick"

run_tick >/dev/null 2>&1   # 2. unhealthy-Tick IN FOLGE -- jetzt darf's losgehen
assert_eq "Szenario8: ZWEI unhealthy-Ticks IN FOLGE loesen den Restart aus" "$((RESTARTS_BEFORE_S8 + 1))" "$(restart_count_for debounce-svc-mock-1)"
assert_eq "Szenario8: Start-Meldung beim Erreichen der Debounce-Schwelle" "$((NOTIFY_BEFORE_S8 + 1))" "$(notify_count)"

# Gegenprobe: nicht-konsekutive unhealthy-Ticks akkumulieren sich NICHT --
# der Streak muss bei jedem gesunden Tick auf 0 zurueckfallen.
register_container "debounce-reset-svc" "debounce-reset-svc-mock-1" "healthy"
CUR_ALLOWLIST="debounce-reset-svc"
run_tick >/dev/null 2>&1
set_health "debounce-reset-svc-mock-1" "unhealthy"
run_tick >/dev/null 2>&1                              # Streak 1/2
set_health "debounce-reset-svc-mock-1" "healthy"
run_tick >/dev/null 2>&1                              # Streak reisst ab
set_health "debounce-reset-svc-mock-1" "unhealthy"
run_tick >/dev/null 2>&1                              # Streak wieder nur 1/2, NICHT 2/2
assert_eq "Szenario8: nicht-konsekutive unhealthy-Ticks akkumulieren sich nicht (Streak reisst bei jedem gesunden Tick ab)" "0" "$(restart_count_for debounce-reset-svc-mock-1)"

CUR_ALLOWLIST=""
CUR_DEBOUNCE=""
echo

# =============================================================================
# Szenario 9 — Sabotage Blocker 3 + Flatter-Test: given_up ueberlebt einen
# einzelnen gesunden Tick, Meldungen bleiben ueber viele Flatter-Ticks
# begrenzt (scripts/docker-health-restart.sh:237-256).
# =============================================================================
echo "--- Szenario 9: Sabotage Blocker 3 + Flatter-Test (Produktions-Entprellung) ---"
register_container "flatter-svc" "flatter-svc-mock-1" "unhealthy"
CUR_ALLOWLIST="flatter-svc"
CUR_MAX_ATTEMPTS=3
CUR_BACKOFF=0        # Backoff-Wartezeit deaktiviert -- isoliert die Streak-Logik von echter Wartezeit, kein Sleep noetig.
CUR_DEBOUNCE=2       # Produktions-Default, explizit (Harness-Default fuer die aelteren Szenarien ist 1).
CUR_HEALTHY_CLEAR=3  # Produktions-Default.

RESTARTS_BEFORE_S9="$(restart_count_for flatter-svc-mock-1)"
NOTIFY_BEFORE_S9="$(notify_count)"

# Flatter-Muster: 2x unhealthy, 1x gesund, wiederholt -- eine WebSocket-
# Verbindung, die sich kurz "erholt" bevor sie wieder haengt (der
# Anlass-Incident dieses PRs), nicht reine Jeden-Tick-Alternierung.
TICK_PATTERN="U U H U U H U U H U U H"
for tick_state in $TICK_PATTERN; do
    if [ "$tick_state" = "U" ]; then
        set_health "flatter-svc-mock-1" "unhealthy"
    else
        set_health "flatter-svc-mock-1" "healthy"
    fi
    run_tick >/dev/null 2>&1
done
TICK_COUNT=$(echo "$TICK_PATTERN" | wc -w | tr -d ' ')
RESTARTS_S9=$(( $(restart_count_for flatter-svc-mock-1) - RESTARTS_BEFORE_S9 ))
NOTIFY_S9=$(( $(notify_count) - NOTIFY_BEFORE_S9 ))

echo "  Flatter-Probe: ${TICK_COUNT} Ticks (Muster 2x unhealthy / 1x healthy wiederholt)"
echo "  Ergebnis: ${RESTARTS_S9} Restarts, ${NOTIFY_S9} Meldungen (Referenz Rex-Review, altes Verhalten: 5 Restarts / 5 Meldungen bei 10 Ticks abwechselnd)"

assert_eq "Szenario9: Flatter-Probe ueber ${TICK_COUNT} Ticks -- genau MAX_ATTEMPTS(3) Restarts, kein Doppel-Zyklus nach Gesund-Blip" "3" "$RESTARTS_S9"
assert_eq "Szenario9: Flatter-Probe ueber ${TICK_COUNT} Ticks -- genau 2 Meldungen (Start + Aufgeben), nicht eine pro Zyklus" "2" "$NOTIFY_S9"

# Weiterflattern im selben Incident: given_up haelt jetzt (Blocker 3) --
# Meldungen duerfen NICHT mit der Tick-Zahl weiterwachsen.
EXTRA_PATTERN="U U H U U H"
for tick_state in $EXTRA_PATTERN; do
    if [ "$tick_state" = "U" ]; then
        set_health "flatter-svc-mock-1" "unhealthy"
    else
        set_health "flatter-svc-mock-1" "healthy"
    fi
    run_tick >/dev/null 2>&1
done
EXTRA_COUNT=$(echo "$EXTRA_PATTERN" | wc -w | tr -d ' ')
TOTAL_TICKS=$((TICK_COUNT + EXTRA_COUNT))
RESTARTS_S9_EXTENDED=$(( $(restart_count_for flatter-svc-mock-1) - RESTARTS_BEFORE_S9 ))
NOTIFY_S9_EXTENDED=$(( $(notify_count) - NOTIFY_BEFORE_S9 ))
echo "  Nach insgesamt ${TOTAL_TICKS} Flatter-Ticks: ${RESTARTS_S9_EXTENDED} Restarts, ${NOTIFY_S9_EXTENDED} Meldungen"
assert_eq "Szenario9: nach insgesamt ${TOTAL_TICKS} Flatter-Ticks bleiben es bei 3 Restarts (given_up haelt ueber jeden Gesund-Blip)" "3" "$RESTARTS_S9_EXTENDED"
assert_eq "Szenario9: nach insgesamt ${TOTAL_TICKS} Flatter-Ticks bleiben es bei 2 Meldungen" "2" "$NOTIFY_S9_EXTENDED"

# Blocker-3-Kernbeweis, isoliert: given_up (Feld 4) steht nach den
# Flatter-Ticks weiterhin auf 1 -- ein einzelner gesunder Tick hat es NICHT
# geloescht (das war exakt der Bug).
STATE_AFTER_S9=$(cat "$TEST_HOME/.mc/docker-health-restart-state/flatter-svc.state" 2>/dev/null)
GIVEN_UP_FIELD=$(printf '%s' "$STATE_AFTER_S9" | cut -f4)
assert_eq "Szenario9: given_up steht nach den Flatter-Ticks weiterhin auf 1 (nicht durch einen Gesund-Blip geloescht)" "1" "$GIVEN_UP_FIELD"

CUR_ALLOWLIST=""
CUR_MAX_ATTEMPTS=""
CUR_BACKOFF=""
CUR_DEBOUNCE=""
CUR_HEALTHY_CLEAR=""
echo

# =============================================================================
echo "== Ergebnis: $PASS_COUNT PASS, $FAILURES FAIL =="
echo "== Logs/State fuer manuelle Inspektion: $TEST_ROOT =="

if [ "$FAILURES" -gt 0 ]; then
    exit 1
fi
exit 0
