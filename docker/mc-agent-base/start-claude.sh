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
#   2. CARD.md (falls vorhanden, Context-Economy Stufe 2 Opt-in) oder sonst
#      SOUL.md lesen und als --append-system-prompt an openclaude weiter-
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
CARD_FILE="/home/agent/.claude/CARD.md"
SOUL_FILE="/home/agent/.claude/SOUL.md"
# Context-Economy Stufe 2: CARD.md (<=5KB) ersetzt SOUL.md (~29KB) als
# --append-system-prompt, aber nur fuer Agenten mit gesetztem Opt-in-Flag
# (docker_agent_sync.write_operating_card schreibt/loescht die Datei je nach
# agent.use_operating_card). Datei-Existenz ist die einzige Weiche hier.
[ -f "$CARD_FILE" ] || CARD_FILE="$SOUL_FILE"
OPENCLAUDE_ARGS="--dangerously-skip-permissions"

# Schritt 1: .env sourcen (wenn vorhanden) — ueberschreibt Container-env
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Schritt 2+3: CARD.md/SOUL.md als system-prompt + openclaude starten
if [ -s "$CARD_FILE" ]; then
    exec openclaude $OPENCLAUDE_ARGS --append-system-prompt "$(cat "$CARD_FILE")"
else
    exec openclaude $OPENCLAUDE_ARGS
fi
