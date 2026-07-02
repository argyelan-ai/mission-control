#!/bin/bash
# turn-state.sh — Klassifikation des claude/openclaude Turn-States via tmux pane capture.
# Wird von poll.sh gesourced. Exportiert: detect_turn_state(), extract_turn_error(),
# turn_activity_hash().
#
# Hintergrund (Plan 2026-04-17-agent-turn-state-observability.md):
# openclaude behandelt transient API-Errors (fetch failed, Connection error, 5xx)
# als Turn-Abort und kehrt zum interaktiven `❯`-Prompt zurueck. Der claude-Prozess
# lebt weiter, aber niemand meldet den Fehler ans Backend — Task bleibt fuer immer
# in_progress. Dieser Helper schliesst die Feedback-Loop: poll.sh klassifiziert
# den Turn-State und meldet crashed/stagnated runs als Blocker.

# Gibt einen von: working | crashed | idle | unknown
detect_turn_state() {
    local session="${1:?session name required}"
    local capture
    capture=$(tmux capture-pane -t "${session}:0" -p -S -50 2>/dev/null || echo "")

    if [ -z "$capture" ]; then
        echo "unknown"
        return
    fi

    # Crashed-Markers: NUR echte LLM/Network-Errors die den Turn abbrechen.
    # `Error: Exit code [^0]` war vorher hier — entfernt, weil das ein
    # normaler Bash-Tool-Fehler ist (mc deliverable 422, mkdir, etc). Claude
    # bekommt den Exit-Code als Tool-Output und self-corrected; das ist
    # KEIN Session-Crash.
    if echo "$capture" | grep -qE 'API Error: fetch failed|API Error: Connection error|API Error: 5[0-9]{2}'; then
        echo "crashed"
        return
    fi

    # Idle-Marker: ❯-Prompt in den letzten 5 Zeilen zeigt an dass Claude wartet.
    # Vor dem Working-Check geprueft — gescrollte Tool-Outputs (● Write, ✻ Churned)
    # in der Pane-History erzeugen sonst false-positives im Working-Check.
    if echo "$capture" | tail -5 | grep -qE '^❯ *$'; then
        echo "idle"
        return
    fi

    # Working-Markers — nur letzte 20 Zeilen (nicht volle History).
    # ✻ Churned = Claude hat aufgehoert (Timeout/Stagnation), kein aktiver Turn.
    local recent
    recent=$(echo "$capture" | tail -20)
    if echo "$recent" | grep -qE 'Cogitated|Crunched|Spelunking|esc to interrupt|● Bash\(|● Read\(|● Write\(|● Edit\('; then
        echo "working"
        return
    fi
    if echo "$recent" | grep -qE '✻' && ! echo "$recent" | grep -qE '✻ Churned'; then
        echo "working"
        return
    fi

    # Fallback Idle: bypass permissions sichtbar (Claude-TUI Statusleiste).
    if echo "$capture" | tail -5 | grep -qE 'bypass permissions'; then
        echo "idle"
        return
    fi

    echo "unknown"
}

# Extrahiert die letzte Error-Zeile aus dem Pane (max 200 chars). Fallback: leer.
extract_turn_error() {
    local session="${1:?session name required}"
    tmux capture-pane -t "${session}:0" -p -S -100 2>/dev/null \
        | grep -E 'API Error|Error: Exit code|fetch failed|Connection error' \
        | tail -1 \
        | cut -c1-200
}

# Hash der letzten 20 sichtbaren Zeilen (fuer activity-stagnation detection).
# sha1sum statt shasum (shasum ist BSD/macOS-only, fehlt in Debian-Containern).
turn_activity_hash() {
    local session="${1:?session name required}"
    tmux capture-pane -t "${session}:0" -p 2>/dev/null | tail -20 | sha1sum | awk '{print $1}'
}
