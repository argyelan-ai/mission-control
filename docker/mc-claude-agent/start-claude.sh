#!/bin/bash
# start-claude.sh — claude-code Launcher mit SOUL.md als System-Prompt.
#
# Wird von entrypoint.sh via tmux aufgerufen. Drei Aufgaben:
#
#   1. /home/agent/.claude/.env sourcen wenn vorhanden (CLAUDE_CODE_OAUTH_TOKEN
#      kommt aus Bootstrap-Response, siehe entrypoint.sh).
#
#   2. SOUL.md lesen und als --append-system-prompt an claude weiterreichen
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
SOUL_FILE="/home/agent/.claude/SOUL.md"
CLAUDE_ARGS="--dangerously-skip-permissions"

# Schritt 1: .env sourcen (wenn vorhanden) — überschreibt Container-env
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Schritt 2+3: SOUL.md als system-prompt + claude starten
if [ -s "$SOUL_FILE" ]; then
    exec claude $CLAUDE_ARGS --append-system-prompt "$(cat "$SOUL_FILE")"
else
    exec claude $CLAUDE_ARGS
fi
