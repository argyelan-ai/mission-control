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

# Next to this script, not the working directory (the tests run it elsewhere).
# shellcheck source=scripts/lib/remote-access.sh
. "$(dirname "$0")/scripts/lib/remote-access.sh"

if [ -f .env ]; then
  echo "✅ .env already exists, skipping generation"
else
  cp .env.example .env
  # .env holds DB/JWT/encryption secrets — lock it to the owner right away.
  # chmod only flips permission bits, it never touches the inode (unlike
  # `sed -i` on some platforms, which can replace the file).
  chmod 600 .env

  # Generate secure values for every cryptographic placeholder — a fresh
  # install must never run with weak defaults.
  LOCAL_AUTH_TOKEN=$(openssl rand -hex 32)
  DB_PASSWORD=$(openssl rand -hex 16)
  JWT_SECRET_KEY=$(openssl rand -hex 32)
  REDIS_PASSWORD=$(openssl rand -hex 16)
  # Fernet key for the secrets vault (32 bytes, url-safe base64)
  SECRETS_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null \
      || openssl rand -base64 32 | tr '+/' '-_' | cut -c1-44)
  INTERNAL_BOOTSTRAP_SECRET=$(openssl rand -hex 32)

  sed_i "s|^LOCAL_AUTH_TOKEN=.*|LOCAL_AUTH_TOKEN=$LOCAL_AUTH_TOKEN|" .env
  sed_i "s|^DB_PASSWORD=.*|DB_PASSWORD=$DB_PASSWORD|" .env
  sed_i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$JWT_SECRET_KEY|" .env
  sed_i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$REDIS_PASSWORD|" .env
  sed_i "s|^SECRETS_ENCRYPTION_KEY=.*|SECRETS_ENCRYPTION_KEY=$SECRETS_ENCRYPTION_KEY|" .env
  sed_i "s|^INTERNAL_BOOTSTRAP_SECRET=.*|INTERNAL_BOOTSTRAP_SECRET=$INTERNAL_BOOTSTRAP_SECRET|" .env

  # Host-specific values the containers need at runtime:
  # HOST_UID → tmux socket path for the host-pty bridge; MC_REPO_PATH →
  # absolute repo path for cross-image runtime switches (see docker-compose.yml).
  sed_i "s|^HOST_UID=.*|HOST_UID=$(id -u)|" .env
  sed_i "s|^MC_REPO_PATH=.*|MC_REPO_PATH=$(pwd)|" .env
  # PUID/PGID → the backend runs as YOUR uid/gid so it can write the ~/.mc
  # bind mount (Linux only; Docker Desktop on macOS maps uids by itself, there
  # they stay empty = unchanged image default).
  if [ "$(uname -s)" = "Linux" ]; then
    sed_i "s|^PUID=.*|PUID=$(id -u)|" .env
    sed_i "s|^PGID=.*|PGID=$(id -g)|" .env
  fi

  echo "✅ .env created with generated secrets"
  echo "   LOCAL_AUTH_TOKEN:       ${LOCAL_AUTH_TOKEN:0:8}..."
  echo "   DB_PASSWORD:            ${DB_PASSWORD:0:8}..."
  echo "   JWT_SECRET_KEY:         ${JWT_SECRET_KEY:0:8}..."
  echo "   REDIS_PASSWORD:         ${REDIS_PASSWORD:0:8}..."
  echo "   SECRETS_ENCRYPTION_KEY: ${SECRETS_ENCRYPTION_KEY:0:8}..."
  echo "   INTERNAL_BOOTSTRAP_SECRET: ${INTERNAL_BOOTSTRAP_SECRET:0:8}..."
  echo "   HOST_UID:               $(id -u)"
  echo "   MC_REPO_PATH:           $(pwd)"
  if [ "$(uname -s)" = "Linux" ]; then
    echo "   PUID / PGID:            $(id -u) / $(id -g)"
  fi
fi

# Backfill the owner-only permission for .env files that predate this check
# (upgrades, hand-copied .env.example) — the fresh-install branch above never
# runs for those. chmod only flips permission bits; it leaves the file (and
# its inode) exactly as it was — never `sed -i`/rewrite .env here.
chmod 600 .env

# Backfill host-specific keys for .env files that predate them (upgrades /
# hand-copied .env.example) — the fresh-install branch above never runs then.
grep -q '^HOST_UID=' .env || { echo "HOST_UID=$(id -u)" >> .env; echo "✅ backfilled HOST_UID=$(id -u)"; }
grep -q '^MC_REPO_PATH=' .env || { echo "MC_REPO_PATH=$(pwd)" >> .env; echo "✅ backfilled MC_REPO_PATH=$(pwd)"; }
# PUID/PGID (Linux): an .env from before the key existed has no line at all;
# an empty line is left alone (it is the documented "image default" choice).
if [ "$(uname -s)" = "Linux" ]; then
  grep -q '^PUID=' .env || { echo "PUID=$(id -u)" >> .env; echo "✅ backfilled PUID=$(id -u)"; }
  grep -q '^PGID=' .env || { echo "PGID=$(id -g)" >> .env; echo "✅ backfilled PGID=$(id -g)"; }
fi
# Ohne dieses Secret antwortet /api/v1/internal/bootstrap mit 401 und JEDER
# Agent-Container bricht beim Start ab (docker/*/entrypoint.sh). Ein .env aus
# der Zeit vor PR #404 hat den Key nicht — der Fresh-Install-Zweig oben laeuft
# dann nicht, also hier nachziehen. Danach `bash scripts/start-all.sh`, das
# schreibt docker/.env.shared neu, ueber das die Container den Wert bekommen.
grep -q '^INTERNAL_BOOTSTRAP_SECRET=' .env || { echo "INTERNAL_BOOTSTRAP_SECRET=$(openssl rand -hex 32)" >> .env; echo "✅ backfilled INTERNAL_BOOTSTRAP_SECRET (neu erzeugt — danach scripts/start-all.sh ausfuehren)"; }

# Your own agent fleet. Same pattern as .env from .env.example above, and for
# the same reason: docker/docker-compose.agents.yml has to exist BEFORE the
# first agent is created, or the first runtime bind fails inside a background
# task where nobody sees the error. It is not in version control — it rewrites
# itself at runtime and describes your machine.
bash scripts/ensure-agents-yml.sh

# Preflight: create deliverables dirs as the host user (so the first
# `docker compose up` does not create them as root).
if [ -x scripts/init-mc-deliverables-dirs.sh ]; then
  echo ""
  echo "🗂️  Initializing ~/.mc/deliverables/..."
  bash scripts/init-mc-deliverables-dirs.sh
fi
# Same reason for the other bind-mount source the compose file names directly
# (Docker would create a missing one as root on Linux).
mkdir -p "${HOME}/.mc/mcp-screenshots"

echo ""
echo "Next steps:"
echo "  1. Start the stack:    docker compose up --build -d"
echo "     (database migrations run automatically inside the backend)"
echo "  2. Open $(mc_access_url .env) and register the first admin user"
echo "     (POST /api/v1/auth/register works only while no user exists)."
# No browser on this machine: MC stays on localhost until an admin exists
# (anyone on the network could claim it before). install.sh does both steps.
if [ -z "${MC_SETUP_VIA_INSTALLER:-}" ] && mc_is_headless \
  && [ "$(mc_access_url .env)" = "http://localhost" ]; then
  mc_print_tunnel_hint
  mc_print_open_command
fi
echo ""
echo "Optional integrations (Discord, Telegram, voice) are configured via"
echo ".env — every key is documented in .env.example. GitHub can also be"
echo "connected later in the app under Settings → GitHub."
echo ""
echo "To run CLI agents (docs/setup/first-agent.md): start the host bridge"
echo "  make bridge-install               # managed service on 127.0.0.1:18792"
echo "  (or: python3 scripts/cli-bridge.py &)"
echo "  It creates its shared token in ~/.mc/secrets/cli-bridge.token (0600);"
echo "  the backend reads the same file — details: docs/setup/cli-bridge-auth.md"
echo ""
