#!/usr/bin/env bash
# migrate-agents-yml.sh — carry your own agent fleet across the OSS split.
#
# Background: docker/docker-compose.agents.yml used to be in version control
# AND is rewritten at runtime by the compose renderer. The release that takes
# it out of the repo breaks a plain `git pull` in one of two ways:
#
#   (a) Your file is identical to the committed one → git deletes it
#       SILENTLY. start-all.sh then creates an empty one, every mc-agent-*
#       service is gone from the compose — including hand-added mounts the
#       renderer never recreates.
#   (b) Your file differs (the normal case, it rewrites itself) →
#       `git pull --ff-only` aborts, and `install.sh --update` dies with it
#       (set -euo pipefail).
#
# Usage — wrap it around the pull:
#
#   scripts/migrate-agents-yml.sh save      # BEFORE git pull
#   git pull --ff-only
#   scripts/migrate-agents-yml.sh restore   # AFTER git pull
#
# `install.sh --update` does this for you. Both modes are no-ops once the
# migration has happened (and on installs that never had the tracked file),
# so they are safe to keep in the update path forever.
#
# NOTE: this script cannot rescue the very pull that brings it — it does not
# exist in your checkout until after that pull. For that one crossing, do the
# four commands in docs/setup/updating.md by hand; from then on this runs
# automatically.
set -euo pipefail

AGENTS="docker/docker-compose.agents.yml"
BACKUP="docker/docker-compose.agents.yml.pre-oss-split"

MODE="${1:-}"
# Argument first: a typo must always produce the usage, never a cheerful
# "nothing to migrate" that hides the mistake.
case "$MODE" in
  save|restore) ;;
  *)
    echo "usage: $0 {save|restore}" >&2
    echo "  save     before 'git pull' — back up your fleet, unblock the pull" >&2
    echo "  restore  after 'git pull'  — put your fleet back in place" >&2
    exit 1
    ;;
esac

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$ROOT" ]; then
  echo "✓ not a git checkout — nothing to migrate"
  exit 0
fi
cd "$ROOT"

is_tracked() { git ls-files --error-unmatch "$AGENTS" >/dev/null 2>&1; }

case "$MODE" in
  save)
    if ! is_tracked; then
      echo "✓ $AGENTS is already out of version control — nothing to migrate"
      exit 0
    fi
    if [ ! -f "$AGENTS" ]; then
      echo "✓ $AGENTS is tracked but not on disk — nothing to save"
      exit 0
    fi
    cp "$AGENTS" "$BACKUP"
    # Put the pristine committed content back so the pull can delete the file
    # without a conflict. Your version lives in $BACKUP until `restore`.
    git checkout -- "$AGENTS"
    echo "✓ your agent fleet is saved to $BACKUP"
    ;;

  restore)
    if [ ! -f "$BACKUP" ]; then
      echo "✓ no $BACKUP — nothing to restore"
      exit 0
    fi
    if is_tracked; then
      # The pull did not happen, or this version still ships the file. Put the
      # operator back exactly where they were and KEEP the backup — losing
      # their fleet inside a backup file would be the same bug in green.
      cp "$BACKUP" "$AGENTS"
      echo "! $AGENTS is still in version control — your version is back in"
      echo "  place and $BACKUP is kept. Re-run after a successful git pull."
      exit 0
    fi
    cp "$BACKUP" "$AGENTS"
    rm -f "$BACKUP"
    echo "✓ your agent fleet is back in $AGENTS (untracked now, see .gitignore)"
    ;;

esac
