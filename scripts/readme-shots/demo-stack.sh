#!/usr/bin/env bash
# Throwaway demo stack for the README screenshots (see README.md here).
#
#   scripts/readme-shots/demo-stack.sh build   # images mc-demo-*:demo from this checkout
#   scripts/readme-shots/demo-stack.sh up      # start db, redis, backend, frontend
#   scripts/readme-shots/demo-stack.sh admin   # first admin + wizard skipped (shots after the wizard)
#   scripts/readme-shots/demo-stack.sh seed    # demo board via scripts/demo-seed.py
#   scripts/readme-shots/demo-stack.sh down    # remove containers, volume and images
#
# Everything is project "mc-demo": own network, own volume, own ports
# (DEMO_WEB_PORT, default 13000; DEMO_API_PORT, default 18000). It has no
# Docker access and starts no worker, agents, voice or browser services.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STATE="${DEMO_STATE_DIR:-${TMPDIR:-/tmp}/mc-readme-demo}"
ENV_FILE="$STATE/demo.env"
COMPOSE=(docker compose -p mc-demo --env-file "$ENV_FILE" -f "$HERE/docker-compose.demo.yml")
API="http://127.0.0.1:${DEMO_API_PORT:-18000}"

secrets() {
  mkdir -p "$STATE"
  [ -f "$ENV_FILE" ] && return 0
  r() { openssl rand -hex 24; }
  cat > "$ENV_FILE" <<ENV
DB_PASSWORD=$(r)
REDIS_PASSWORD=$(r)
LOCAL_AUTH_TOKEN=$(r)
JWT_SECRET_KEY=$(r)
SECRETS_ENCRYPTION_KEY=$(r)
INTERNAL_BOOTSTRAP_SECRET=$(r)
ENV
  chmod 600 "$ENV_FILE"
}

token() { grep '^LOCAL_AUTH_TOKEN=' "$ENV_FILE" | cut -d= -f2; }

case "${1:-}" in
  build)
    # Disk preflight like every other build path (docker/shared/disk-preflight.sh):
    # two images, a full disk would die mid-layer-write.
    MC_ENV_FILE="${MC_ENV_FILE:-$REPO/.env}"
    export MC_ENV_FILE
    . "$REPO/docker/shared/disk-preflight.sh"
    mc_disk_preflight
    docker build -t mc-demo-backend:demo "$REPO/backend"
    # Build the frontend from a clean export of the committed tree: no local
    # node_modules (the Dockerfile copies the whole context) and no
    # uncommitted edits end up in the image.
    tmp="$(mktemp -d)"
    git -C "$REPO" archive HEAD frontend-v2 | tar -x -C "$tmp"
    docker build -t mc-demo-frontend:demo "$tmp/frontend-v2"
    rm -rf "$tmp"
    ;;
  up)
    secrets
    "${COMPOSE[@]}" up -d --wait
    echo "demo UI: http://127.0.0.1:${DEMO_WEB_PORT:-13000}  (token in $ENV_FILE)"
    ;;
  admin)
    # The first admin through the same endpoint the wizard uses.
    curl -fsS -X POST "$API/api/v1/auth/register" -H 'Content-Type: application/json' \
      -d '{"email":"operator@example.com","password":"demo-password-123","name":"Operator"}' >/dev/null \
      && echo "admin created (operator@example.com / demo-password-123)"
    ;;
  seed)
    MC_API_URL="$API" LOCAL_AUTH_TOKEN="$(token)" python3 "$REPO/scripts/demo-seed.py"
    ;;
  token)
    token
    ;;
  down)
    "${COMPOSE[@]}" down -v --remove-orphans || true
    docker rmi mc-demo-backend:demo mc-demo-frontend:demo 2>/dev/null || true
    rm -rf "$STATE"
    ;;
  *)
    sed -n '2,13p' "$0"; exit 2 ;;
esac
