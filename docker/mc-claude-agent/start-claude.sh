#!/bin/bash
# start-claude.sh — claude-code Launcher mit SOUL.md als System-Prompt.
#
# Wird von entrypoint.sh via tmux aufgerufen. Drei Aufgaben:
#
#   1. /home/agent/.claude/.env sourcen wenn vorhanden (CLAUDE_CODE_OAUTH_TOKEN
#      kommt aus Bootstrap-Response, siehe entrypoint.sh).
#
#   2. CARD.md (falls vorhanden, Context-Economy Stufe 2 Opt-in) oder sonst
#      SOUL.md lesen und als --append-system-prompt an claude weiterreichen
#      (Brücke zwischen Template-Renderer und claude-code CLI).
#
#   3. claude starten. env-Vars werden vom Prozess geerbt.
#
# Unterschiede zu docker/mc-agent-base/start-claude.sh (openclaude-Variant):
#   - Binary: `claude` statt `openclaude`
#   - Kein OpenAI-Shim — claude-code nutzt CLAUDE_CODE_OAUTH_TOKEN direkt
#   - Modell kommt aus settings.json (CLAUDE_CONFIG_DIR/settings.json)
#
# Weitere MC-Config-Files (TOOLS.md, HEARTBEAT.md, USER.md, MEMORY.md) liegen
# im selben Verzeichnis (/home/agent/.claude/) und können vom Agent per
# Tool-Call gelesen werden.

set -eu

ENV_FILE="/home/agent/.claude/.env"
CARD_FILE="/home/agent/.claude/CARD.md"
SOUL_FILE="/home/agent/.claude/SOUL.md"
# Context-Economy Stufe 2: CARD.md (<=5KB) ersetzt SOUL.md (~29KB) als
# --append-system-prompt, aber nur fuer Agenten mit gesetztem Opt-in-Flag
# (docker_agent_sync.write_operating_card schreibt/loescht die Datei je nach
# agent.use_operating_card). -s statt -f: eine LEERE CARD.md (0 Byte) muss
# wie "fehlt" behandelt werden, sonst startet der Agent ganz ohne
# System-Prompt statt auf SOUL.md zurueckzufallen (matcht den -s-Check unten).
[ -s "$CARD_FILE" ] || CARD_FILE="$SOUL_FILE"
CLAUDE_ARGS="--dangerously-skip-permissions"

# Schritt 1: .env sourcen (wenn vorhanden) — überschreibt Container-env
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Schritt 2+3: CARD.md/SOUL.md als system-prompt + claude starten
if [ -s "$CARD_FILE" ]; then
    exec claude $CLAUDE_ARGS --append-system-prompt "$(cat "$CARD_FILE")"
else
    exec claude $CLAUDE_ARGS
fi
