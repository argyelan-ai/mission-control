#!/bin/bash
# telegram-in.sh — Boss-Host Telegram-Inbound-Bridge (laeuft in tmux Window 2).
# Long-pollt getUpdates vom Reports-Bot, filtert nach Marks chat_id,
# injiziert Textnachrichten als Prompt in die LIVE Claude-Session (Window 0).
#
# Pendant zu poll.sh (Window 1), aber Quelle = Telegram statt MC-HTTP.
# Injektion = identische paste_and_submit-Technik (Bugfix 2026-04-23).
#
# Quelle der Tokens: TELEGRAM_REPORTS_BOT_TOKEN / TELEGRAM_REPORTS_CHAT_ID
# aus Backend config.py (Settings.telegram_reports_bot_token / chat_id),
# gelesen aus docker/.env.shared via Backend-Umgebung.
# Host-Bridge liest sie aus ~/.mc/agents/boss-host/agent.env (umgesetzt von Mark).

set -euo pipefail

ENV_FILE="$HOME/.mc/agents/boss-host/agent.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

: "${TELEGRAM_REPORTS_BOT_TOKEN:?TELEGRAM_REPORTS_BOT_TOKEN is not set — Quelle: docker/.env.shared (TELEGRAM_REPORTS_BOT_TOKEN)}"
: "${TELEGRAM_REPORTS_CHAT_ID:?TELEGRAM_REPORTS_CHAT_ID is not set — Quelle: docker/.env.shared (TELEGRAM_REPORTS_CHAT_ID)}"

SESSION_NAME="boss-host"
OFFSET_FILE="$HOME/.mc/agents/boss-host/.telegram-in.offset"
LOG_PREFIX="[telegram-in]"

MAX_BACKOFF=60
RETRY_DELAY=1

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $LOG_PREFIX $*"
}

# load_offset — liest den letzten verarbeiteten offset aus Datei
load_offset() {
    if [ -f "$OFFSET_FILE" ]; then
        cat "$OFFSET_FILE"
    else
        echo "0"
    fi
}

# save_offset — speichert offset
save_offset() {
    echo "$1" > "$OFFSET_FILE"
}

# paste_and_submit TEXT — sendet TEXT via tmux paste-buffer an Window 0.
# Identisch zu poll.sh::paste_and_submit, aber mit Prefix und ohne Datei.
paste_and_submit() {
    local text="$1"
    local prefixed="[Telegram von Mark] ${text}"

    # Temporäre Datei fuer den Prompt-Inhalt
    local tmpfile="/tmp/boss_host_telegram_prompt.txt"
    printf '%s\n' "$prefixed" > "$tmpfile"

    tmux load-buffer "$tmpfile"
    tmux paste-buffer -t "${SESSION_NAME}:0"
    sleep 0.3
    # Explizit Bracketed-Paste-End senden: ESC [ 2 0 1 ~
    # (Bugfix 2026-04-23: naives send-keys haengt claude im Paste-Mode)
    tmux send-keys -t "${SESSION_NAME}:0" -H 1b 5b 32 30 31 7e
    sleep 0.2
    tmux send-keys -t "${SESSION_NAME}:0" Enter

    rm -f "$tmpfile"
    log "Injiziert: ${prefixed:0:80}..."
}

# process_messages — verarbeitet text-Nachrichten aus python-Output
# Input: TSV (update_id \t chat_id \t text), Output: paste_and_submit
process_messages() {
    while IFS=$'\t' read -r uid chat_id text; do
        if [ -z "$uid" ] || [ -z "$chat_id" ] || [ -z "$text" ]; then
            continue
        fi
        # NUR Marks Reports-Chat — fremde Chats ablehnen
        if [ "$chat_id" != "$TELEGRAM_REPORTS_CHAT_ID" ]; then
            log "Ignoriert: chat_id=$chat_id (erwartet $TELEGRAM_REPORTS_CHAT_ID)"
            continue
        fi
        log "Telegram-Nachricht #$uid von chat $chat_id"
        paste_and_submit "$text"
    done
}

# main loop — long-poll getUpdates
main() {
    local offset
    offset=$(load_offset)
    log "Start (offset=$offset)"

    # Preflight: getWebhookInfo — prueft dass kein Webhook gesetzt ist
    log "Preflight: getWebhookInfo ..."
    local webhook_info
    webhook_info=$(curl -sf \
        -X POST "https://api.telegram.org/bot${TELEGRAM_REPORTS_BOT_TOKEN}/getWebhookInfo" \
        -H "Content-Type: application/json" \
        --max-time 30 2>/dev/null || echo '{"ok":false}')

    local webhook_url
    webhook_url=$(echo "$webhook_info" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('result', {}).get('url', ''))
except:
    print('')
" 2>/dev/null || echo "")

    if [ -n "$webhook_url" ]; then
        echo "FATAL: Webhook ist bereits gesetzt auf: $webhook_url" >&2
        echo "telegram-in.sh verwendet long-poll (getUpdates). Ein Webhook kollidiert damit." >&2
        echo "Webhook entfernen: curl -X POST 'https://api.telegram.org/bot<token>/deleteWebhook'" >&2
        exit 1
    fi
    log "Preflight: OK — kein Webhook gesetzt (getUpdates-Slot frei)"

    while true; do
        # getUpdates mit offset, timeout=25 (long-poll), allowed_updates=["message"]
        local response
        response=$(curl -sf \
            -X POST "https://api.telegram.org/bot${TELEGRAM_REPORTS_BOT_TOKEN}/getUpdates" \
            -H "Content-Type: application/json" \
            -d "{\"offset\":${offset},\"timeout\":25,\"allowed_updates\":[\"message\"]}" \
            --max-time 30 2>/dev/null || echo '{"ok":false,"error":"network"}')

        local ok
        ok=$(echo "$response" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('ok', False))
except:
    print(False)
" 2>/dev/null || echo "False")

        if [ "$ok" = "True" ]; then
            # Python: offset aktualisieren + text-Nachrichten filtern
            local updates
            updates=$(echo "$response" | python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
updates = data.get('result', [])

# Hoechsten update_id + 1 als neuen offset berechnen
new_off = int(sys.argv[1]) if len(sys.argv) > 1 else 0
for u in updates:
    uid = u.get('update_id', 0)
    if uid >= new_off:
        new_off = uid + 1

# Neuen offset ausgeben
print('OFFSET:' + str(new_off))

# Nur text-Nachrichten (message oder edited_message), TSV formatiert
for u in updates:
    msg = u.get('message') or u.get('edited_message')
    if msg and 'text' in msg:
        chat_id = str(msg.get('chat', {}).get('id', ''))
        text = msg['text'].replace('\t', ' ').replace('\n', ' ')
        print(f\"MSG:{u['update_id']}\t{chat_id}\t{text}\")
" "$offset" 2>/dev/null || echo "OFFSET:$offset")

            # Offset extrahieren und speichern (|| true — grep exit 1 bei 0 Matches)
            local new_offset
            new_offset=$(echo "$updates" | grep '^OFFSET:' | sed 's/^OFFSET://') || true
            if [ -n "$new_offset" ] && [ "$new_offset" != "0" ]; then
                save_offset "$new_offset"
                log "Offset aktualisiert: $new_offset"
            fi

            # Nachrichten verarbeiten (|| true — grep exit 1 bei 0 Matches)
            echo "$updates" | grep '^MSG:' | sed 's/^MSG://' | process_messages || true

        else
            local err_msg
            err_msg=$(echo "$response" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('description', d.get('error_code', 'unknown')))
except:
    print('network error')
" 2>/dev/null || echo "network error")

            log "API-Fehler: $err_msg (retry in ${RETRY_DELAY}s)"

            # Backoff bei Fehlern
            sleep "$RETRY_DELAY"
            RETRY_DELAY=$(( RETRY_DELAY * 2 ))
            if [ "$RETRY_DELAY" -gt "$MAX_BACKOFF" ]; then
                RETRY_DELAY="$MAX_BACKOFF"
            fi
        fi
    done
}

main
