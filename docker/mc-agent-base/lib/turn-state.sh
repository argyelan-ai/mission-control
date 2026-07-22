#!/bin/bash
# turn-state.sh — Klassifikation des claude/openclaude Turn-States.
# Wird von poll.sh gesourced. Exportiert: detect_turn_state(), extract_turn_error(),
# turn_activity_hash().
#
# KANONISCHE QUELLE: dieses File existiert byte-identisch als
#   docker/mc-claude-agent/lib/turn-state.sh
#   docker/mc-agent-base/lib/turn-state.sh
# Beide Kopien MUESSEN identisch bleiben (test_turn_state.py laeuft gegen beide).
# build-agent-images.sh synct nur shared/poll.sh, NICHT lib/ — daher Handpflege.
#
# Hintergrund (Plan 2026-04-17-agent-turn-state-observability.md):
# openclaude behandelt transient API-Errors (fetch failed, Connection error, 5xx)
# als Turn-Abort und kehrt zum interaktiven `❯`-Prompt zurueck. Der claude-Prozess
# lebt weiter, aber niemand meldet den Fehler ans Backend — Task bleibt fuer immer
# in_progress. Dieser Helper schliesst die Feedback-Loop: poll.sh klassifiziert
# den Turn-State und meldet crashed/stagnated runs als Blocker.
#
# W2.1 Turn-Signal (Phase A, 2026-07-22): native Claude-Code-Hooks
# (UserPromptSubmit/Stop) appenden `<epoch> submit` bzw. `<epoch> stop` an
# eine Signal-Datei (Default /home/agent/.turn-signal). detect_turn_state liest
# sie zuerst und faellt nur bei fehlender/veralteter Datei auf das (fragile)
# Pane-Scraping zurueck. Stop feuert NICHT bei User-Interrupt (Esc) und
# API-Fehler — deshalb ist der Scraping-Fallback PFLICHT, kein Nice-to-have.

# _turn_signal_truncate_if_large FILE — kappt eine davongelaufene Signal-Datei
# auf ihre letzte Zeile (der aktuelle State). Ein Dauerlaeufer-Agent wuerde die
# Datei sonst unbegrenzt wachsen lassen (ein Append pro Submit/Stop). Guenstiger
# stat-Check pro Poll-Zyklus, seltenes Rewrite. Default-Cap 1 MB, ENV-tunable.
# stat ist plattformabhaengig: BSD/macOS `-f%z` vs GNU/Linux `-c%s` — beide
# probieren (Tests laufen auf dem Mac, Prod im Debian-Container).
_turn_signal_truncate_if_large() {
    local file="$1"
    local max="${TURN_SIGNAL_MAX_BYTES:-1048576}"
    local size
    size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null || echo 0)
    [ -n "$size" ] || size=0
    case "$size" in
        ''|*[!0-9]*) return ;;
    esac
    if [ "$size" -gt "$max" ]; then
        local tmp="${file}.tmp.$$"
        if tail -n 1 "$file" > "$tmp" 2>/dev/null; then
            mv "$tmp" "$file" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
        fi
    fi
}

# _turn_signal_probe FILE — echot "<kind> <age>" wobei kind = stop|submit und
# age = Sekunden seit dem Epoch der letzten Signal-Zeile. Echot "none" wenn die
# Datei fehlt / leer / unlesbar / unparsebar ist. bash-3.2-safe, nur date+tail.
_turn_signal_probe() {
    local file="$1"
    [ -f "$file" ] && [ -r "$file" ] || { echo "none"; return; }
    _turn_signal_truncate_if_large "$file"
    local last
    last=$(tail -n 1 "$file" 2>/dev/null || echo "")
    [ -n "$last" ] || { echo "none"; return; }
    # Zeilenformat: "<epoch> <kind>" — erstes Feld Epoch, letztes Feld kind.
    local epoch kind
    epoch=${last%% *}
    kind=${last##* }
    case "$epoch" in
        ''|*[!0-9]*) echo "none"; return ;;
    esac
    case "$kind" in
        stop|submit) : ;;
        *) echo "none"; return ;;
    esac
    local now age
    now=$(date +%s)
    age=$((now - epoch))
    echo "$kind $age"
}

# _pane_is_crashed SESSION — 0 (true) wenn das Pane einen echten Turn-Crash-
# Marker zeigt (API/Network-Error der den Turn abbricht). Wird aus dem Turn-
# Signal-Schnellpfad gerufen: ein frischer `submit` faerbt working, aber ein
# Crash mitten im Turn feuert KEIN Stop-Hook — daher bleibt die Crash-Erkennung
# scrape-autoritativ. Muster bewusst identisch zum Crashed-Check im Scrape-Pfad
# von detect_turn_state (eine Aenderung dort hier mitziehen).
_pane_is_crashed() {
    local session="$1" cap
    cap=$(tmux capture-pane -t "${session}:0" -p -S -50 2>/dev/null || echo "")
    [ -n "$cap" ] || return 1
    echo "$cap" | grep -qE 'API Error: fetch failed|API Error: Connection error|API Error: 5[0-9]{2}'
}

# Gibt einen von: working | crashed | idle | unknown
detect_turn_state() {
    local session="${1:?session name required}"

    # ── W2.1 Turn-Signal (Phase A) ────────────────────────────────────────
    # TURN_SIGNAL_MODE: auto (default) | hooks | scrape.
    #   scrape → Signal ignorieren, sofort Alt-Pfad (byte-identisch).
    #   hooks  → NUR Signal (fuer Tests): kein Scrape-Fallback.
    #   auto   → Signal wenn vorhanden+frisch, sonst Scrape-Fallback.
    # Ohne Signal-Datei ist auto byte-identisch zum alten Verhalten (Flotte live).
    local sig_mode="${TURN_SIGNAL_MODE:-auto}"
    if [ "$sig_mode" != "scrape" ]; then
        local sig_file="${TURN_SIGNAL_FILE:-/home/agent/.turn-signal}"
        local probe kind age
        probe=$(_turn_signal_probe "$sig_file")
        if [ "$probe" != "none" ]; then
            kind=${probe%% *}
            age=${probe##* }
            if [ "$kind" = "stop" ]; then
                echo "idle"
                return
            elif [ "$kind" = "submit" ]; then
                if [ "$sig_mode" = "hooks" ]; then
                    # hooks-only (Test-Modus): reines Signal, kein Crash-Scrape.
                    echo "working"
                    return
                fi
                # auto: ein `submit` heisst claude ist IN einem Turn — aber ein
                # API/Network-Crash mitten im Turn feuert KEIN Stop-Hook. Der
                # Crash-Marker muss daher scrape-autoritativ bleiben: billiges
                # crashed-Scraping laufen lassen, crashed gewinnt ueber working.
                # Sonst versteckt sich ein Crash bis zur Staleness-Grenze (~900s)
                # hinter dem frischen submit statt in ~15s als Blocker zu feuern.
                if _pane_is_crashed "$session"; then
                    echo "crashed"
                    return
                fi
                # Staleness-Schutz: Stop feuert nicht bei Interrupt (Esc). Ein
                # `submit` aelter als STALE_SECONDS ist verdaechtig → auf volles
                # Scraping zurueckfallen (idle/crashed werden dort erkannt).
                local stale="${TURN_SIGNAL_STALE_SECONDS:-900}"
                if [ "$age" -lt "$stale" ]; then
                    echo "working"
                    return
                fi
                # auto + stale submit → Fall-through zum Scraping.
            fi
        elif [ "$sig_mode" = "hooks" ]; then
            # hooks-only: keine (verwertbare) Datei → nichts zu melden.
            echo "unknown"
            return
        fi
        # auto + (none | stale submit) → weiter zum Scraping unten.
    fi

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
    # normaler Bash-Tool-Fehler ist (mc deliverable 422, mkdir permission,
    # etc). Claude bekommt den Exit-Code als Tool-Output zurueck und
    # reagiert self-correcting; das ist KEIN Session-Crash. Vorher hat
    # poll.sh Agents mitten im Self-Correct-Flow ge-killed.
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
# Debian-base hat kein `shasum` (BSD/macOS-spezifisch) — sha1sum ist portable
# und auf jeder Linux-Distro vorhanden. Gleicher Output-Format ($hash  -).
turn_activity_hash() {
    local session="${1:?session name required}"
    tmux capture-pane -t "${session}:0" -p 2>/dev/null | tail -20 | sha1sum | awk '{print $1}'
}
