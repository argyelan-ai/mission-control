#!/bin/bash
# start-all.sh — Startet Core-Services + Agent-Container in der richtigen Reihenfolge.
#
# Warum ein Script:
#   docker-compose.agents.yml braucht --env-file docker/.env.agents fuer die
#   MC_TOKEN_* Variablen. Aber --env-file ERSETZT die Standard-.env — dann
#   fehlt REDIS_PASSWORD und Redis crasht. Deshalb: Core zuerst (mit .env),
#   dann Agents (mit beiden env files).
#
# Usage:
#   ./scripts/start-all.sh          # Alles starten
#   ./scripts/start-all.sh --build  # Mit Backend-Rebuild

set -euo pipefail
cd "$(dirname "$0")/.."

BUILD_FLAG=""
if [ "${1:-}" = "--build" ]; then
    BUILD_FLAG="--build"
fi

echo "=== MC Start ==="

# 1. Core: DB, Redis, Backend, Qdrant, Caddy
echo "  [1/2] Core-Services..."
docker compose up -d $BUILD_FLAG 2>&1 | grep -E "Started|Healthy|Error" | sed 's/^/    /'

# Warten bis Backend healthy ist (abhaengig von DB + Redis)
# `timeout` existiert nicht standardmaessig auf macOS — native bash-loop:
echo "    Warte auf Backend..."
DEADLINE=$(($(date +%s) + 30))
until docker compose ps backend --format "{{.Health}}" 2>/dev/null | grep -q healthy; do
    if [ $(date +%s) -ge $DEADLINE ]; then
        echo "    Backend nicht healthy nach 30s — Logs:"
        docker compose logs backend --tail=10
        exit 1
    fi
    sleep 1
done

# 2. Agent-Tokens aus Vault generieren (falls vorhanden)
ENV_AGENTS="docker/.env.agents"
GENERATED=$(docker compose exec -T backend python3 -c "
import asyncio
import re
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.database import engine
from app.models.secret import Secret
from app.services.encryption import safe_decrypt

async def gen():
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Secret).where(Secret.provider == 'mc-agent'))
        lines = []
        for s in result.all():
            # Sanitize to a valid shell env-var name and align with the
            # compose renderer's key (slug.upper().replace('-', '_')). The
            # vault key is mc_token_{slug} (dash-form, e.g.
            # 'mc_token_host-testpilot') since the slug migration (0152);
            # sanitizing hyphens → underscores yields MC_TOKEN_HOST_TESTPILOT,
            # matching compose's \${MC_TOKEN_HOST_TESTPILOT}. Kept as defense
            # against any legacy space-form key that predates the migration.
            raw = s.key.replace('mc_token_', '').upper()
            name = re.sub(r'[^A-Z0-9_]', '_', raw)
            val = safe_decrypt(s.encrypted_value)
            if val:
                lines.append(f'MC_TOKEN_{name}={val}')
        print('\n'.join(sorted(lines)))

asyncio.run(gen())
" 2>/dev/null)

if [ -n "$GENERATED" ]; then
    echo "$GENERATED" > "$ENV_AGENTS"
    echo "  [2/3] Agent-Tokens aus Vault generiert ($(echo "$GENERATED" | wc -l | tr -d ' ') Tokens)"
else
    echo "  [2/3] Keine Vault-Tokens — verwende bestehende $ENV_AGENTS"
fi

# 2b. docker/.env.shared aus root .env regenerieren (CLAUDE_CODE_OAUTH_TOKEN,
# GH_TOKEN). Wird via env_file: in docker-compose.agents.yml geladen — macht
# Container-Recreate fail-safe gegen fehlende --env-file flags. Siehe SKILL
# mc-container-lifecycle.
ENV_SHARED="docker/.env.shared"
if [ -f .env ]; then
    grep -E "^(CLAUDE_CODE_OAUTH_TOKEN|GH_TOKEN)=" .env > "$ENV_SHARED"
    chmod 600 "$ENV_SHARED"
    echo "  [2b]  Shared Agent-Env regeneriert ($(wc -l < "$ENV_SHARED" | tr -d ' ') Vars)"
fi

# 3. Agent-Container (braucht BEIDE env files)
echo "  [3/3] Agent-Container..."
docker compose -f docker-compose.yml -f docker/docker-compose.agents.yml \
    --env-file .env --env-file docker/.env.agents \
    up -d 2>&1 | grep -E "Started|Running|Error" | sed 's/^/    /'

# 3. Status
echo ""
echo "=== Status ==="
docker ps --format "  {{.Names}}: {{.Status}}" | sort
echo ""
echo "Done."
