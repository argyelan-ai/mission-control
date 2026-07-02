#!/usr/bin/env bash
# poll-health-check.sh — Silent-Failure-Monitor fuer Boss-Host poll.sh
#
# Hintergrund: Am 2026-04-23 lief Boss-Host ~2 Wochen mit einem Shell-Escape-
# Bug (Bug C) in deliver_comments(). Der Fehler produzierte bei jedem System-
# Comment-Dispatch diese Log-Zeilen:
#   poll.sh: line XXX: mc: command not found
#   poll.sh: line XXX: command substitution: syntax error
# Stille Regression — niemand hat gegrept, Close-Reminder kamen 2 Wochen lang
# nicht an Boss, aber das Safety-Net Auto-Close (PR #68) hat das kaschiert.
#
# Diese Script laeuft alle 5min via launchd (~/Library/LaunchAgents/
# com.mc.poll-health.plist), pruft den letzten 5min-Fensterinhalt von
# ~/.mc/agents/boss-host/logs/poll.log auf Error-Patterns und alertet
# via Reports-Telegram-Bot wenn welche gefunden werden.
#
# State-File verhindert Alert-Spam: 1x pro Error-Pattern pro Stunde.
#
# Stoppen:
#   launchctl unload ~/Library/LaunchAgents/com.mc.poll-health.plist

set -euo pipefail

POLL_LOG="$HOME/.mc/agents/boss-host/logs/poll.log"
STATE_FILE="$HOME/.mc/poll-health-state"
LOG_FILE="$HOME/.mc/poll-health.log"

# Lookback-Window: letzte 6min (5min Interval + 1min Puffer). Wenn der Poll-
# log 5min+ alt ist, wird trotzdem die tail -n 500 ausgewertet. Das ist OK —
# Alert-State-Dedup verhindert Wiederholung.
LOOKBACK_MIN=6

# Dedup-Cooldown: pro Error-Pattern max 1 Alert pro 3600s.
ALERT_COOLDOWN=3600

# Error-Patterns: regex → Label
# Wenn einer der Patterns matched → Alert mit Label als Error-Type.
declare -a PATTERNS=(
    "command substitution.*syntax error|BossPoll: Python-Shell-Escape (Bug C regression)"
    "deliver_comments: python parse failed|BossPoll: deliver_comments python crash"
    "command not found|BossPoll: Host-Command missing (PATH oder Tool-Reference kaputt)"
    "ERROR.*asyncpg|BossPoll: DB-Integrity-Error vom Backend"
    "active-task-recovery.*5[0-9][0-9]|BossPoll: Active-Task-Recovery 5xx"
)

# Telegram Bot env
ENV_FILE="$HOME/Workspace/Projects/mission-control/.env"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*" >> "$LOG_FILE"; }

if [ ! -f "$POLL_LOG" ]; then
    log "ERROR: poll.log nicht gefunden: $POLL_LOG"
    exit 0  # Kein Fail — vielleicht ist Boss grade nicht aufgesetzt
fi

if [ ! -f "$ENV_FILE" ]; then
    log "WARNING: .env fehlt — kein Telegram-Alert moeglich: $ENV_FILE"
    exit 0
fi

# Token + Chat-ID aus .env ziehen (nur diese zwei Keys — keine anderen leaken)
# shellcheck disable=SC1090
REPORTS_TOKEN=$(grep -E '^TELEGRAM_REPORTS_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
REPORTS_CHAT=$(grep -E '^TELEGRAM_REPORTS_CHAT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")

if [ -z "$REPORTS_TOKEN" ] || [ -z "$REPORTS_CHAT" ]; then
    log "WARNING: TELEGRAM_REPORTS_BOT_TOKEN oder _CHAT_ID nicht in .env — kein Alert moeglich"
    exit 0
fi

# State-Format: jede Zeile `LABEL\tTIMESTAMP` (TAB als Trenner damit Labels
# Leerzeichen enthalten duerfen). macOS bash 3.2 hat keine associative arrays,
# deswegen grep-basierte Lookups statt declare -A.
touch "$STATE_FILE"

_get_last_alert() {
    local key="$1"
    # Exact-match key bis TAB, return TS. grep exit 1 bei no-match ist hier OK —
    # wir fangen es via || true ab damit pipefail nicht das ganze Script killt.
    { grep -F "$(printf '%s\t' "$key")" "$STATE_FILE" 2>/dev/null || true; } | tail -n 1 | cut -f2
}

_set_last_alert() {
    local key="$1" ts="$2"
    # Alte Eintraege fuer diesen key entfernen, neuen anhaengen
    grep -vF "$(printf '%s\t' "$key")" "$STATE_FILE" > "$STATE_FILE.tmp" 2>/dev/null || true
    printf '%s\t%s\n' "$key" "$ts" >> "$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}

_cleanup_state() {
    local now="$1" max_age="$2"
    local tmp="$STATE_FILE.tmp"
    : > "$tmp"
    while IFS=$'\t' read -r key ts; do
        [ -z "$key" ] && continue
        if [ "$((now - ts))" -lt "$max_age" ] 2>/dev/null; then
            printf '%s\t%s\n' "$key" "$ts" >> "$tmp"
        fi
    done < "$STATE_FILE"
    mv "$tmp" "$STATE_FILE"
}

NOW_EPOCH=$(date +%s)

# Lookback-Window extrahieren — ZEITBASIERT, nicht zeilenbasiert.
#
# Frueher nutzten wir `tail -n 500` als Approximation. Problem: Bei ~30s
# Heartbeat-Interval sind 500 Zeilen mehrere STUNDEN History. Historische
# Fehler von vor einem Fix (z.B. 2026-04-23 Bug C vor PR #85) blieben so
# bis zum naechsten Log-Rotate in der tail-Fenster sichtbar und triggerten
# nach jedem 1h-Cooldown einen neuen Alert. Resultat: Fehlalarme fuer laengst
# behobene Fehler.
#
# Jetzt: awk filtert nur Zeilen, die zu einem Log-Eintrag innerhalb der
# letzten $LOOKBACK_MIN Minuten gehoeren. Error-Zeilen ohne eigenen Timestamp
# (z.B. bash-Fehler `poll.sh: line 210: mc: command not found`) folgen einer
# timestamped Heartbeat-Zeile — awk traegt das "in_window" Flag mit.
CUTOFF_TS=$(date -v-${LOOKBACK_MIN}M '+%Y-%m-%dT%H:%M:%S' 2>/dev/null || date -d "-${LOOKBACK_MIN} minutes" '+%Y-%m-%dT%H:%M:%S')
RECENT_LOG=$(awk -v cutoff="$CUTOFF_TS" '
    /^\[[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\]/ {
        ts = substr($0, 2, 19)
        in_window = (ts >= cutoff) ? 1 : 0
    }
    in_window
' "$POLL_LOG" 2>/dev/null || echo "")

alert_count=0
for pattern_spec in "${PATTERNS[@]}"; do
    pattern="${pattern_spec%%|*}"
    label="${pattern_spec#*|}"

    # Grep extended regex case-sensitive (poll.log logs sind in Deutsch+English,
    # die Error-Patterns sind bewusst in Englisch/Shell-Standard). Nur die
    # ERSTE Match-Zeile pro Run — Details werden vom Alert-Text gezeigt.
    first_match=$(echo "$RECENT_LOG" | grep -E "$pattern" | tail -n 1 || true)
    [ -z "$first_match" ] && continue

    # Dedup: war der letzte Alert fuer dieses Label < $ALERT_COOLDOWN sekunden her?
    last_ts="$(_get_last_alert "$label")"
    last_ts="${last_ts:-0}"
    age=$((NOW_EPOCH - last_ts))
    if [ "$age" -lt "$ALERT_COOLDOWN" ]; then
        log "Match '$label' supressed (cooldown: ${age}s < ${ALERT_COOLDOWN}s)"
        continue
    fi

    # Alert senden via Reports-Bot
    msg="⚠️ <b>MC Health-Check Alert</b>

<b>Pattern:</b> ${label}

<b>Log-Sample:</b>
<code>$(echo "$first_match" | head -c 300 | sed 's/</&lt;/g; s/>/&gt;/g')</code>

<b>Datei:</b> <code>${POLL_LOG}</code>
<b>Zeit:</b> $(date '+%Y-%m-%d %H:%M:%S')

<i>Naechster Alert fuer dieses Pattern fruehestens in $((ALERT_COOLDOWN/60))min.</i>"

    response=$(curl -sf "https://api.telegram.org/bot${REPORTS_TOKEN}/sendMessage" \
        -d "chat_id=${REPORTS_CHAT}" \
        --data-urlencode "text=${msg}" \
        -d "parse_mode=HTML" \
        --max-time 10 2>&1) || {
        log "ERROR: Telegram-Alert fehlgeschlagen fuer '$label': $response"
        continue
    }

    log "ALERT gesendet: '$label' — sample: $first_match"
    _set_last_alert "$label" "$NOW_EPOCH"
    alert_count=$((alert_count + 1))
done

# State-Cleanup: Entries aelter als 48h (172800s) entfernen
_cleanup_state "$NOW_EPOCH" 172800

if [ "$alert_count" -gt 0 ]; then
    log "Run finished — $alert_count Alert(s) gesendet"
fi
