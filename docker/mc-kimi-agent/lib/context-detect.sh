# context-detect.sh — harness-aware context%-Scraper fuer poll.sh heartbeat().
#
# CTX-01 Nachzug (2026-08-09, Bug gemeldet von Mark): heartbeat() suchte NUR
# Claudes `ctx: NN`-Format im Pane — bei allen anderen Harnesses (Hermes,
# omp/Sparky/openclaude, Kimi) blieb der Kontext-Balken in der UI dauerhaft
# bei 0%, weil das Wort "ctx" in deren Statuszeilen gar nicht vorkommt. Live
# in der DB belegt: Hermes/Sparky/Grok/Kimi/Shakespeare(openclaude) 0%,
# waehrend Claude-Agenten (Davinci/Rex) korrekte Werte meldeten.
#
# Diese Datei ist EXTRA (nicht in ui-detect.sh) weil sie unabhaengig von der
# Runtime-UI-Erkennung (Bug 14, Bracketed-Paste) getestet werden muss — beide
# lesen zwar denselben Pane-Text, beantworten aber verschiedene Fragen.
#
# scrape_context_pct TEXT — extrahiert den Kontext-Prozentwert aus TEXT
# (typischerweise ein tmux pane_title oder ein capture-pane Tail). Waehlt das
# Muster ueber PANE_UI_OVERRIDE (im Image gebacken, siehe ui-detect.sh) — bei
# unbekanntem/leerem Harness oder wenn das harness-eigene Muster nichts
# findet, werden ALLE Muster der Reihe nach probiert (Fallback).
#
# WICHTIG: gibt bei keinem Treffer NICHTS aus (leerer String) — NIE "0"
# raten. heartbeat() behandelt einen leeren Rueckgabewert als "diesmal nicht
# gemeldet", das Backend behaelt dann den letzten bekannten Wert. Wuerde
# stattdessen "0" zurueckgegeben, wuerde eine frisch gestartete Session ohne
# Statuszeile (`ctx --`) faelschlich als "0% Kontext benutzt" gemeldet.
#
# Unterstuetzte Formate (mit Beispiel-Statuszeile):
#   claude:      `ctx: NN` / `ctx NN`
#                ✻ ctx 12%                                    (pane_title/Tail)
#   kimi:        `context: NN%`
#                context: 8% (21.3K/262.1K)                    (Tail, siehe ui-detect.sh)
#   openclaude:  Prozent DIREKT vor einem `/` (Bruch-Anzeige ohne Leerzeichen)
#                ◫ 8.3%/262K                                   (omp/Sparky-Statuszeile)
#   hermes:      Prozent DIREKT nach einer schliessenden Klammer `]` (Balken)
#                [█░░░░░░░░░] 8%                                (Hermes-Statuszeile)
#   fraction:    `21.3K/262.1K` ohne eigene %-Anzeige → Prozent BERECHNET
#                21.3K/262.1K                                   (Hermes-Fallback)
#
# `ctx --` / `[░░░░░░░░░░] --` (kein Wert, z.B. frisch gestartete Session)
# matcht ABSICHTLICH kein Muster — kein Treffer heisst "kein Wert", nicht "0".

# _ctx_claude TEXT — `ctx: NN` / `ctx NN`, optional gefolgt von `%`.
_ctx_claude() {
    echo "$1" | grep -oE 'ctx[: ]*[0-9]+' | grep -oE '[0-9]+' | tail -1
}

# _ctx_kimi TEXT — `context: NN%` (kimi-code Statuszeile).
_ctx_kimi() {
    echo "$1" | grep -oE 'context: [0-9]+%' | grep -oE '[0-9]+' | tail -1
}

# _ctx_openclaude TEXT — Prozentzahl DIREKT vor einem `/` (omp/Sparky:
# `◫ 8.3%/262K`). Ganzzahl-Anteil vor dem `.` reicht (wir runden ohnehin).
_ctx_openclaude() {
    echo "$1" | grep -oE '[0-9]+(\.[0-9]+)?%/' | grep -oE '[0-9]+' | head -1
}

# _ctx_hermes_bar TEXT — Prozentzahl DIREKT nach einer schliessenden
# Balken-Klammer `]` (Hermes: `[█░░░░░░░░░] 8%`). `[░░░░░░░░░░] --` (kein
# Wert) matcht bewusst nicht — die Regex verlangt Ziffern vor dem `%`.
_ctx_hermes_bar() {
    echo "$1" | grep -oE '\][[:space:]]*[0-9]+(\.[0-9]+)?%' | grep -oE '[0-9]+' | head -1
}

# _ctx_fraction TEXT — Bruchform `USED/TOTAL` mit optionalem K/M-Suffix
# (Hermes ohne eigene %-Anzeige oder als Cross-Check): `21.3K/262.1K` →
# rechnet used/total*100, gerundet. K/M werden aufgeloest (K=*1000, M=*1e6);
# ein Wert ohne Suffix bleibt roh. Division durch 0 (Total=0) → kein Treffer.
_ctx_fraction() {
    local frac
    frac=$(echo "$1" | grep -oE '[0-9]+(\.[0-9]+)?[KkMm]?/[0-9]+(\.[0-9]+)?[KkMm]?' | head -1)
    [ -n "$frac" ] || return 0
    echo "$frac" | awk -F'/' '
        function resolve(v,   suf, num) {
            suf = substr(v, length(v), 1)
            if (suf == "K" || suf == "k") {
                num = substr(v, 1, length(v) - 1) + 0
                return num * 1000
            } else if (suf == "M" || suf == "m") {
                num = substr(v, 1, length(v) - 1) + 0
                return num * 1000000
            }
            return v + 0
        }
        {
            used = resolve($1)
            total = resolve($2)
            if (total <= 0) { exit 1 }
            pct = (used / total) * 100
            printf "%d\n", (pct + 0.5)
        }
    '
}

# scrape_context_pct TEXT — siehe Datei-Kopf.
scrape_context_pct() {
    local text="$1"
    local pct=""

    case "${PANE_UI_OVERRIDE:-}" in
        claude)
            pct=$(_ctx_claude "$text")
            ;;
        kimi)
            pct=$(_ctx_kimi "$text")
            ;;
        openclaude)
            pct=$(_ctx_openclaude "$text")
            ;;
    esac

    # Fallback: harness unbekannt/leer ODER das harness-eigene Muster hat
    # nichts gefunden (z.B. Boss/claude-cli-Statuszeile in einer Uebergangs-
    # form) — alle Muster der Reihe nach probieren, spezifischste zuerst.
    if [ -z "$pct" ]; then
        pct=$(_ctx_claude "$text")
    fi
    if [ -z "$pct" ]; then
        pct=$(_ctx_kimi "$text")
    fi
    if [ -z "$pct" ]; then
        pct=$(_ctx_openclaude "$text")
    fi
    if [ -z "$pct" ]; then
        pct=$(_ctx_hermes_bar "$text")
    fi
    if [ -z "$pct" ]; then
        pct=$(_ctx_fraction "$text")
    fi

    # Sanitize: muss 0-100 Ganzzahl sein (defense-in-depth, das Backend
    # validiert zusaetzlich mit Field(ge=0, le=100)).
    if ! [[ "$pct" =~ ^[0-9]+$ ]] || [ "$pct" -gt 100 ] 2>/dev/null; then
        pct=""
    fi
    echo "$pct"
}
