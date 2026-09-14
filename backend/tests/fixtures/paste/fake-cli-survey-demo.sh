#!/bin/bash
# fake-cli-survey-demo.sh — minimaler Stand-in fuer claude-cli, NUR fuer den
# echten (nicht gestubbten) tmux-Vorher/Nachher-Beweis von Fall 4
# (2026-09-14). Rendert dieselbe Composer-Box wie die claude-24-*.txt
# Fixtures und zeigt nach dem ERSTEN empfangenen Enter den beobachteten
# Feedback-Umfrage-Dialog — genau die Reihenfolge aus dem Live-Fund bei Rex.
set -u

BOX='────────────────────────────────────────────────────────────────────────────────'

render_idle() {
    clear
    printf '\n ▐▛███▜▌   Claude Code v2.1.217 (Fall-4-Demo)\n▝▜█████▛▘  Sonnet 5 · Claude API\n  ▘▘ ▝▝    /home/agent\n\n ▎ Fake-CLI — nur fuer den tmux-Beweis, kein echtes claude-cli\n\n\n\n\n\n\n\n'
    printf '%s\n' "$BOX"
    printf '❯\xc2\xa0%s\n' "$1"
    printf '%s\n' "$BOX"
    printf '  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n'
}

render_survey() {
    clear
    printf '\n ▐▛███▜▌   Claude Code v2.1.217 (Fall-4-Demo)\n▝▜█████▛▘  Sonnet 5 · Claude API\n  ▘▘ ▝▝    /home/agent\n\n ▎ Fake-CLI — nur fuer den tmux-Beweis, kein echtes claude-cli\n\n\n\n\n'
    printf '1: Bad   2: Fine   3: Good   0: Dismiss\n\n\n'
    printf '%s\n' "$BOX"
    printf '❯\xc2\xa0\n'
    printf '%s\n' "$BOX"
    printf '  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n'
}

composer=""
survey_shown=0
in_survey=0
render_idle "$composer"

# Die Umfrage ist ein Hotkey-Menue (kein Enter noetig) — genau wie Claude
# Codes echte Permission-Dialoge ("1. Yes  2. Yes, and don't ask again  3.
# No"), die auf einen einzelnen Tastendruck sofort reagieren. Deshalb liest
# der Umfrage-Zweig EIN Zeichen (read -n 1), waehrend der normale Composer
# zeilenweise auf Enter wartet (echtes claude-cli-Verhalten fuer Text-Eingabe).
while true; do
    if [ "$in_survey" = "1" ]; then
        IFS= read -r -n 1 key || break
        if [ "$key" = "0" ]; then
            in_survey=0
            render_idle "$composer"
        else
            in_survey=0
            clear
            printf '\n[FEHLER-SIMULATION] Falscher Key waere hier als Bewertung durchgegangen: %s\n' "$key"
            render_idle "$composer"
        fi
        continue
    fi
    IFS= read -r line || break
    composer=""
    if [ "$survey_shown" = "0" ]; then
        survey_shown=1
        in_survey=1
        render_survey
    else
        render_idle "$composer"
    fi
done
