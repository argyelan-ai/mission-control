#!/usr/bin/env bash
# Open Mission Control to the network (laptop, phone) — AFTER the first admin
# exists. Before that, anyone on the network could register as admin.
#
#   bash scripts/open-network.sh [address]
#
# address: the IP or name.local other devices use (default: detected).
# With a terminal and no admin yet, it offers to create the admin here first
# (over localhost, password hidden). Writes MC_BIND_ADDRESS=0.0.0.0 and
# PUBLIC_HOST=<address> to .env, then `docker compose up -d`.
# Details and the safer Tailscale way: scripts/lib/remote-access.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && [ -f docker-compose.yml ] || { echo "✗ run inside an installed Mission Control directory." >&2; exit 1; }
# shellcheck source=scripts/lib/remote-access.sh
. ./scripts/lib/remote-access.sh

MC_INTERACTIVE=0
{ [ -e /dev/tty ] && : </dev/tty; } 2>/dev/null && MC_INTERACTIVE=1
export MC_INTERACTIVE

host="${1:-$(mc_detect_address)}"
[ -n "$host" ] || { echo "✗ could not detect this machine's address — pass it: bash scripts/open-network.sh <address>" >&2; exit 1; }
if ! mc_admin_exists; then
  mc_register_admin_tty .env || { echo "✗ no admin yet — nothing opened." >&2; exit 1; }
fi
mc_open_network "$host" .env
