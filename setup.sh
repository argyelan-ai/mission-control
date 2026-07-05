#!/usr/bin/env bash
# Mission Control — Quick Setup Script
# Run once to generate .env from .env.example and prepare the environment.
# Portable: works with both BSD sed (macOS) and GNU sed (Linux).
set -e

echo "🚀 Mission Control — Setup"
echo "================================"

# Portable in-place sed: BSD sed needs `-i ''`, GNU sed plain `-i`.
sed_i() {
  if sed --version >/dev/null 2>&1; then
    sed -i "$@"        # GNU sed
  else
    sed -i '' "$@"     # BSD sed (macOS)
  fi
}

if [ -f .env ]; then
  echo "✅ .env already exists, skipping generation"
else
  cp .env.example .env

  # Generate secure values for every cryptographic placeholder — a fresh
  # install must never run with weak defaults.
  LOCAL_AUTH_TOKEN=$(openssl rand -hex 32)
  DB_PASSWORD=$(openssl rand -hex 16)
  JWT_SECRET_KEY=$(openssl rand -hex 32)
  REDIS_PASSWORD=$(openssl rand -hex 16)
  # Fernet key for the secrets vault (32 bytes, url-safe base64)
  SECRETS_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null \
      || openssl rand -base64 32 | tr '+/' '-_' | cut -c1-44)

  sed_i "s|^LOCAL_AUTH_TOKEN=.*|LOCAL_AUTH_TOKEN=$LOCAL_AUTH_TOKEN|" .env
  sed_i "s|^DB_PASSWORD=.*|DB_PASSWORD=$DB_PASSWORD|" .env
  sed_i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$JWT_SECRET_KEY|" .env
  sed_i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$REDIS_PASSWORD|" .env
  sed_i "s|^SECRETS_ENCRYPTION_KEY=.*|SECRETS_ENCRYPTION_KEY=$SECRETS_ENCRYPTION_KEY|" .env

  # Host-specific values the containers need at runtime:
  # HOST_UID → tmux socket path for the host-pty bridge; MC_REPO_PATH →
  # absolute repo path for cross-image runtime switches (see docker-compose.yml).
  sed_i "s|^HOST_UID=.*|HOST_UID=$(id -u)|" .env
  sed_i "s|^MC_REPO_PATH=.*|MC_REPO_PATH=$(pwd)|" .env

  echo "✅ .env created with generated secrets"
  echo "   LOCAL_AUTH_TOKEN:       ${LOCAL_AUTH_TOKEN:0:8}..."
  echo "   DB_PASSWORD:            ${DB_PASSWORD:0:8}..."
  echo "   JWT_SECRET_KEY:         ${JWT_SECRET_KEY:0:8}..."
  echo "   REDIS_PASSWORD:         ${REDIS_PASSWORD:0:8}..."
  echo "   SECRETS_ENCRYPTION_KEY: ${SECRETS_ENCRYPTION_KEY:0:8}..."
  echo "   HOST_UID:               $(id -u)"
  echo "   MC_REPO_PATH:           $(pwd)"
fi

# Backfill host-specific keys for .env files that predate them (upgrades /
# hand-copied .env.example) — the fresh-install branch above never runs then.
grep -q '^HOST_UID=' .env || { echo "HOST_UID=$(id -u)" >> .env; echo "✅ backfilled HOST_UID=$(id -u)"; }
grep -q '^MC_REPO_PATH=' .env || { echo "MC_REPO_PATH=$(pwd)" >> .env; echo "✅ backfilled MC_REPO_PATH=$(pwd)"; }

# Preflight: create deliverables dirs as the host user (so the first
# `docker compose up` does not create them as root).
if [ -x scripts/init-mc-deliverables-dirs.sh ]; then
  echo ""
  echo "🗂️  Initializing ~/.mc/deliverables/..."
  bash scripts/init-mc-deliverables-dirs.sh
fi

echo ""
echo "Next steps:"
echo "  1. Start the stack:    docker compose up --build -d"
echo "     (database migrations run automatically inside the backend)"
echo "  2. Open http://localhost and register the first admin user"
echo "     (POST /api/v1/auth/register works only while no user exists)."
echo ""
echo "Optional integrations (Discord, Telegram, voice) are configured via"
echo ".env — every key is documented in .env.example. GitHub can also be"
echo "connected later in the app under Settings → GitHub."
echo ""
echo "To run CLI agents (docs/setup/first-agent.md): start the host bridge"
echo "  python3 scripts/cli-bridge.py &   # provisioning server on :18792"
echo ""
