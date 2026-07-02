#!/bin/bash
# poll.sh — Boss-Host HTTP-Poll Loop (laeuft in tmux Window 1).
# Pollt MC-Backend (localhost:8000) fuer naechsten Task, sendet Prompt
# via tmux paste-buffer an interaktives claude in Window 0.
#
# Pendant zu docker/mc-agent-base/poll.sh, aber:
#   - Source aus ~/.mc/agents/boss-host/agent.env
#   - SESSION_NAME=boss-host (Host-tmux-Session)
#   - MC_API_URL=http://localhost:8000
#   - Temp-File /tmp/boss_host_task_prompt.txt (vermeidet Clash mit Container)

set -euo pipefail

ENV_FILE="$HOME/.mc/agents/boss-host/agent.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

: "${MC_API_URL:?MC_API_URL is not set}"
: "${MC_TOKEN:?MC_TOKEN is not set}"

SESSION_NAME="boss-host"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
HEARTBEAT_INTERVAL=30
LAST_HEARTBEAT=0
# Task-ID-Tracking fuer /clear-bei-Task-Wechsel. Siehe Kommentar in run_task().
LAST_DISPATCHED_TASK_ID=""
# Dedup-Guard via dispatch_attempt_id (sicherer als task_id) — verhindert
# Re-Paste-Spam solange der Boss einen Dispatch noch nicht ge-ackt hat.
# Portiert aus docker/mc-agent-base/poll.sh (2026-06-12). Siehe run_task().
LAST_DISPATCHED_ATTEMPT_ID=""

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [$SESSION_NAME] $*"
}

# paste_and_submit FILE — laedt FILE in tmux paste-buffer, pastet in Window 0,
# schickt expliziten Bracketed-Paste-End-Marker + Enter zum Submit.
#
# Hintergrund (Bug 2026-04-23): tmux paste-buffer aktiviert Bracketed-Paste-Mode
# (\e[200~ ... \e[201~) damit claude-CLI den Inhalt als Paste erkennt. Bei
# offiziellem Anthropic claude-CLI auf dem Host kommt der \e[201~ End-Marker
# manchmal nicht korrekt durch — claude bleibt im Paste-Mode haengen, der
# nachfolgende Enter wird als Newline IM Paste-Buffer interpretiert (statt
# Submit). Folge: Prompt sitzt im Input-Feld, Boss arbeitet nicht, DB sagt
# "in_progress" (poll-claim hat ack gesetzt) — System haengt.
#
# Fix: nach paste-buffer den End-Marker EXPLIZIT als Hex senden (-H ...),
# DANN Enter. Verifiziert: Boss reagierte sofort mit Submit (✢ Crystallizing…).
paste_and_submit() {
    local file="$1"
    tmux load-buffer "$file"
    tmux paste-buffer -t "${SESSION_NAME}:0"
    sleep 0.3
    # Explizit Bracketed-Paste-End senden: ESC [ 2 0 1 ~
    tmux send-keys -t "${SESSION_NAME}:0" -H 1b 5b 32 30 31 7e
    sleep 0.2
    tmux send-keys -t "${SESSION_NAME}:0" Enter
}

heartbeat() {
    local status="${1:-idle}"
    python3 -c "
import json, urllib.request, os, sys
payload = {'status': '$status'}
data = json.dumps(payload).encode()
req = urllib.request.Request(
    os.environ['MC_API_URL'] + '/api/v1/agent/me/heartbeat',
    data=data,
    headers={
        'Authorization': 'Bearer ' + os.environ['MC_TOKEN'],
        'Content-Type': 'application/json'
    },
    method='POST'
)
try:
    urllib.request.urlopen(req, timeout=5)
except Exception as e:
    print(f'Heartbeat failed: {e}', file=sys.stderr)
" 2>/dev/null || true
}

poll() {
    curl -sf \
        -H "Authorization: Bearer $MC_TOKEN" \
        "$MC_API_URL/api/v1/agent/me/poll" \
        --max-time 10 \
        2>/dev/null || echo '{"state":"error"}'
}

# Recovery: Beim Startup einen in_progress Task ohne lokalen Kontext zurueck
# auf 'inbox' setzen lassen, damit der naechste Poll den Prompt neu liefert.
# Pendant zu docker/mc-agent-base/poll.sh.
recover_task() {
    # ADR-024: GET /me/active-task-recovery (read-only, kein Status-Change).
    local response
    response=$(curl -sf \
        -H "Authorization: Bearer $MC_TOKEN" \
        "$MC_API_URL/api/v1/agent/me/active-task-recovery" \
        --max-time 8 2>/dev/null || echo '{"active":false}')
    local active
    active=$(echo "$response" | python3 -c "import json,sys; print(json.load(sys.stdin).get('active', False))" 2>/dev/null || echo "False")
    if [ "$active" = "True" ]; then
        local tid
        tid=$(echo "$response" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task',{}).get('id','?'))" 2>/dev/null)
        log "Startup-Recovery: aktiver Task $tid — Prompt wird re-dispatched (read-only)"
        run_task "$response"
    else
        log "Startup-Recovery: kein aktiver Task"
    fi
}

run_task() {
    local response_json="$1"
    local task_id
    task_id=$(echo "$response_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['task']['id'])")
    local board_id
    local attempt_id
    board_id=$(echo "$response_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['task'].get('board_id') or '')" 2>/dev/null || echo "")
    attempt_id=$(echo "$response_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['task'].get('dispatch_attempt_id') or '')" 2>/dev/null || echo "")

    log "Task erhalten: $task_id"

    # Task-Kontext fuer `mc` CLI in der claude-Shell zugaenglich machen. Ohne
    # das sendet `mc` Updates OHNE X-Dispatch-Attempt-Id Header und das Backend
    # lehnt die PATCHes mit 409/missing_dispatch_attempt_id bzw. stale ab
    # (siehe backend/app/routers/agent_scoped.py:2545ff). tmux set-environment
    # exportiert in neue Shells (claude bash tool spawnt fresh); /tmp-File als
    # Fallback fuer den Fall dass die env-Propagation nachhinkt — dieselbe
    # Belt-and-braces Strategie wie im Docker-Agent shared/poll.sh.
    tmux set-environment -t "$SESSION_NAME" TASK_ID "$task_id" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" BOARD_ID "$board_id" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" X_DISPATCH_ATTEMPT_ID "$attempt_id" 2>/dev/null || true
    cat > /tmp/mc-context.env <<EOF
TASK_ID=$task_id
BOARD_ID=$board_id
X_DISPATCH_ATTEMPT_ID=$attempt_id
EOF
    chmod 644 /tmp/mc-context.env 2>/dev/null || true

    echo "$response_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
with open('/tmp/boss_host_task_prompt.txt', 'w') as f:
    f.write(data['task']['prompt'])
"
    heartbeat "working"

    # KEIN /clear bei Task-Wechsel fuer Boss (orchestrator-Rolle).
    #
    # Rationale (Live-Learning 2026-04-24 aus internen Multi-Projekt-Tests):
    # Boss sammelt team-weiten Kontext ueber Tasks hinweg — welche Agents
    # worked, welche Skills ein Worker hat, Reflection-Lessons, laufende
    # Projekte. Ein /clear pro Task-Wechsel wirft das weg → Time-to-First-
    # Action stieg auf 3-8 min weil Boss sich jedes Mal neu orientieren
    # musste (tmux capture zeigte repeated trial-and-error mit mc delegate
    # --help, GET /agent/me, etc.).
    #
    # Context-Overflow-Risiko wird durch Claude Code v2+ auto-compact
    # abgefangen (greift bei ctx > 90%). Typischer Dispatch-Prompt ist
    # 5-10k Tokens, Opus max 200k → ~20 Tasks ohne Compact moeglich, in der
    # Praxis greift Compact frueher.
    #
    # Workers (Cody, Shakespeare, Rex, ...) behalten /clear-on-task-switch
    # in docker/shared/poll.sh — die sollen per-Task isoliert arbeiten
    # (Privacy + deterministische Reproduzierbarkeit). Diese Differenz ist
    # bewusst: Orchestrator braucht Team-Memory, Workers nicht.
    #
    # Notfall-Hard-Reset falls Boss verirrt:
    #   launchctl kickstart -k gui/$(id -u)/com.openclaw.boss
    if [ "$task_id" != "$LAST_DISPATCHED_TASK_ID" ]; then
        log "Task $task_id: neuer Task (previous: ${LAST_DISPATCHED_TASK_ID:-none}) — context beibehalten fuer orchestrator"
    else
        log "Task $task_id: re-dispatch, context kept"
    fi
    LAST_DISPATCHED_TASK_ID="$task_id"

    # Dedup via dispatch_attempt_id (portiert aus docker/mc-agent-base/poll.sh,
    # 2026-06-12): gleiche attempt_id = derselbe Dispatch-Versuch, noch kein ACK
    # vom Boss — NICHT nochmal pasten. Ohne diesen Guard re-pastet poll.sh den
    # Prompt bei JEDEM Poll (~10s), solange der (busy) Boss nicht innerhalb eines
    # Zyklus ackt → Prompt-Spam in die Session (beobachtet 2026-06-12, Task
    # 1fc32243: Dispatch 20:56:27, identischer Re-Paste 20:56:39). Das Backend
    # nimmt diesen Dedup bereits an (agents.py:2062-2066). Eine NEUE attempt_id =
    # echter Re-Dispatch (Review-Reject, Recovery, ACK-Timeout) → pasten. Leere
    # attempt_id deaktiviert Dedup (sicheres Fallback).
    if [ "$attempt_id" = "$LAST_DISPATCHED_ATTEMPT_ID" ] && [ -n "$attempt_id" ]; then
        log "Task $task_id: attempt $attempt_id bereits gesendet, warte auf ACK"
        return
    fi
    LAST_DISPATCHED_ATTEMPT_ID="$attempt_id"

    paste_and_submit /tmp/boss_host_task_prompt.txt
    log "Task $task_id (attempt ${attempt_id:-unbekannt}) an claude gesendet (fire-and-forget)"
}

cancel_task() {
    # Idempotent via $LAST_CANCELLED_TASK_ID Marker — Backend returnt state=cancelled
    # solange Task failed bleibt, aber wir wollen ESC nur EINMAL senden.
    local task_id="$1"
    if [ "$task_id" = "${LAST_CANCELLED_TASK_ID:-}" ]; then
        return 0
    fi
    log "Task $task_id extern auf 'failed' gesetzt — sende ESC an claude"
    tmux send-keys -t "${SESSION_NAME}:0" Escape
    LAST_CANCELLED_TASK_ID="$task_id"
    heartbeat "idle"
}

stop_task_session() {
    # Manual stop vom Operator (run_control=stopped). ESC + /clear + context reset.
    local task_id="$1"
    log "Task $task_id vom Operator gestoppt — ESC + /clear + context reset"
    tmux send-keys -t "${SESSION_NAME}:0" Escape 2>/dev/null || true
    sleep 0.5
    tmux send-keys -t "${SESSION_NAME}:0" "/clear" Enter 2>/dev/null || true
    : > /tmp/mc-context.env 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" TASK_ID "" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" BOARD_ID "" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" X_DISPATCH_ATTEMPT_ID "" 2>/dev/null || true
    heartbeat "idle"
}

deliver_comments() {
    # Neue User-Kommentare aus poll-Response an claude paste-buffer uebergeben.
    # Pendant zu mc-agent-base/poll.sh. Unterschiede:
    #  - Temp-Pfad /tmp/boss_host_new_comments_prompt.txt
    local response_json="$1"
    local count
    count=$(echo "$response_json" | python3 -c "
import json, sys
try:
    print(len(json.load(sys.stdin).get('new_comments') or []))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    if [ "$count" = "0" ] || [ -z "$count" ]; then
        return
    fi

    log "Liefere $count neuen Kommentar(e)/Event(s) an claude"

    # Python via heredoc mit quoted delimiter ('PYEOF') um Shell-Expansion
    # KOMPLETT zu disablen. Vorher: python3 -c "..." mit Double-Quotes liess
    # Bash die Backticks innerhalb von Python-Strings als Command-Substitution
    # interpretieren — `mc done <xxx>` im CLOSE-REMINDER-Text wurde versucht
    # auf dem Host auszufuehren BEVOR Python startete, Ergebnis: "mc: command
    # not found" + Syntax-Errors + close-reminders NIE an Boss geliefert.
    # Siehe Incident-Analyse 2026-04-23 (Bug C).
    export MC_POLL_RESPONSE="$response_json"
    python3 <<'PYEOF' || { log "deliver_comments: python parse failed — skipping"; unset MC_POLL_RESPONSE; return; }
import json, os
data = json.loads(os.environ['MC_POLL_RESPONSE'])
comments = data.get('new_comments') or []
user_c = [c for c in comments if c.get('source') == 'user']
sys_c  = [c for c in comments if c.get('source') == 'system']

lines = []
if user_c:
    lines += [
        '# Neue User-Kommentare auf deinen aktiven Tasks',
        '',
        'Der Operator hat kommentiert. Lies, antworte im Task-Thread, arbeite am Task weiter.',
        '',
    ]
    for c in user_c:
        lines.append(f"## Task: {c['task_title']}  (id: {c['task_id']})")
        lines.append(f"- Zeit: {c['created_at']}")
        lines.append('- Inhalt:')
        for line in c['content'].splitlines():
            lines.append(f'  > {line}')
        lines.append('')

if sys_c:
    if user_c:
        lines += ['---', '']
    # Close-Reminder hervorheben — sonst uebersieht Boss den Call-to-Action.
    close_reminders = [c for c in sys_c if '[orch-close-reminder]' in (c.get('content') or '')]
    other_sys = [c for c in sys_c if '[orch-close-reminder]' not in (c.get('content') or '')]

    if close_reminders:
        lines += [
            '# 🔴 CLOSE-REMINDER — SOFORT abschliessen',
            '',
            'Eines deiner Parent-Tasks wartet auf `mc done`. Die Phase-Approval ist bereits erledigt,',
            'aber der Parent haengt weil du ihn noch nicht formell abgeschlossen hast.',
            '',
            '**Mach das jetzt — in dieser Reihenfolge:**',
            '1. `mc telegram "<Final-Report>"` fuer den Parent (wenn report_back_required=true)',
            '2. `mc done <PARENT-TASK-ID>` — **nutze genau die id aus dem Reminder unten**',
            '',
            '**Haeufiger Fehler:** NICHT die Phase-Approval-Subtask erneut patchen — die ist schon done.',
            'Die Parent-ID steht unten im Reminder (id: <xxx>). `mc done <xxx>` ist die Aktion.',
            '',
            '**Keine Wartezeit:** wenn du 2 Reminder ignorierst, schliesst das Backend den Parent automatisch',
            'auf review und benachrichtigt den Operator. Das willst du nicht — mach es selbst sauber.',
            '',
        ]
        for c in close_reminders:
            lines.append(f"## 🔴 Close-Reminder: {c['task_title']}  (**PARENT-id: {c['task_id']}**)")
            lines.append(f"- Zeit: {c['created_at']}")
            lines.append('- Inhalt:')
            for line in c['content'].splitlines():
                lines.append(f'  > {line}')
            lines.append('')
        if other_sys:
            lines += ['---', '']

    if other_sys:
        lines += [
            '# System-Events auf deinen aktiven Tasks',
            '',
            'Automatische Events (kein User-Input). Reagiere faktenbasiert:',
            '- subtask_completed: Subtask ist fertig. Pruefe Deliverables (GET /deliverables auf Root-Task-ID!), entscheide ob Parent-Task auf review kann.',
            '- resolution: Agent hat Task abgeschlossen.',
            '- blocker: Task blockiert. Pruefe Impact + Entscheidung.',
            '',
            '**WICHTIG — State vor Block pruefen:** Bevor du einen Task auf blocked setzt, MUSS du den aktuellen Deliverable/Task-Zustand via GET frisch abfragen. Alte Checkpoints koennten ueberholt sein.',
            '',
        ]
        for c in other_sys:
            ct = c.get('comment_type', 'system')
            lines.append(f"## [{ct}] {c['task_title']}  (id: {c['task_id']})")
            lines.append(f"- Zeit: {c['created_at']}")
            lines.append('- Inhalt:')
            for line in c['content'].splitlines():
                lines.append(f'  > {line}')
            lines.append('')

lines.append('**Aktion:** Arbeite am relevanten Task weiter. Antwort-Kommentar nur wenn inhaltlich noetig.')
with open('/tmp/boss_host_new_comments_prompt.txt', 'w') as f:
    f.write('\n'.join(lines))
PYEOF
    unset MC_POLL_RESPONSE

    paste_and_submit /tmp/boss_host_new_comments_prompt.txt
}

log "Boss-Host poll.sh gestartet. Polle $MC_API_URL alle ${POLL_INTERVAL}s..."

# Startup-Recovery Flag: siehe docker/mc-agent-base/poll.sh fuer Details.
FIRST_POLL=true

while true; do
    NOW=$(date +%s)
    if [ $((NOW - LAST_HEARTBEAT)) -ge $HEARTBEAT_INTERVAL ]; then
        heartbeat "idle"
        LAST_HEARTBEAT=$NOW
        log "heartbeat sent (idle)"
    fi

    RESPONSE=$(poll)
    STATE=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('state','unknown'))" 2>/dev/null || echo "error")

    # Startup-Recovery: Host-Restart oder poll.sh-Crash waehrend aktivem Task.
    # Aktiv-Polling via inline turn-state-Logik bis Claude einen stabilen State
    # zeigt — max 30s, alle 2s pruefen. Backend rate-limited auf 1x/60s.
    #
    # Bug 2026-04-28: Vorheriger PANE_SIGNALS-Check nutzte grep -E '●|...' —
    # das '●' allein war ein False-Positive: Claude Code TUI zeigt '●' waehrend
    # der System-Prompt-Initialisierung. Kanonische Working-Marker brauchen
    # Space + konkreten Tool-Namen ('● Bash\(', '● Read\(' etc.).
    # Inline-Variante weil boss-host poll.sh turn-state.sh nicht sourced.
    if $FIRST_POLL && [ "$STATE" = "working" ]; then
        STARTUP_RESOLVED=false
        STARTUP_WAIT=0
        STARTUP_MAX_WAIT=30
        TMUX_SOCK="$HOME/.mc/agents/boss-host/.tmux.sock"

        while [ "$STARTUP_WAIT" -lt "$STARTUP_MAX_WAIT" ]; do
            sleep 2
            STARTUP_WAIT=$((STARTUP_WAIT + 2))
            STARTUP_PANE=$(tmux -S "$TMUX_SOCK" capture-pane -t "${SESSION_NAME}:0" -p -S -50 2>/dev/null || echo "")

            if echo "$STARTUP_PANE" | grep -qE 'API Error: fetch failed|API Error: Connection error|API Error: 5[0-9]{2}'; then
                STARTUP_TURN_STATE="crashed"
            elif echo "$STARTUP_PANE" | grep -qE 'Cogitated|✻|Crunched|Spelunking|esc to interrupt|● Bash\(|● Read\(|● Write\(|● Edit\('; then
                STARTUP_TURN_STATE="working"
            elif echo "$STARTUP_PANE" | tail -5 | grep -qE '^❯ *$|bypass permissions'; then
                STARTUP_TURN_STATE="idle"
            else
                STARTUP_TURN_STATE="unknown"
            fi

            case "$STARTUP_TURN_STATE" in
                working)
                    log "Startup-Skip recovery: turn_state=working nach ${STARTUP_WAIT}s (Claude arbeitet)"
                    STARTUP_RESOLVED=true
                    break
                    ;;
                idle|crashed)
                    log "Startup-Recovery: turn_state=${STARTUP_TURN_STATE} nach ${STARTUP_WAIT}s → re-dispatch"
                    recover_task
                    STARTUP_RESOLVED=true
                    break
                    ;;
                *) ;;  # unknown → Claude noch am laden, weiter warten
            esac
        done

        if ! $STARTUP_RESOLVED; then
            log "Startup-Recovery: timeout nach ${STARTUP_MAX_WAIT}s (turn_state=unknown) → re-dispatch"
            recover_task
        fi

        FIRST_POLL=false
        sleep "$POLL_INTERVAL"
        continue
    fi
    FIRST_POLL=false

    case "$STATE" in
        new_task)  run_task "$RESPONSE" ;;
        cancelled)
            TASK_ID=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null)
            [ -n "$TASK_ID" ] && cancel_task "$TASK_ID"
            ;;
        stopped)
            TASK_ID=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null)
            [ -n "$TASK_ID" ] && stop_task_session "$TASK_ID"
            ;;
        idle|working) ;;  # nur Comments zustellen (unten)
        error) ;;
    esac

    # Kommentare/System-Events auch bei new_task zustellen (review_rejection
    # schickt Task-Prompt UND haengt den Review-Kommentar des Operators im Poll-Response an).
    if [ "$STATE" != "error" ] && [ "$STATE" != "cancelled" ] && [ "$STATE" != "stopped" ]; then
        deliver_comments "$RESPONSE"
    fi

    sleep "$POLL_INTERVAL"
done
