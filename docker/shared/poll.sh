#!/bin/bash
# poll.sh — Agent HTTP-Poll Loop (läuft in Window 1)
# Pollt MC-Backend für nächsten Task, sendet Prompt via tmux paste-buffer an
# interaktiven openclaude in Window 0. User sieht CLI-Header + AI-Antwort live.
#
# Single endpoint: GET /agent/me/poll
# Returns one of: cancelled, working, new_task, idle

set -euo pipefail
: "${MC_API_URL:?MC_API_URL is not set — container misconfigured}"
: "${MC_TOKEN:?MC_TOKEN is not set — container misconfigured}"

SESSION_NAME="${AGENT_NAME:-agent}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
HEARTBEAT_INTERVAL=30
LAST_HEARTBEAT=0

# Turn-State Tracking (siehe docker/mc-agent-base/lib/turn-state.sh)
# shellcheck source=lib/turn-state.sh
source /home/agent/lib/turn-state.sh
# UI-Runtime-Detection (siehe docker/mc-agent-base/lib/ui-detect.sh) —
# Bug 14: openclaude bricht bei bracketed-paste-end-marker, claude-cli braucht ihn.
# shellcheck source=lib/ui-detect.sh
source /home/agent/lib/ui-detect.sh
# Cached runtime-UI of tmux Window 0. Set by wait_for_clean_prompt() on every
# successful detect, used by paste_and_submit() to decide whether to send the
# `\e[201~` end-marker. Empty until first detection — paste_and_submit treats
# empty as "send marker" (safe default for claude-cli majority).
PANE_UI_DETECTED=""
CURRENT_TASK_ID=""
CURRENT_BOARD_ID=""
LAST_TURN_STATE=""
LAST_ACTIVITY_HASH=""
# Task-ID des zuletzt via run_task() an openclaude gesendeten Tasks. Wird zum
# Erkennen von Task-Wechseln genutzt — bei Wechsel feuert /clear, bei
# Re-Dispatch desselben Tasks (Review-Rejection, Recovery) bleibt der Kontext.
LAST_DISPATCHED_TASK_ID=""
LAST_DISPATCHED_ATTEMPT_ID=""   # Dedup-Guard via dispatch_attempt_id (sicherer als task_id)
# Task-ID fuer letzten gehandhabten Stop (Idempotenz-Guard gegen repeated /clear).
LAST_STOPPED_TASK_ID=""
# Epoch seconds of the most recent stop_task_session() — used by run_task() to
# detect "operator stop+restart of the same task within a short window" and
# skip a redundant /clear (the stop already cleared). See Bug 2026-05-12
# ("/clear/clear" injected mid-paste during operator blocked->in_progress flip).
LAST_STOPPED_AT_EPOCH=0
# Window inside which a re-dispatch of the just-stopped task is treated as a
# resume rather than a fresh task switch (no second /clear). 60s covers the
# typical operator round-trip (notice failure -> set blocked -> set in_progress)
# while still triggering a normal /clear for "stopped task that comes back
# minutes later".
QUICK_RESTART_WINDOW_SEC="${QUICK_RESTART_WINDOW_SEC:-60}"
STAGNATION_COUNT=0
# Bug 6 (2026-05-13): Threshold von 12 (60s) auf 36 (180s) angehoben.
# 60s ohne Screen-Aenderung ist fuer komplexe LLM-Reasonings (Cogitated/
# Crunched Phasen, lange Tool-Calls) zu aggressiv. Sparky bekam waehrend
# einem 12-Min-Cook einen false-positive Blocker. ENV-tunable damit der Operator
# fuer einzelne Agents (Researcher, Sparky) anders setzen kann.
STAGNATION_THRESHOLD="${STAGNATION_THRESHOLD:-36}"   # 36 * POLL_INTERVAL (5s) = 180s
# Bug 6 idempotency: dedup-Marker damit poll.sh nicht jeden Zyklus erneut
# einen Blocker postet sobald die Threshold erreicht ist. Wird beim Wechsel
# zu einer neuen CURRENT_TASK_ID resettet.
LAST_BLOCKED_TASK_ID=""
# Lockfile: poll.sh schreibt dieses File sobald ein Task aktiv ist.
# recycler.sh prueft es vor idle-Kill — verhindert Recycle mitten im Task.
# Stale-Lock-Schutz: recycler prueft ob poll.sh noch laeuft (pgrep).
TASK_LOCK_FILE="/home/agent/.task-active.lock"


log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [$SESSION_NAME] $*"
}

# tmux_submit TARGET — send a Carriage Return (\r, 0x0d) to TARGET.
#
# Bug 2026-05-15 (live qwen incident): `tmux send-keys ... Enter` translates
# to LF (\n, 0x0a). claude-cli accepts LF as submit (lenient line-discipline
# handler), but openclaude runs the pty in raw mode and only recognises CR
# (\r, 0x0d) as Enter — LF gets buffered as a literal newline character
# inside the input field.
#
# Empirically reproduced in Sparky (openclaude/qwen) with
# `tmux send-keys "/clear" Enter`:
#   1. `/clear\n` bytes arrive at openclaude pty
#   2. openclaude accumulates `/clear` in input box, ignores `\n`
#   3. Later `tmux paste-buffer` writes brief content INTO same input box
#   4. Result: `/clear<BRIEF>` submitted as one message
#
# With `tmux send-keys -H 0d` (raw CR byte) openclaude submits cleanly:
#   - `/clear` → executes as slash-command, clears context
#   - paste-buffer brief → submits as standalone message
#
# claude-cli is unaffected (tested with /help + paste-buffer + brief):
# both LF and CR work, so CR is the universal safe choice.
tmux_submit() {
    tmux send-keys -t "$1" -H 0d
}

# paste_and_submit FILE — laedt FILE in tmux paste-buffer, pastet in Window 0,
# schickt expliziten Bracketed-Paste-End-Marker + Enter zum Submit.
#
# Hintergrund (Bug 2026-04-23): tmux paste-buffer aktiviert Bracketed-Paste-Mode
# (\e[200~ ... \e[201~). Bei manchen Konstellationen kommt der \e[201~ End-
# Marker nicht zuverlaessig durch — claude/openclaude bleibt im Paste-Mode
# haengen, der nachfolgende Enter wird als Newline IM Paste-Buffer interpretiert
# (statt Submit). Folge: Prompt sitzt im Input-Feld, Agent arbeitet nicht,
# DB sagt "in_progress" (poll-claim hat ack gesetzt) — System haengt.
#
# Fix: nach paste-buffer den End-Marker EXPLIZIT als Hex senden (-H ...),
# DANN Enter. Verifiziert auf Boss-Host am 2026-04-23.
#
# Defense (Bug 2026-05-12): vor dem paste warten bis openclaude einen sauberen
# Prompt zeigt. Sonst landen pending Keystrokes aus dem pty-Buffer (z.B. ein
# `/clear` aus einem kurz vorher abgesetzten stop_task_session) IN die paste-
# Boundary → erscheinen als Text im Prompt statt als Slash-Command. Wir polle
# das pane-Capture auf openclaude's input-box Border-Glyph (╭ / ╰). Wenn nach
# READY_TIMEOUT_SEC kein clean prompt erkannt wird: paste trotzdem (fail-open
# wie bisher), aber WARNING loggen damit der Operator/wir das in den Logs sehen.
READY_TIMEOUT_SEC="${READY_TIMEOUT_SEC:-5}"
READY_POLL_INTERVAL_SEC="${READY_POLL_INTERVAL_SEC:-0.2}"

wait_for_clean_prompt() {
    # Erfolgsfall: gibt 0 zurueck wenn pane einen clean-prompt zeigt.
    # Fehlerfall: gibt 1 zurueck nach Timeout — caller entscheidet ob trotzdem
    # pasten oder retry.
    #
    # Bug 12+13 fix (2026-05-13): toleriert beide Runtime-UIs via detect_pane_ui.
    # - claude-cli: input box mit `╭─` / `╰─` glyphs
    # - openclaude: horizontal `────` lines mit `❯` prompt
    #
    # Bug 14 fix (2026-05-13): bei jedem positiven Match wird die globale
    # PANE_UI_DETECTED gesetzt, damit paste_and_submit weiss ob es den
    # Bracketed-Paste-End-Marker schicken darf (claude) oder nicht (openclaude).
    local deadline
    deadline=$(( $(date +%s) + READY_TIMEOUT_SEC ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        local ui
        if ui=$(detect_pane_ui "${SESSION_NAME}:0"); then
            PANE_UI_DETECTED="$ui"
            return 0
        fi
        sleep "$READY_POLL_INTERVAL_SEC"
    done
    return 1
}

# Bug 10 (2026-05-13): fail-open des paste-Schritts war silent — bei Race
# zwischen paste-buffer und openclaude pty landete die Eingabe gelegentlich
# NICHT im Pane (claude blieb idle, Task stuck, kein Hinweis im Log).
#
# Tunables fuer Post-Paste-Verify + Retry:
#   PASTE_VERIFY_DELAY_SEC — Wartezeit nach Enter bevor wir capture-pane probieren
#   PASTE_RETRY_DELAY_SEC  — Zusaetzliche Wartezeit zwischen Versuch 1 und 2
#   PASTE_FINGERPRINT_LEN  — Zeichen der ersten nicht-leeren Zeile als Fingerprint
#   PASTE_MAX_ATTEMPTS     — wie oft retry (default 2 = 1 Original + 1 Retry)
PASTE_VERIFY_DELAY_SEC="${PASTE_VERIFY_DELAY_SEC:-2}"
PASTE_RETRY_DELAY_SEC="${PASTE_RETRY_DELAY_SEC:-1}"
PASTE_FINGERPRINT_LEN="${PASTE_FINGERPRINT_LEN:-40}"
PASTE_MAX_ATTEMPTS="${PASTE_MAX_ATTEMPTS:-2}"

# verify_paste_landed wird aus lib/paste-verify.sh geladen (sourceable fuer Tests).
# shellcheck source=lib/paste-verify.sh
source /home/agent/lib/paste-verify.sh

paste_and_submit() {
    local file="$1"
    if ! wait_for_clean_prompt; then
        log "WARNING: paste_and_submit ohne clean-prompt nach ${READY_TIMEOUT_SEC}s — paste trotzdem (fail-open). Pending keystrokes im pty-Buffer koennen mit pasten."
        # Bug 14: letzte Chance auf UI-Detection vor dem paste, damit wir den
        # End-Marker korrekt routen koennen auch wenn wait_for_clean_prompt
        # nicht zum sauberen Prompt durchkam.
        if [ -z "$PANE_UI_DETECTED" ]; then
            local ui_probe
            if ui_probe=$(detect_pane_ui "${SESSION_NAME}:0"); then
                PANE_UI_DETECTED="$ui_probe"
            fi
        fi
    fi
    local attempt=1
    while [ "$attempt" -le "$PASTE_MAX_ATTEMPTS" ]; do
        tmux load-buffer "$file"
        tmux paste-buffer -t "${SESSION_NAME}:0"
        sleep 0.3
        # Bug 14 (2026-05-13): bracketed-paste end-marker `\e[201~` ist
        # runtime-spezifisch. claude-cli BRAUCHT ihn (sonst bleibt der pty im
        # paste-mode und der Enter wird als Newline interpretiert). openclaude
        # BRECHT bei dem Marker (zeigt ihn als Literal-Text + verschluckt das
        # Submit). Skip wenn openclaude erkannt. Bei unbekannter UI: senden
        # (safe default — claude-cli ist die Mehrheit der Agents).
        if [ "$PANE_UI_DETECTED" != "openclaude" ]; then
            tmux send-keys -t "${SESSION_NAME}:0" -H 1b 5b 32 30 31 7e
            sleep 0.2
        fi
        tmux_submit "${SESSION_NAME}:0"
        # Post-Paste-Verify (Bug 10 fix). Wir warten kurz und prueffen ob die
        # Eingabe in den Pane gerendert wurde. Wenn nicht: retry.
        sleep "$PASTE_VERIFY_DELAY_SEC"
        if verify_paste_landed "$file"; then
            if [ "$attempt" -gt 1 ]; then
                log "paste_and_submit erfolgreich auf Versuch ${attempt}."
            fi
            return 0
        fi
        if [ "$attempt" -lt "$PASTE_MAX_ATTEMPTS" ]; then
            log "WARNING: paste_and_submit Versuch ${attempt}: Fingerprint nicht im Pane sichtbar — Retry in ${PASTE_RETRY_DELAY_SEC}s."
            sleep "$PASTE_RETRY_DELAY_SEC"
        fi
        attempt=$((attempt + 1))
    done
    log "ERROR: paste_and_submit FAILED nach ${PASTE_MAX_ATTEMPTS} Versuchen — Eingabe ist NICHT im claude-Pane gelandet. Task stuck. Manueller Eingriff (Status-Flip oder tmux send-keys) noetig."
    return 1
}

heartbeat() {
    local status="${1:-idle}"
    # CTX-01 (Phase 6): scrape ctx% from claude statusline in tmux Window 0.
    # Strategy 1: pane_title (claude writes status-right here in newer versions).
    # Strategy 2: capture-pane tail (older versions write to bottom status bar).
    # On scrape failure: omit context_pct entirely — backend handler treats None
    # as "not reported this cycle" and preserves previous context_tokens value.
    local ctx_pct=""
    ctx_pct=$(tmux display-message -t "${SESSION_NAME}:0" -p "#{pane_title}" 2>/dev/null \
        | grep -oE 'ctx[: ]*[0-9]+' | grep -oE '[0-9]+' | head -1 || true)
    if [ -z "$ctx_pct" ]; then
        ctx_pct=$(tmux capture-pane -t "${SESSION_NAME}:0" -p 2>/dev/null \
            | tail -10 | grep -oE 'ctx[: ]*[0-9]+%?' | grep -oE '[0-9]+' | tail -1 || true)
    fi
    # Sanitize: must be 0-100 integer (defense-in-depth even though backend
    # validates with Field(ge=0, le=100); avoid sending garbage).
    if ! [[ "$ctx_pct" =~ ^[0-9]+$ ]] || [ "$ctx_pct" -gt 100 ] 2>/dev/null; then
        ctx_pct=""
    fi
    # Pass scraped value via env-var (CTX_PCT) instead of f-string interpolation
    # — defense against shell-metachar injection if pane_title is ever attacker-
    # controlled (T-06-03-01).
    CTX_PCT="$ctx_pct" STATUS="$status" python3 -c "
import json, urllib.request, os, sys
payload = {'status': os.environ.get('STATUS', 'idle')}
ctx = os.environ.get('CTX_PCT', '').strip()
if ctx.isdigit():
    val = int(ctx)
    if 0 <= val <= 100:
        payload['context_pct'] = float(val)
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

# Recovery (ADR-024): read-only Prompt-Neu-Lieferung nach Container-Restart.
# Neuer Weg GET /me/active-task-recovery statt alter POST /recover-task
# (der mutierte den Status → Dispatch-Loop-Risiko). Response-Form ist
# identisch zu /me/poll new_task sodass run_task() direkt wiederverwendet wird.
#
# Bug 15 (2026-05-13): vor diesem fix returnte run_task() bei `task.status=
# in_progress` early aus dem "Session-Restart-Ausnahme"-Block (Commit
# 35dc7b16, 2026-05-03), ohne paste_and_submit zu rufen. Effekt: bei jedem
# Container-Recreate sah Sparky/FreeCode den prompt nie — pane blieb leer
# am ❯/╭─ prompt. recover_task() setzt jetzt IS_RECOVERY_DISPATCH=true
# damit run_task() den /clear ueberspringt ABER trotzdem pasted.
recover_task() {
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
        log "Startup-Recovery: aktiver Task $tid — Prompt wird re-dispatched (read-only, kein Status-Change)"
        IS_RECOVERY_DISPATCH=true run_task "$response"
    else
        log "Startup-Recovery: kein aktiver Task — Agent ist frei"
    fi
}

run_task() {
    local response_json="$1"
    local task_id
    local board_id
    local attempt_id
    task_id=$(echo "$response_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['task']['id'])")
    board_id=$(echo "$response_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['task'].get('board_id') or '')" 2>/dev/null || echo "")
    attempt_id=$(echo "$response_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['task'].get('dispatch_attempt_id') or '')" 2>/dev/null || echo "")

    log "Task erhalten: $task_id"

    # Workstream A fix — expose task context to the `mc` CLI running inside
    # openclaude. Same mechanism as mc-claude-agent: tmux session env so new
    # shells inherit, plus a /tmp file as a belt-and-braces fallback.
    tmux set-environment -t "$SESSION_NAME" TASK_ID "$task_id" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" BOARD_ID "$board_id" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" X_DISPATCH_ATTEMPT_ID "$attempt_id" 2>/dev/null || true
    cat > /tmp/mc-context.env <<EOF
TASK_ID=$task_id
BOARD_ID=$board_id
X_DISPATCH_ATTEMPT_ID=$attempt_id
EOF
    chmod 644 /tmp/mc-context.env 2>/dev/null || true

    # Prompt in Datei schreiben
    echo "$response_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
with open('/tmp/current_task_prompt.txt', 'w') as f:
    f.write(data['task']['prompt'])
"

    heartbeat "working"

    # Phase 3 — Marker for the recycler's idle-detection (ADR-024).
    # Updates mtime so recycler.sh sees activity = now. File-only signal,
    # no syscall to backend. First-boot is handled by recycler.sh itself.
    touch /home/agent/.claude/last-task.marker 2>/dev/null || true
    # Lockfile: recycler.sh prueft dieses File vor idle-Kill.
    echo "$task_id" > "$TASK_LOCK_FILE" 2>/dev/null || true

    # /clear bei echtem Task-Wechsel, nicht bei Re-Dispatch des gleichen Tasks.
    #
    # Warum ueberhaupt clearen: openclaude haelt die komplette Conversation-
    # History in seiner Session, auch ueber abgeschlossene Tasks hinweg. Ohne
    # Reset summiert sich jeder Dispatch-Prompt mit der gesamten alten Historie
    # im Request-Payload. Bei Cloud-LLMs (ollama.com / glm-5.1:cloud heute
    # beobachtet) fuehrt das nach 1-2 Tasks zu "API Error: fetch failed" —
    # Streaming-Response bricht ab, Model haengt im Sauteed/Moseying/Churned
    # state.
    #
    # Warum NICHT immer clearen: bei Re-Dispatch (Review-Rejection, Recovery)
    # kommt der gleiche Task nochmal zurueck — der Agent hat Zwischenstand,
    # Lessons, Datei-Reads. Wegwerfen waere Verlust. poll.sh merkt sich die
    # letzte gelieferte Task-ID und cleart nur bei Wechsel.
    #
    # Session-Restart-Ausnahme: nach manuellem Restart (Sessions-Seite) ist
    # LAST_DISPATCHED_TASK_ID leer — der aktive Task sieht wie ein "neuer" aus.
    # Wenn task.status bereits "in_progress" ist, ist der Agent mitten in der
    # Arbeit. /clear wuerde die laufende Session zerstoeren. Loesung: Status
    # aus der Response lesen und bei in_progress immer ueberspringen, egal ob
    # LAST_DISPATCHED_TASK_ID leer ist.
    local current_task_status=""
    current_task_status=$(echo "$response_json" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin)['task'].get('status', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

    if [ "$task_id" != "$LAST_DISPATCHED_TASK_ID" ]; then
        if [ "$current_task_status" = "in_progress" ]; then
            # Task laeuft bereits (Session-Restart oder Recovery-Pfad) — context erhalten.
            log "Task $task_id: bereits in_progress (Session-Restart oder Recovery) — /clear UEBERSPRUNGEN"
            LAST_DISPATCHED_TASK_ID="$task_id"
            # Bug 15 (2026-05-13): vor diesem fix war hier ein hartes `return`,
            # das bei jedem Container-Recreate / Recovery den paste-Step
            # uebersprungen hat — Sparky bekam den prompt nie. Jetzt: nur bei
            # echtem Session-Restart (poll.sh laeuft schon, der Operator restartet
            # claude-Pane manuell, Agent cookt evtl. weiter) den paste skippen.
            # Bei Recovery (Container-Recreate, claude-Prozess ist neu, pane
            # leer) den paste durchfuehren — sonst weiss der Agent nichts vom
            # aktiven Task.
            if [ "${IS_RECOVERY_DISPATCH:-false}" != "true" ]; then
                LAST_DISPATCHED_ATTEMPT_ID="$attempt_id"
                return
            fi
            # IS_RECOVERY_DISPATCH=true → fall through to paste path below.
            # LAST_DISPATCHED_ATTEMPT_ID wird erst beim erfolgreichen paste
            # gesetzt (siehe nach der attempt-dedup-Sektion).
        fi

        # Quick stop+restart of THIS task — the stop already did ESC + /clear,
        # sending a second /clear now is redundant AND risks landing in the
        # bracketed-paste boundary of the upcoming dispatch (Bug 2026-05-12:
        # operator blocked->in_progress within 17s produced "/clear/clear"
        # inside the prompt because the second /clear was queued in the pty
        # while paste-buffer was still flushing).
        local now_epoch
        now_epoch=$(date +%s)
        local time_since_stop=$(( now_epoch - LAST_STOPPED_AT_EPOCH ))
        if [ "$task_id" = "${LAST_STOPPED_TASK_ID:-}" ] \
           && [ "$LAST_STOPPED_AT_EPOCH" -gt 0 ] \
           && [ "$time_since_stop" -lt "$QUICK_RESTART_WINDOW_SEC" ]; then
            log "Task $task_id: stop+restart within ${time_since_stop}s — /clear UEBERSPRUNGEN (session already cleared by stop_task_session)"
        else
            # Defense gegen Context-Loss: wenn der VORHERIGE Task noch in_progress ist,
            # wuerde /clear die laufende Arbeit zerstoeren.
            local prev_status=""
            if [ -n "$LAST_DISPATCHED_TASK_ID" ] && [ -n "$board_id" ]; then
                prev_status=$(curl -sf \
                    -H "Authorization: Bearer $MC_TOKEN" \
                    "$MC_API_URL/api/v1/agent/boards/$board_id/tasks/$LAST_DISPATCHED_TASK_ID/detail" \
                    --max-time 5 2>/dev/null \
                    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
            fi

            if [ "$prev_status" = "in_progress" ]; then
                log "WARNING: Task $task_id kam, aber vorheriger Task $LAST_DISPATCHED_TASK_ID ist noch in_progress — /clear UEBERSPRUNGEN (Context-Preservation, siehe dispatch _skip_busy)"
            else
                tmux send-keys -t "${SESSION_NAME}:0" "/clear"
                tmux_submit "${SESSION_NAME}:0"
                sleep 2
                log "Task $task_id: context cleared (new task, previous: ${LAST_DISPATCHED_TASK_ID:-none}, prev_status=${prev_status:-unknown})"
            fi
        fi

    else
        log "Task $task_id: re-dispatch, context kept"
    fi
    LAST_DISPATCHED_TASK_ID="$task_id"

    # Dedup via dispatch_attempt_id: gleiche attempt_id = derselbe Dispatch-Versuch,
    # noch kein ACK vom Agenten — nicht nochmal senden (verhindert Loop).
    # Neue attempt_id = echter Re-Dispatch (Review-Rejection, Unblocking) — senden.
    # Fallback: leere attempt_id deaktiviert Dedup (sicheres Fallback).
    if [ "$attempt_id" = "$LAST_DISPATCHED_ATTEMPT_ID" ] && [ -n "$attempt_id" ]; then
        log "Task $task_id: attempt $attempt_id bereits gesendet, warte auf ACK"
        return
    fi
    LAST_DISPATCHED_ATTEMPT_ID="$attempt_id"

    sleep 0.5
    # Bug 12 fix (2026-05-13): paste_and_submit kann mit return 1 fehlschlagen
    # (Bug 10 fix). Vorher: `set -euo pipefail` killte poll.sh komplett, der
    # entrypoint restartete den Loop, und race-condition entschied ob der
    # Task doch lief. Jetzt: Return-Code explicit handlen — bei Fehler nur
    # WARN-Log, kein poll.sh exit. Der Task bleibt assigned + in_progress,
    # claude meldet sich entweder selbst oder der Operator sieht den Task stuck.
    if ! paste_and_submit /tmp/current_task_prompt.txt; then
        log "WARNING: paste_and_submit returnte non-zero fuer Task $task_id — claude koennte den Prompt verzoegert verarbeiten oder Task ist stuck. Kein poll.sh exit, Task bleibt in_progress."
    else
        log "Task $task_id (attempt ${attempt_id:-unbekannt}) an claude gesendet (fire-and-forget)"
    fi

    # Turn-State Tracking initialisieren fuer crashed/stagnation Detection
    CURRENT_TASK_ID="$task_id"
    CURRENT_BOARD_ID="$board_id"
    LAST_TURN_STATE="working"
    STAGNATION_COUNT=0
    LAST_ACTIVITY_HASH=$(turn_activity_hash "$SESSION_NAME")
    # Reset stop-dedup Marker — erlaubt spaeteren Stop dieses Tasks neu clearen.
    LAST_STOPPED_TASK_ID=""
    # Reset Bug-6 dedup-Marker (LAST_BLOCKED_TASK_ID) sobald wir einen neuen
    # Task starten — sonst werden false-positive Blocker im naechsten Task
    # auch nicht mehr gemeldet wenn der WIRKLICH stagnations-blocked ist.
    LAST_BLOCKED_TASK_ID=""
    # Kein Warten auf Completion — claude meldet sich selbst via MC API.
}

cancel_task() {
    # Idempotent via $LAST_CANCELLED_TASK_ID Marker — Backend returnt state=cancelled
    # solange Task failed bleibt, aber wir wollen ESC nur EINMAL senden (sonst
    # pruegelt jeder 5s-Poll ein ESC in die Session). Analog zu stop_task_session().
    local task_id="$1"
    if [ "$task_id" = "${LAST_CANCELLED_TASK_ID:-}" ]; then
        return 0
    fi
    log "Task $task_id extern auf 'failed' gesetzt — sende ESC an claude"
    tmux send-keys -t "${SESSION_NAME}:0" Escape
    LAST_CANCELLED_TASK_ID="$task_id"
    CURRENT_TASK_ID=""
    CURRENT_BOARD_ID=""
    LAST_TURN_STATE=""
    STAGNATION_COUNT=0
    rm -f "$TASK_LOCK_FILE" 2>/dev/null || true
    heartbeat "idle"
}

stop_task_session() {
    # Manual stop vom Operator (run_control=stopped). Idempotent via $LAST_STOPPED_TASK_ID
    # Marker — Backend returnt `state=stopped` solange run_control=stopped, aber
    # wir wollen /clear nur EINMAL senden (sonst prügelt jeder 5s-Poll ein /clear
    # in die Session).
    local task_id="$1"
    if [ "$task_id" = "${LAST_STOPPED_TASK_ID:-}" ]; then
        return 0
    fi
    log "Task $task_id vom Operator gestoppt — ESC + /clear + context reset"
    tmux send-keys -t "${SESSION_NAME}:0" Escape 2>/dev/null || true
    sleep 0.5
    tmux send-keys -t "${SESSION_NAME}:0" "/clear" 2>/dev/null || true
    tmux_submit "${SESSION_NAME}:0" 2>/dev/null || true
    : > /tmp/mc-context.env
    tmux set-environment -t "$SESSION_NAME" TASK_ID "" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" BOARD_ID "" 2>/dev/null || true
    tmux set-environment -t "$SESSION_NAME" X_DISPATCH_ATTEMPT_ID "" 2>/dev/null || true
    CURRENT_TASK_ID=""
    CURRENT_BOARD_ID=""
    LAST_TURN_STATE=""
    STAGNATION_COUNT=0
    LAST_DISPATCHED_TASK_ID=""
    LAST_DISPATCHED_ATTEMPT_ID=""
    LAST_STOPPED_TASK_ID="$task_id"
    LAST_STOPPED_AT_EPOCH=$(date +%s)
    rm -f "$TASK_LOCK_FILE" 2>/dev/null || true
    heartbeat "idle"
}

# Task bei Backend als blockiert melden + Blocker-Kommentar posten.
# Wird aufgerufen wenn poll.sh einen crashed/stagnated Turn erkennt.
report_blocker() {
    local task_id="$1"
    local reason="$2"
    local error_detail="${3:-no error detail captured}"
    log "Blocker erkannt auf Task $task_id: $reason"

    # Blocker-Kommentar via Python (sichere JSON-Encoding mit Newlines/Quotes)
    if [ -n "$CURRENT_BOARD_ID" ]; then
        POLL_REASON="$reason" POLL_ERROR="$error_detail" \
        POLL_URL="$MC_API_URL/api/v1/agent/boards/$CURRENT_BOARD_ID/tasks/$task_id/comments" \
        POLL_TOKEN="$MC_TOKEN" python3 -c "
import json, os, urllib.request
reason = os.environ['POLL_REASON']
err = os.environ['POLL_ERROR']
body = {
    'content': f'**Automatisch erkannt (poll.sh turn-state):** {reason}\n\n\`\`\`\n{err}\n\`\`\`',
    'comment_type': 'blocker',
}
req = urllib.request.Request(
    os.environ['POLL_URL'],
    data=json.dumps(body).encode(),
    headers={'Authorization': 'Bearer ' + os.environ['POLL_TOKEN'], 'Content-Type': 'application/json'},
    method='POST',
)
try:
    urllib.request.urlopen(req, timeout=5)
except Exception as e:
    print(f'blocker-comment failed: {e}', file=__import__('sys').stderr)
" 2>/dev/null || true
    fi

    # Task-Status auf blocked (PATCH).
    # WICHTIG: agent_task_status.py:1599-1608 verlangt seit Phase 28+
    # bei status=blocked pflichtmaessig blocker_type + blocker_question
    # (D-14 callback-wait Sonderfall greift hier nicht). Ohne diese
    # Felder → HTTP 422 + Task bleibt in_progress → Watchdog stale-loop
    # alle 60min (recovery_started Discord-spam). Daher senden wir hier
    # einen vollstaendigen Body mit blocker_type='technical_problem'.
    POLL_REASON="$reason" POLL_ERROR="$error_detail" \
    POLL_URL="$MC_API_URL/api/v1/agent/me/tasks/$task_id" \
    POLL_TOKEN="$MC_TOKEN" python3 -c "
import json, os, urllib.request
body = {
    'status': 'blocked',
    'blocker_type': 'technical_problem',
    'blocker_question': f'Agent stalled — poll.sh turn-state auto-detection: {os.environ[\"POLL_REASON\"]}',
    'blocker_description': os.environ['POLL_ERROR'][:300],
}
req = urllib.request.Request(
    os.environ['POLL_URL'],
    data=json.dumps(body).encode(),
    headers={'Authorization': 'Bearer ' + os.environ['POLL_TOKEN'], 'Content-Type': 'application/json'},
    method='PATCH',
)
try:
    urllib.request.urlopen(req, timeout=5)
except Exception as e:
    print(f'status-patch failed: {e}', file=__import__('sys').stderr)
" 2>/dev/null || true

    # Claude aus dem crashed/stalled state befreien
    tmux send-keys -t "${SESSION_NAME}:0" Escape 2>/dev/null || true

    CURRENT_TASK_ID=""
    CURRENT_BOARD_ID=""
    LAST_TURN_STATE=""
    STAGNATION_COUNT=0
    rm -f "$TASK_LOCK_FILE" 2>/dev/null || true
}

deliver_comments() {
    # Neue User-Kommentare aus poll-Response an claude uebergeben.
    # Keine Zustellung wenn leer. Vermeidet Spam durch eigene Agent-Kommentare
    # (Backend filtert die bereits raus via author_type).
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

    # Response ueber env-var uebergeben (Heredoc wuerde stdin blocken)
    export MC_POLL_RESPONSE="$response_json"
    python3 -c "
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
        lines.append(f\"## Task: {c['task_title']}  (id: {c['task_id']})\")
        lines.append(f\"- Zeit: {c['created_at']}\")
        lines.append('- Inhalt:')
        for line in c['content'].splitlines():
            lines.append(f'  > {line}')
        lines.append('')

if sys_c:
    if user_c:
        lines += ['---', '']
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
    for c in sys_c:
        ct = c.get('comment_type', 'system')
        lines.append(f\"## [{ct}] {c['task_title']}  (id: {c['task_id']})\")
        lines.append(f\"- Zeit: {c['created_at']}\")
        lines.append('- Inhalt:')
        for line in c['content'].splitlines():
            lines.append(f'  > {line}')
        lines.append('')

lines.append('**Aktion:** Arbeite am relevanten Task weiter. Antwort-Kommentar nur wenn inhaltlich noetig.')
with open('/tmp/new_comments_prompt.txt', 'w') as f:
    f.write('\n'.join(lines))
" || { log "deliver_comments: python parse failed — skipping"; unset MC_POLL_RESPONSE; return; }
    unset MC_POLL_RESPONSE

    # Bug 12 fix (2026-05-13): siehe run_task — kein set-e-kill bei Fehler.
    if ! paste_and_submit /tmp/new_comments_prompt.txt; then
        log "WARNING: paste_and_submit fuer new_comments returnte non-zero — Comments wurden ggf. nicht ans claude-Pane geliefert. Nicht fatal, poll-Loop laeuft weiter."
    fi
}

# Stale lock von vorherigem poll.sh-Run entfernen (z.B. nach SIGKILL wo trap nicht lief).
rm -f "$TASK_LOCK_FILE" 2>/dev/null || true
# Lockfile bei sauberem Exit raeumen. SIGKILL kann trap nicht abfangen —
# recycler.sh prueft deshalb zusaetzlich ob poll.sh noch laeuft (pgrep).
trap 'rm -f "$TASK_LOCK_FILE"' EXIT TERM INT

log "Gestartet. Polle $MC_API_URL alle ${POLL_INTERVAL}s..."

# Startup-Recovery Flag: wird nach dem ersten Poll-Zyklus auf false gesetzt.
# Wenn das Backend beim allerersten Poll bereits `state=working` meldet, hat
# der Container/Host einen Restart erlebt waehrend ein Task lief — die tmux-
# Session ist leer und claude hat den Prompt nicht mehr. Dann Recovery triggern.
FIRST_POLL=true

while true; do
    NOW=$(date +%s)

    # Heartbeat alle 30s. Bug 13 fix (2026-05-13): vorher wurde pauschal
    # "idle" gesendet — auch wenn claude im Cook ist. Backend musste mit
    # Bug 2 self-heal kompensieren (zu aggressiv, maskierte echte Inaktivitaet).
    # Jetzt: detect_turn_state aus lib/turn-state.sh liefert working|crashed|
    # idle|unknown. Wir mappen es auf "working" oder "idle" fuer den Heartbeat-
    # Payload — Backend uebernimmt das 1:1 in agent.status (Bug 2 refined).
    if [ $((NOW - LAST_HEARTBEAT)) -ge $HEARTBEAT_INTERVAL ]; then
        HB_STATE="idle"
        if [ -n "$CURRENT_TASK_ID" ]; then
            HB_TS=$(detect_turn_state "$SESSION_NAME")
            if [ "$HB_TS" = "working" ]; then
                HB_STATE="working"
            fi
        fi
        heartbeat "$HB_STATE"
        LAST_HEARTBEAT=$NOW
    fi

    # Turn-State Check — wenn wir einen aktiven Task haben, pruefen ob claude
    # noch arbeitet (vs. crashed / stagnated). Siehe lib/turn-state.sh.
    if [ -n "$CURRENT_TASK_ID" ]; then
        CUR_STATE=$(detect_turn_state "$SESSION_NAME")

        if [ "$CUR_STATE" = "crashed" ]; then
            # 3 CONSECUTIVE crashed-Detections als Minimum bevor Blocker fuert.
            # Einzelne Matches passieren oft wenn der Agent aus einem transient
            # Error self-correcting ist. Nur ein persistenter crashed-State
            # (3x in Folge, ~15s) ist ein echter Turn-Crash.
            CRASHED_COUNT=$((${CRASHED_COUNT:-0} + 1))
            if [ "$CRASHED_COUNT" -ge 3 ]; then
                ERR=$(extract_turn_error "$SESSION_NAME")
                report_blocker "$CURRENT_TASK_ID" "claude turn crashed (API/fetch error)" "$ERR"
                CRASHED_COUNT=0
            fi
        elif [ "$CUR_STATE" = "idle" ] && [ "$LAST_TURN_STATE" = "working" ]; then
            # War am arbeiten, jetzt idle ohne Completion-Signal → Stagnation pruefen.
            CUR_HASH=$(turn_activity_hash "$SESSION_NAME")
            if [ "$CUR_HASH" = "$LAST_ACTIVITY_HASH" ]; then
                STAGNATION_COUNT=$((STAGNATION_COUNT + 1))
            else
                STAGNATION_COUNT=0
                LAST_ACTIVITY_HASH="$CUR_HASH"
            fi
            if [ $STAGNATION_COUNT -ge $STAGNATION_THRESHOLD ]; then
                # Bug 6 fix (2026-05-13): final re-check vor Blocker-Post.
                # Lange LLM-Reasonings koennen das Pane fuer 3+ Min static
                # halten ohne dass claude wirklich aufgehoert hat. Wir warten
                # 2s und prueffen erneut detect_turn_state + activity_hash.
                # Wenn jetzt working ODER Hash-Aenderung → false-positive,
                # reset counter und kein Blocker.
                sleep 2
                RECHECK_STATE=$(detect_turn_state "$SESSION_NAME")
                RECHECK_HASH=$(turn_activity_hash "$SESSION_NAME")
                if [ "$RECHECK_STATE" = "working" ] || [ "$RECHECK_HASH" != "$LAST_ACTIVITY_HASH" ]; then
                    log "Stagnation re-check zeigt Aktivitaet (state=$RECHECK_STATE, hash-change=$([ "$RECHECK_HASH" != "$LAST_ACTIVITY_HASH" ] && echo yes || echo no)) — skip blocker, reset counter."
                    STAGNATION_COUNT=0
                    LAST_ACTIVITY_HASH="$RECHECK_HASH"
                elif [ "$LAST_BLOCKED_TASK_ID" = "$CURRENT_TASK_ID" ]; then
                    # Idempotency: schon einmal fuer diesen Task gemeldet,
                    # nicht spammen bis Operator/Agent reagiert.
                    :
                else
                    report_blocker "$CURRENT_TASK_ID" \
                        "Agent idle nach working, ${STAGNATION_THRESHOLD} Zyklen ohne Screen-Aenderung" \
                        "turn wurde beendet ohne status-completion (PATCH review/blocked/failed fehlt)"
                    LAST_BLOCKED_TASK_ID="$CURRENT_TASK_ID"
                fi
            fi
        elif [ "$CUR_STATE" = "working" ]; then
            LAST_TURN_STATE="working"
            STAGNATION_COUNT=0
            CRASHED_COUNT=0
            LAST_ACTIVITY_HASH=$(turn_activity_hash "$SESSION_NAME")
        else
            # idle/unknown → reset Crash-Counter.
            CRASHED_COUNT=0
        fi
    fi

    # Unified poll: ein Request, vier moegliche States
    RESPONSE=$(poll)
    STATE=$(echo "$RESPONSE" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('state', 'error'))
except Exception:
    print('error')
" 2>/dev/null || echo "error")

    # Startup-Recovery: Backend meldet aktiven Task, wir haben keinen lokalen
    # Kontext (Container-Restart oder poll.sh-Crash). Aktiv-Polling via
    # detect_turn_state() bis Claude einen stabilen State zeigt — max 30s,
    # alle 2s pruefen. Backend rate-limited zusaetzlich auf 1x/60s pro Task.
    #
    # Bug 2026-04-28: Vorheriger PANE_SIGNALS-Check nutzte grep -E '●|...' —
    # das '●' allein war ein False-Positive: Claude Code TUI zeigt '●' waehrend
    # der System-Prompt-Initialisierung (--append-system-prompt, 29KB SOUL.md).
    # detect_turn_state() matcht korrekt nur '● Bash\(', '● Read\(' etc.
    # (Space + konkreter Tool-Name) und ist die kanonische Erkennungsmethode.
    if $FIRST_POLL && [ "$STATE" = "working" ] && [ -z "$CURRENT_TASK_ID" ]; then
        STARTUP_RESOLVED=false
        STARTUP_WAIT=0
        STARTUP_MAX_WAIT=30

        while [ "$STARTUP_WAIT" -lt "$STARTUP_MAX_WAIT" ]; do
            sleep 2
            STARTUP_WAIT=$((STARTUP_WAIT + 2))
            STARTUP_TURN_STATE=$(detect_turn_state "$SESSION_NAME" 2>/dev/null || echo "unknown")

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
        new_task)
            run_task "$RESPONSE"
            LAST_HEARTBEAT=0
            ;;
        cancelled)
            CANCEL_TASK_ID=$(echo "$RESPONSE" | python3 -c "
import json, sys
print(json.load(sys.stdin).get('task_id', '?'))" 2>/dev/null || echo "?")
            cancel_task "$CANCEL_TASK_ID"
            ;;
        stopped)
            STOP_TASK_ID=$(echo "$RESPONSE" | python3 -c "
import json, sys
print(json.load(sys.stdin).get('task_id', '?'))" 2>/dev/null || echo "?")
            stop_task_session "$STOP_TASK_ID"
            ;;
        working)
            # Marker refreshen damit der Recycler weiss dass der Agent aktiv ist.
            # Ohne diesen Touch wuerde ein Task der laenger als RECYCLER_IDLE_MIN
            # (Default 15 Min) dauert durch den Recycler gekillt — Bug 2026-05-03.
            touch /home/agent/.claude/last-task.marker 2>/dev/null || true
            ;;
        idle)
            # Task-State unveraendert — nur Kommentare zustellen (unten).
            # Wenn CURRENT_TASK_ID gesetzt aber Backend meldet idle → Task wurde
            # abgeschlossen oder extern geloescht. Monitoring-State clearen damit
            # der Recycler nicht dauerhaft blockiert wird (Lock freigeben).
            if [ -n "$CURRENT_TASK_ID" ]; then
                log "Task $CURRENT_TASK_ID nicht mehr aktiv (state=idle) — resetting"
                CURRENT_TASK_ID=""
                CURRENT_BOARD_ID=""
                LAST_TURN_STATE=""
                STAGNATION_COUNT=0
                rm -f "$TASK_LOCK_FILE" 2>/dev/null || true
                heartbeat "idle"
            fi
            ;;
        error|*)
            # Backend nicht erreichbar oder unerwartete Antwort — nicht spammen.
            :
            ;;
    esac

    # Kommentare/System-Events zustellen — auch bei new_task (Re-Dispatch
    # nach review_rejection schickt den Task-Prompt UND haengt den Review-
    # Kommentar des Operators an). Nur error/cancelled/stopped ueberspringen.
    if [ "$STATE" != "error" ] && [ "$STATE" != "cancelled" ] && [ "$STATE" != "stopped" ]; then
        deliver_comments "$RESPONSE"
    fi

    sleep "$POLL_INTERVAL"
done
