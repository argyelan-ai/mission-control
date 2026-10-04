#!/usr/bin/env bash
# docker/omp-bridge/cdp-relay.sh — omp's browser tool onto the shared agent
# browser (`cdp-browser` service, Chromium with WebGL).
#
# WHY THIS FILE EXISTS
# --------------------
# omp's built-in browser tool first tries to launch its OWN Chrome. On arm64
# that can never work ("Chrome for Testing does not provide linux/arm64"), so
# an omp agent had no browser at all — and nothing it did would ever show up
# in the operator's live browser panel, which watches `cdp-browser`.
#
# omp can attach to an existing browser instead (`browser.cdpUrl`, an HTTP CDP
# discovery URL). Pointing it at `http://cdp-browser:9223` fails, though:
# Chromium's DevTools endpoint rejects every Host header that is not an IP or
# `localhost` ("Host header is specified and is not an IP address or
# localhost") — the same wall playwright-mcp and the backend hit (see the
# `cdpnet` comment in docker-compose.yml and routers/browser_live.py).
#
# So this script runs a tiny local relay: socat listens on 127.0.0.1:<port>
# INSIDE the agent container and forwards every connection to the service.
# omp talks to `http://127.0.0.1:<port>` → the Host header is an IP, Chromium
# accepts it, and the WebSocket URL Chromium hands back
# (ws://127.0.0.1:<port>/devtools/browser/...) runs through the same relay.
# socat opens the target per connection (`fork`), so the service name is
# resolved fresh each time — a recreated cdp-browser with a new container IP
# needs no agent restart. No compose/network change, no IP in any config.
#
# render-omp-config.sh sets `browser.cdpUrl` to this relay (only when the
# target resolves); the entrypoint calls this script at boot and from its
# watchdog loop, so a dead relay comes back within 30 s.
#
# AGENT ATTRIBUTION (live finding 04.10.2026)
# -----------------------------------------
# Behind the default target sits cdp-gateway, which tells the operator's
# per-agent browser panel which tab belongs to which agent — from a
# `/a/<slug>/` prefix on the request path. Neither omp's cdpUrl prefix
# (Puppeteer drops it for /json/version) nor reverse DNS of this container
# (failed in the real network) delivered it, so every omp tab was "nobody's".
# When attribution is on, this script therefore starts cdp_relay.py instead
# of socat: same loopback listener, same per-connection target resolution,
# but it puts `/a/<slug>` in front of every request line (see cdp_relay.py).
# Attribution off (plain-Chromium target, no valid slug, or python3 /
# cdp_relay.py missing) keeps the exact pre-change socat relay.
#
# Env
# ---
#   OMP_BROWSER_CDP_TARGET  host:port of the shared browser's CDP endpoint.
#                           Default `cdp-browser:9300` (cdp-gateway, bauplan
#                           PR B1). Set to `cdp-browser:9223` to bypass the
#                           gateway: plain Chromium, socat relay, no per-agent
#                           attribution — the safe rollback.
#   OMP_BROWSER_CDP_ATTRIBUTION  auto (default: on for a :9300 target) | on | off
#   AGENT_SLUG / AGENT_NAME the agent's slug (compose sets both).
#   OMP_BROWSER_CDP_PORT    local relay port. Default 9222.
#
# Exit: always 0 — a missing browser must never stop the agent from booting.
set -u

TARGET="${OMP_BROWSER_CDP_TARGET-cdp-browser:9300}"
PORT="${OMP_BROWSER_CDP_PORT:-9222}"

case "$TARGET" in
    ""|off|none|0) exit 0 ;;
esac

_self="$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")"
RELAY_PY="$(cd "$(dirname "$_self")" && pwd)/cdp_relay.py"
AGENT_PATH=""
if command -v python3 >/dev/null 2>&1 && [ -f "$RELAY_PY" ]; then
    AGENT_PATH="$(OMP_BROWSER_CDP_TARGET="$TARGET" python3 "$RELAY_PY" --agent-path 2>/dev/null || true)"
fi

# Already running? (idempotent: the watchdog calls this every 30 s) — either
# flavour counts; the port can only have one listener anyway.
if pgrep -f "socat TCP-LISTEN:${PORT},bind=127.0.0.1" >/dev/null 2>&1 \
        || pgrep -f "cdp_relay.py --listen-port ${PORT} " >/dev/null 2>&1; then
    exit 0
fi

if [ -n "$AGENT_PATH" ]; then
    OMP_BROWSER_CDP_TARGET="$TARGET" nohup python3 "$RELAY_PY" --listen-port "$PORT" --target "$TARGET" \
        >/dev/null 2>&1 &
    echo "[cdp-relay] 127.0.0.1:${PORT} -> ${TARGET} (pid $!, Agent-Zuordnung ${AGENT_PATH})"
    exit 0
fi

if ! command -v socat >/dev/null 2>&1; then
    echo "[cdp-relay] WARN: socat fehlt im Image — kein Browser-Relay" >&2
    exit 0
fi

nohup socat "TCP-LISTEN:${PORT},bind=127.0.0.1,fork,reuseaddr" "TCP:${TARGET}" \
    >/dev/null 2>&1 &
echo "[cdp-relay] 127.0.0.1:${PORT} -> ${TARGET} (pid $!)"
exit 0
