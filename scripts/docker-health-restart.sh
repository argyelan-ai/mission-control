#!/usr/bin/env bash
# docker-health-restart.sh — Host-Waechter fuer unhealthy MC-Docker-Services.
#
# Hintergrund: Der cdp-browser-Container meldete 5 Tage lang "healthy",
# waehrend die WebSocket-Verbindung faktisch haengen geblieben war (PR #544
# behebt die ERKENNUNG selbst — CDP/WS-Healthcheck statt nur HTTP). Erkennen
# allein heilt nicht: ohne diesen Job wuerde der Container weiter haengen bis
# ein Mensch eingreift — HEUTE reagiert niemand automatisch auf `unhealthy`,
# `restart: unless-stopped` greift nur bei Prozess-Exit. Ein autoheal-Sidecar
# mit docker.sock-Mount wurde abgelehnt — das gibt einem Container faktisch
# Vollzugriff auf den gesamten Docker-Host, unverhaeltnismaessig fuer das
# Problem "haengender Browser". Stattdessen laeuft dieser Watcher HOST-SEITIG
# via launchd (~/Library/LaunchAgents/com.mc.docker-health-restart.plist) —
# dort liegen die Docker-Rechte ohnehin, kein Container bekommt zusaetzliche
# Privilegien, die Angriffsflaeche waechst nicht.
#
# Bewusst GETRENNT von com.mc.poll-health (Boss-Host poll.log Error-Alert) —
# unterschiedliche Concerns, unterschiedliche Intervalle, unterschiedliche
# State-Files. Siehe scripts/launchd/README.md.
#
# Positivliste (NICHT Ausschlussliste): nur Services, die hier explizit
# gelistet sind, werden je angefasst. mc-agent-* Container werden zusaetzlich
# per Hard-Guard geblockt, selbst wenn sie versehentlich in die Liste
# rutschen wuerden — ein Agent-Container mit laufendem Task darf nie
# automatisch neu gestartet werden.
#
# Laeuft alle 60s via launchd. Bei unhealthy: docker restart mit
# Exponential-Backoff. Nach MAX_ATTEMPTS erfolglosen Versuchen: aufhoeren,
# EINMALIG melden, Mensch uebernimmt. Keine Wiederholungs-Meldung pro Tick
# (13 Wiederholungen in 2h an anderer Stelle war der Anlass fuer diese Regel).
#
# Stoppen:
#   launchctl unload ~/Library/LaunchAgents/com.mc.docker-health-restart.plist

set -euo pipefail

# --- Positivliste (Compose-Service-Namen, nicht Container-Namen — robust ---
# --- gegen wechselnden COMPOSE_PROJECT_NAME-Praefix) ------------------------
# cdp-browser    = dediziertes CDP-Chromium fuer den Agent-Browser-Workflow
#                  (der Anlass-Incident). Stateless, profile=browser.
# playwright-mcp = Playwright-MCP-Sidecar, haengt per CDP an cdp-browser.
#                  Stateless, profile=browser.
# Bewusst NICHT drin: db/redis/qdrant (stateful, Blind-Restart riskant),
# backend/frontend/caddy/mc-worker (eigener Deploy-Workflow mit Backup, siehe
# TOOLS.md), mc-playwright (separater Visual-Verifier, anderer Incident),
# jede mc-agent-* Instanz (siehe Hard-Guard unten).
#
# Ueberschreibbar via MC_HEALTH_ALLOWLIST (space-separated) — Default bleibt
# der Produktions-Wert. Existiert primaer damit Tests (siehe
# scripts/launchd/test-docker-health-restart.sh) einen mc-agent-* Eintrag
# INJIZIEREN koennen um den Hard-Guard zu pruefen, ohne die Produktionsdatei
# anzufassen.
# shellcheck disable=SC2206
ALLOWLIST=(${MC_HEALTH_ALLOWLIST:-cdp-browser playwright-mcp})

# --- Zahlen (Begruendung siehe scripts/launchd/README.md) -------------------
# Ueberschreibbar fuer Tests (siehe test-docker-health-restart.sh) — Defaults
# sind die begruendeten Produktions-Werte.
MAX_ATTEMPTS="${MC_HEALTH_MAX_ATTEMPTS:-3}"                   # 3 Versuche ueberleben transiente Ausreisser (z.B.
                           # kurzer Image-Pull-Haenger), ohne einen wirklich
                           # kaputten Service endlos zu bearbeiten.
BACKOFF_BASE_SECONDS="${MC_HEALTH_BACKOFF_BASE_SECONDS:-120}" # Healthcheck-Settle-Zeit der Kandidaten ist bis zu
                           # ~90s (interval 30s * retries 3) — 120s Backoff
                           # gibt dem frisch neugestarteten Container eine
                           # faire Chance, bevor wir erneut urteilen.
INCIDENT_MAX_AGE_SECONDS=86400  # State-Cleanup: verwaiste Incident-Files
                                 # (z.B. Service wurde manuell entfernt)
                                 # nach 24h wegraeumen.

# --- Pfade (folgt der ~/.mc/ Konvention aus poll-health-check.sh) -----------
DOCKER_BIN="${DOCKER_BIN:-/usr/local/bin/docker}"
STATE_DIR="$HOME/.mc/docker-health-restart-state"
LOG_FILE="$HOME/.mc/docker-health-restart.log"
ENV_FILE="${MC_ENV_FILE:-$HOME/Workspace/Projects/mission-control/.env}"

mkdir -p "$STATE_DIR"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*" >> "$LOG_FILE"; }

if [ ! -x "$DOCKER_BIN" ] && ! command -v docker >/dev/null 2>&1; then
    log "ERROR: docker Binary nicht gefunden ($DOCKER_BIN) — breche ab"
    exit 0
fi
command -v docker >/dev/null 2>&1 && DOCKER_BIN="$(command -v docker)"

# --- Telegram Reports-Bot (gleiches Pattern wie poll-health-check.sh) -------
REPORTS_TOKEN=""
REPORTS_CHAT=""
if [ -f "$ENV_FILE" ]; then
    REPORTS_TOKEN=$(grep -E '^TELEGRAM_REPORTS_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
    REPORTS_CHAT=$(grep -E '^TELEGRAM_REPORTS_CHAT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi

notify() {
    local text="$1"
    if [ -z "$REPORTS_TOKEN" ] || [ -z "$REPORTS_CHAT" ]; then
        log "WARNING: kein Telegram-Alert moeglich (TELEGRAM_REPORTS_* fehlt in $ENV_FILE) — Meldung nur geloggt: $text"
        return 0
    fi
    local response
    response=$(curl -sf "https://api.telegram.org/bot${REPORTS_TOKEN}/sendMessage" \
        -d "chat_id=${REPORTS_CHAT}" \
        --data-urlencode "text=${text}" \
        -d "parse_mode=HTML" \
        --max-time 10 2>&1) || {
        log "ERROR: Telegram-Alert fehlgeschlagen: $response"
        return 1
    }
    return 0
}

# --- Hard-Guard: mc-agent-* wird NIEMALS angefasst, egal was in der --------
# --- Allowlist steht. Zweite Verteidigungslinie, nicht die einzige. --------
is_forbidden_agent_container() {
    case "$1" in
        mc-agent-*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Container-Name zu einem Compose-Service-Namen aufloesen ---------------
# Ueber das Compose-Label statt fest verdrahtetem "<project>-<service>-1"
# Namen — robust gegen wechselnden COMPOSE_PROJECT_NAME.
resolve_container_name() {
    local svc="$1"
    "$DOCKER_BIN" ps -a --filter "label=com.docker.compose.service=${svc}" --format '{{.Names}}' | head -n 1
}

health_status() {
    local container="$1"
    "$DOCKER_BIN" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "missing"
}

# --- State-Format pro Service: EINE Zeile "attempts<TAB>last_attempt_ts<TAB>
# notified_start<TAB>given_up" in $STATE_DIR/<service>.state. Bash-3.2-
# kompatibel (macOS Default-Bash) — keine associative arrays, siehe
# poll-health-check.sh Kommentar zum selben Thema.
state_file() { echo "$STATE_DIR/$1.state"; }

read_state() {
    local f
    f="$(state_file "$1")"
    if [ -f "$f" ]; then
        cat "$f"
    else
        echo "0	0	0	0"
    fi
}

write_state() {
    local svc="$1" attempts="$2" last_ts="$3" notified_start="$4" given_up="$5"
    printf '%s\t%s\t%s\t%s\n' "$attempts" "$last_ts" "$notified_start" "$given_up" > "$(state_file "$svc")"
}

clear_state() {
    rm -f "$(state_file "$1")"
}

NOW_EPOCH=$(date +%s)

for svc in "${ALLOWLIST[@]}"; do
    if is_forbidden_agent_container "$svc"; then
        log "SICHERHEITSVERSTOSS: '$svc' matched mc-agent-* und stand trotzdem in der Allowlist — ignoriert, NICHT angefasst."
        continue
    fi

    container="$(resolve_container_name "$svc")"
    if [ -z "$container" ]; then
        log "SKIP $svc — kein laufender Container mit label com.docker.compose.service=$svc gefunden"
        continue
    fi

    # Zweite Verteidigungslinie: auch der AUFGELOESTE Container-Name darf nie
    # mc-agent-* sein (falls Compose-Labels je manipuliert werden koennten).
    if is_forbidden_agent_container "$container"; then
        log "SICHERHEITSVERSTOSS: aufgeloester Container '$container' fuer Service '$svc' matched mc-agent-* — ignoriert, NICHT angefasst."
        continue
    fi

    status="$(health_status "$container")"

    if [ "$status" = "healthy" ] || [ "$status" = "no-healthcheck" ]; then
        # Gesund (oder hat gar keinen Healthcheck) — Incident-State fuer
        # diesen Service zuruecksetzen, damit ein KUENFTIGER Ausfall wieder
        # als neuer Incident behandelt wird (frische Einmal-Meldung).
        if [ -f "$(state_file "$svc")" ]; then
            log "RECOVERED $svc ($container) — Status=$status, Incident-State geloescht"
            clear_state "$svc"
        fi
        continue
    fi

    # --- unhealthy ---
    IFS=$'\t' read -r attempts last_ts notified_start given_up <<< "$(read_state "$svc")"

    if [ "$given_up" = "1" ]; then
        # Backoff bereits ausgeschoepft, Mensch wurde einmalig informiert.
        # Kein weiterer Restart-Versuch, keine weitere Meldung — Stille bis
        # der Service von Hand repariert wird (Status wechselt dann zu
        # healthy und der Incident-State wird oben geloescht) oder ein
        # Mensch den State-File manuell entfernt.
        log "SKIP $svc ($container) — given_up=1, wartet auf manuelle Intervention (Status weiterhin $status)"
        continue
    fi

    if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
        # Letzter Versuch war Attempt #MAX_ATTEMPTS und der Service ist
        # immer noch unhealthy -> Backoff-Ende erreicht, EINMALIG melden,
        # aufhoeren.
        msg="🛑 <b>Docker-Health-Restart — aufgegeben</b>

<b>Service:</b> <code>${svc}</code> (<code>${container}</code>)
<b>Status:</b> weiterhin ${status} nach ${MAX_ATTEMPTS} Neustart-Versuchen
<b>Letzter Versuch:</b> $(date -r "$last_ts" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$last_ts" '+%Y-%m-%d %H:%M:%S')
<b>Jetzt:</b> $(date '+%Y-%m-%d %H:%M:%S')

Automatischer Neustart wurde eingestellt — bitte manuell pruefen. Keine weiteren Meldungen fuer diesen Incident."
        if notify "$msg"; then
            log "GIVE-UP-MELDUNG gesendet fuer $svc nach $attempts Versuchen"
        fi
        write_state "$svc" "$attempts" "$last_ts" "$notified_start" "1"
        continue
    fi

    # Backoff: nach einem Versuch erst nach BACKOFF_BASE_SECONDS * 2^(attempts-1)
    # den naechsten versuchen, damit der Healthcheck des Containers selbst
    # Zeit hat sich zu stabilisieren.
    if [ "$attempts" -gt "0" ]; then
        backoff=$((BACKOFF_BASE_SECONDS * (1 << (attempts - 1))))
        elapsed=$((NOW_EPOCH - last_ts))
        if [ "$elapsed" -lt "$backoff" ]; then
            log "SKIP $svc ($container) — Backoff aktiv (${elapsed}s/${backoff}s seit letztem Versuch #${attempts})"
            continue
        fi
    fi

    next_attempt=$((attempts + 1))
    log "UNHEALTHY $svc ($container) — Status=$status, starte Versuch #${next_attempt}/${MAX_ATTEMPTS}"

    restart_ok=1
    if "$DOCKER_BIN" restart "$container" >> "$LOG_FILE" 2>&1; then
        restart_ok=0
        log "RESTART OK $svc ($container) — Versuch #${next_attempt}"
    else
        log "RESTART FEHLGESCHLAGEN $svc ($container) — Versuch #${next_attempt}"
    fi

    new_notified_start="$notified_start"
    if [ "$notified_start" != "1" ]; then
        msg="🔄 <b>Docker-Health-Restart</b>

<b>Service:</b> <code>${svc}</code> (<code>${container}</code>)
<b>Status vor Neustart:</b> ${status}
<b>Aktion:</b> docker restart (Versuch #${next_attempt}/${MAX_ATTEMPTS})
<b>Zeit:</b> $(date '+%Y-%m-%d %H:%M:%S')

<i>Weitere Versuche laufen still im Hintergrund. Naechste Meldung nur falls der Service nach ${MAX_ATTEMPTS} Versuchen weiterhin unhealthy bleibt.</i>"
        if notify "$msg"; then
            new_notified_start="1"
            log "START-MELDUNG gesendet fuer $svc (Versuch #${next_attempt})"
        fi
    fi

    write_state "$svc" "$next_attempt" "$NOW_EPOCH" "$new_notified_start" "0"
done

# --- Verwaiste State-Files aufraeumen (Service nicht mehr in Allowlist) -----
if [ -d "$STATE_DIR" ]; then
    for f in "$STATE_DIR"/*.state; do
        [ -e "$f" ] || continue
        svc_name="$(basename "$f" .state)"
        found=0
        for allowed in "${ALLOWLIST[@]}"; do
            [ "$allowed" = "$svc_name" ] && found=1 && break
        done
        if [ "$found" = "0" ]; then
            mtime=$(date -r "$f" +%s 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo "$NOW_EPOCH")
            if [ "$((NOW_EPOCH - mtime))" -gt "$INCIDENT_MAX_AGE_SECONDS" ]; then
                log "CLEANUP verwaister State-File fuer '$svc_name' (nicht mehr in Allowlist)"
                rm -f "$f"
            fi
        fi
    done
fi
