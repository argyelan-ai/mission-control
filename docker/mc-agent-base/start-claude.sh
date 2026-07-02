#!/bin/sh
# start-claude.sh — openclaude-Launcher mit SOUL.md als System-Prompt
# und optionalem per-Agent API-Key override aus .env.
#
# Wird von entrypoint.sh via tmux aufgerufen. Drei Aufgaben:
#
#   1. /home/agent/.claude/.env sourcen wenn vorhanden (overrides fuer
#      OPENAI_API_KEY, etc. — wird von docker_agent_sync.py geschrieben
#      wenn agent.secret_id in der DB gesetzt ist. Fehlt die Datei,
#      greift der docker-compose env als Fallback).
#
#   2. SOUL.md lesen und als --append-system-prompt an openclaude weiter-
#      reichen (Bruecke zwischen Template-Renderer und openclaude).
#
#   3. openclaude starten. env-Vars werden vom Prozess geerbt.
#
# Weitere MC-Config-Files (TOOLS.md, HEARTBEAT.md, USER.md, MEMORY.md) liegen
# im selben Verzeichnis (/home/agent/.claude/) und koennen vom LLM per
# Tool-Call gelesen werden.
#
# Siehe: backend/app/services/docker_agent_sync.py (Backend-Seite des Syncs)

set -eu

ENV_FILE="/home/agent/.claude/.env"
SOUL_FILE="/home/agent/.claude/SOUL.md"
OPENCLAUDE_ARGS="--dangerously-skip-permissions"

# Schritt 1: .env sourcen (wenn vorhanden) — ueberschreibt Container-env
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Schritt 2+3: SOUL.md als system-prompt + openclaude starten
if [ -s "$SOUL_FILE" ]; then
    exec openclaude $OPENCLAUDE_ARGS --append-system-prompt "$(cat "$SOUL_FILE")"
else
    exec openclaude $OPENCLAUDE_ARGS
fi
