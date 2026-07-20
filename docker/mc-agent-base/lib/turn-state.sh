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

    # claude-cli 2.1.x renders the idle prompt as `❯` + NO-BREAK SPACE
    # (U+00A0). The `^❯ *$` idle check below only knows plain spaces, so an
    # idle pane classified as working forever and the comm_v2 message gate
    # never opened (live pilot finding 2026-07-20). Normalize NBSP to plain
    # space before any pattern runs.
    # (printf-octal statt $'\\u00a0' — bash 3.2 auf macOS kennt \\uHHHH nicht,
    # die Tests laufen auch auf dem Host.)
    local _nbsp
    _nbsp=$(printf '\302\240')
    capture=${capture//${_nbsp}/ }

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
    # NUR Live-Signale zaehlen (live pilot finding 2026-07-20): claude-cli
    # laesst die ABGESCHLOSSENE Turn-Summary ("✻ Cogitated for 21s") und
    # Transcript-Zeilen im Pane stehen — Vergangenheitsverben wie
    # Cogitated/Crunched/Spelunking als Working-Marker klassifizierten jede
    # idle Pane dauerhaft als working und das comm_v2-Message-Gate oeffnete
    # nie. Live heisst: "esc to interrupt" in der Statuszeile oder ein
    # aktiver Spinner mit Ellipsis ("✻ Verbing… (12s"). ✻ Churned bleibt
    # ausgenommen (Timeout/Stagnation, kein aktiver Turn).
    local recent
    recent=$(echo "$capture" | tail -20)
    if echo "$recent" | grep -qE 'esc to interrupt|● Bash\(|● Read\(|● Write\(|● Edit\('; then
        echo "working"
        return
    fi
    if echo "$recent" | grep -qE '✻.*…' && ! echo "$recent" | grep -qE '✻ Churned'; then
        echo "working"
        return
    fi

    # claude-cli 2.1.x idle: die Inputbox traegt oft Ghost-Text
    # (Prompt-Suggestions / pending Wakeup-Anzeige) — `^❯ *$` matcht dann
    # nie. Wenn die Statuszeile OHNE "esc to interrupt" sichtbar ist, ist
    # der Turn beendet: idle. (Bewusst NACH den Working-Checks — waehrend
    # eines Turns zeigt die Statuszeile "… · esc to interrupt · …" und
    # greift oben schon als working.)
    if echo "$capture" | tail -10 | grep -qE '⏵⏵ bypass permissions|bypass permissions on'; then
        echo "idle"
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
