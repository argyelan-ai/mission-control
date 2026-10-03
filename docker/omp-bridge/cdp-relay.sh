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
# Env
# ---
#   OMP_BROWSER_CDP_TARGET  host:port of the shared browser's CDP endpoint.
#                           Default `cdp-browser:9223`. Empty or `off` = no
#                           relay (omp keeps its own browser handling).
#   OMP_BROWSER_CDP_PORT    local relay port. Default 9222.
#
# Exit: always 0 — a missing browser must never stop the agent from booting.
set -u

TARGET="${OMP_BROWSER_CDP_TARGET-cdp-browser:9223}"
PORT="${OMP_BROWSER_CDP_PORT:-9222}"

case "$TARGET" in
    ""|off|none|0) exit 0 ;;
esac

if ! command -v socat >/dev/null 2>&1; then
    echo "[cdp-relay] WARN: socat fehlt im Image — kein Browser-Relay" >&2
    exit 0
fi

# Already running? (idempotent: the watchdog calls this every 30 s)
if pgrep -f "socat TCP-LISTEN:${PORT},bind=127.0.0.1" >/dev/null 2>&1; then
    exit 0
fi

nohup socat "TCP-LISTEN:${PORT},bind=127.0.0.1,fork,reuseaddr" "TCP:${TARGET}" \
    >/dev/null 2>&1 &
echo "[cdp-relay] 127.0.0.1:${PORT} -> ${TARGET} (pid $!)"
exit 0
