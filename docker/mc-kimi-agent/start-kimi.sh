#!/bin/bash
# start-kimi.sh — Kimi-Code-Launcher mit SOUL.md/CARD.md als Agenten-Identität.
#
# Wird von entrypoint.sh via tmux aufgerufen. Drei Aufgaben:
#
#   1. /home/agent/.claude/.env sourcen wenn vorhanden (MC-Render-Konvention —
#      der Ordner heisst historisch claude-config, ist aber der harness-
#      neutrale MC-Config-Mount der ganzen Flotte).
#
#   2. SOUL-Brücke: Kimi kennt kein --append-system-prompt, lädt aber
#      AGENTS.md aus dem Startverzeichnis automatisch als Projekt-Identität
#      (Spike 2026-07-24: AGENTS.md-Instruktion gewinnt gegen die Default-
#      Identität; --agent-file ist experimental-only und daher ungeeignet).
#      CARD.md (Context-Economy Opt-in) > SOUL.md, gleiches Fallback wie
#      start-claude.sh.
#
#   3. kimi starten — --auto (voll autonom, kein Permission-Prompt) ist das
#      Flotten-Äquivalent zu claude --dangerously-skip-permissions.

set -eu

ENV_FILE="/home/agent/.claude/.env"
CARD_FILE="/home/agent/.claude/CARD.md"
SOUL_FILE="/home/agent/.claude/SOUL.md"
[ -s "$CARD_FILE" ] || CARD_FILE="$SOUL_FILE"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# SOUL → AGENTS.md Brücke. Kopie statt Symlink: der Mount-Ordner und $HOME
# sind verschiedene Filesysteme, und eine Kopie friert den Stand pro
# Session-Start ein (Sync-Config + Window-Respawn liefert den frischen Stand).
if [ -s "$CARD_FILE" ]; then
    cp "$CARD_FILE" /home/agent/AGENTS.md
fi

cd /home/agent
exec kimi --auto
