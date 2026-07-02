#!/bin/bash
# Preview-UI: Startet eine isolierte Frontend-Preview auf einem eigenen Port.
# Nutzt Git-Worktree + npm dev. Backend/DB/Redis werden vom Hauptsystem geteilt.
#
# Usage: ./scripts/preview-ui.sh <branch> [port]
# Example: ./scripts/preview-ui.sh feature/new-dashboard 3001

set -euo pipefail

BRANCH="${1:?Usage: preview-ui.sh <branch> [port]}"
PORT="${2:-3001}"
SLUG=$(echo "$BRANCH" | sed 's|/|-|g' | tr '[:upper:]' '[:lower:]')
WORKTREE="/tmp/mc-preview-${SLUG}"

echo "=== MC Preview-UI ==="
echo "Branch:   $BRANCH"
echo "Port:     $PORT"
echo "Worktree: $WORKTREE"
echo ""

# 1. Worktree erstellen
if [ -d "$WORKTREE" ]; then
    echo "Worktree existiert bereits: $WORKTREE"
    echo "Benutze: ./scripts/preview-ui-cleanup.sh $BRANCH"
    exit 1
fi

git worktree add "$WORKTREE" "$BRANCH"
echo "Worktree erstellt."

# 2. Dependencies installieren
cd "$WORKTREE/frontend"
npm install --silent 2>/dev/null
echo "Dependencies installiert."

# 3. Dev-Server starten
echo ""
echo "Starte Preview auf http://localhost:${PORT}"
echo "Stoppen: Ctrl+C, dann ./scripts/preview-ui-cleanup.sh $BRANCH"
echo ""

# API-URL auf Caddy/Backend zeigen (Preview laeuft auf eigenem Port,
# braucht absoluten Pfad zum Backend — nicht relative Pfade).
NEXT_PUBLIC_API_URL=http://localhost npx next dev -p "$PORT"
