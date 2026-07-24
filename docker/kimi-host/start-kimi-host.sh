#!/bin/bash
# start-kimi-host.sh — Kimi-Launcher für den Host-Agent (Window 0).
#
# SOUL-Brücke wie im Container (start-kimi.sh): Kimi lädt AGENTS.md aus dem
# Startverzeichnis automatisch. CARD.md > SOUL.md, Quelle ist das Host-
# Config-Dir ~/.mc/agents/kimi/ (von sync/provision geschrieben).
# Startverzeichnis = Agent-Workspace.

set -eu

BASE="$HOME/.mc/agents/kimi"
WORKSPACE="$HOME/.mc/workspaces/kimi"
CARD_FILE="$BASE/CARD.md"
SOUL_FILE="$BASE/SOUL.md"
[ -s "$CARD_FILE" ] || CARD_FILE="$SOUL_FILE"

mkdir -p "$WORKSPACE"
if [ -s "$CARD_FILE" ]; then
    cp "$CARD_FILE" "$WORKSPACE/AGENTS.md"
fi

cd "$WORKSPACE"
exec "${KIMI_BIN:-kimi}" --auto
