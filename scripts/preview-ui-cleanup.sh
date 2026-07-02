#!/bin/bash
# Preview-UI Cleanup: Stoppt Preview und entfernt Worktree.
#
# Usage: ./scripts/preview-ui-cleanup.sh <branch>
# Example: ./scripts/preview-ui-cleanup.sh feature/new-dashboard

set -euo pipefail

BRANCH="${1:?Usage: preview-ui-cleanup.sh <branch>}"
SLUG=$(echo "$BRANCH" | sed 's|/|-|g' | tr '[:upper:]' '[:lower:]')
WORKTREE="/tmp/mc-preview-${SLUG}"
PORT="${2:-3001}"

echo "=== MC Preview-UI Cleanup ==="

# 1. Dev-Server stoppen (falls via preview-ui.sh gestartet)
PID=$(lsof -i ":${PORT}" -t 2>/dev/null | head -1)
if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null && echo "Dev-Server gestoppt (PID $PID, Port $PORT)"
fi

# 2. Worktree entfernen
if [ -d "$WORKTREE" ]; then
    git worktree remove "$WORKTREE" --force 2>/dev/null && echo "Worktree entfernt: $WORKTREE"
else
    echo "Worktree nicht gefunden: $WORKTREE"
fi

echo "Cleanup fertig."
