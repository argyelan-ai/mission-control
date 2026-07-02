#!/bin/bash
# Stop-Hook: Memory-Änderungen automatisch committen
# Läuft nach jeder Claude-Antwort — committed geänderte Memory-Files ohne Interaction

# Claude Code project-slug for $HOME is the path with "/" replaced by "-"
MEMORY_DIR="$HOME/.claude/projects/$(echo "$HOME" | tr '/' '-')/memory"

cd "$MEMORY_DIR" 2>/dev/null || exit 0

# Nur wenn git-Repo und Änderungen vorhanden
git rev-parse --git-dir &>/dev/null || exit 0
git diff --quiet && git diff --cached --quiet && exit 0

# Untracked + geänderte Files stagen (nur .md Files)
git add *.md 2>/dev/null

# Committen falls was staged ist
if ! git diff --cached --quiet; then
    git commit -m "auto: memory update $(date '+%Y-%m-%d %H:%M')" --no-gpg-sign -q 2>/dev/null
fi
