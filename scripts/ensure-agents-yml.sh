#!/usr/bin/env bash
# ensure-agents-yml.sh — make sure your own agent fleet file exists.
#
# docker/docker-compose.agents.yml is NOT in version control: it is rewritten
# at runtime (backend/app/services/compose_renderer.py) and describes your own
# fleet — agent names, project references, mount paths. Shipped is the
# agent-free template; this creates your copy from it, once.
#
# Called by setup.sh (first install) and start-all.sh (every start). Must run
# from the project directory. Exits non-zero when the template is missing:
# warning and carrying on only postpones the failure to the next
# `docker compose -f docker/docker-compose.agents.yml ...`, where it surfaces
# as a raw compose error that overwrites the helpful message.
set -euo pipefail

AGENTS_YML="docker/docker-compose.agents.yml"
AGENTS_EXAMPLE="docker/docker-compose.agents.example.yml"

# 0600 whether we create it or find it. The file lists your agents, the
# projects they touch and where their mounts point — the same class of content
# as docker/.env.shared right next to it, which this project already keeps at
# 0600. `cp` below would otherwise inherit the umask and leave it world-readable,
# and installs that predate this keep whatever they were created with, so the
# tightening runs on every start rather than only on creation.
harden() { chmod 600 "$AGENTS_YML"; }

if [ -f "$AGENTS_YML" ]; then
  harden
  exit 0
fi

if [ ! -f "$AGENTS_EXAMPLE" ]; then
  echo "✗ $AGENTS_EXAMPLE not found — cannot create $AGENTS_YML." >&2
  echo "  Every 'docker compose -f $AGENTS_YML ...' will fail without it." >&2
  echo "  Run this from the project directory of a complete checkout." >&2
  exit 1
fi

cp "$AGENTS_EXAMPLE" "$AGENTS_YML"
harden
echo "✓ $AGENTS_YML created from the template (no agents yet)"
