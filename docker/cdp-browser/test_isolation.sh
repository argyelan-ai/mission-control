#!/usr/bin/env bash
# Real-Chromium test for browser-session isolation (ADR-088 isolation step):
# the built cdp-browser image (Chromium + cdp-gateway) with
# CDP_GATEWAY_ISOLATE=1, and isolation_probe.mjs run from the playwright-mcp
# image — a Puppeteer-pattern client (alpha) next to real Playwright (beta),
# in a throw-away Docker network.
#
# Checks: alpha never sees or reaches beta's tabs; beta's new tabs load
# although alpha auto-attaches with waitForDebuggerOnStart (the hang
# regression — the gateway must resume the foreign tabs it hides); cookies
# stay in their session; Browser.close from alpha leaves Chromium running;
# ending a session disposes its context. With the switch off (second run)
# alpha sees beta's tabs exactly as before.
#
# Chromium's flags are read from docker-compose.yml (the configuration that
# ships, like test_webgl.sh).
#
# Usage: test_isolation.sh [cdp-browser image] [playwright-mcp image] [compose-file]
# Sabotage knob (prove the test can fail):
#   CDP_TEST_GATEWAY=<file>   run this cdp_gateway.py instead of the image's
set -euo pipefail

CDP_IMAGE="${1:-mc-cdp-browser-test}"
PW_IMAGE="${2:-mc-playwright-mcp-test}"
COMPOSE="${3:-docker-compose.yml}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NET="cdp-isolation-test-$$"
NAME="cdp-isolation-test-$$"

FLAGS=()
while IFS= read -r f; do
  [ -n "$f" ] && FLAGS+=("$f")
done < <(python3 - "$COMPOSE" <<'PY'
import sys, yaml
svc = yaml.safe_load(open(sys.argv[1]))["services"]["cdp-browser"]
print("\n".join(a for a in svc["command"] if a != "about:blank"))
PY
)

GATEWAY_MOUNT=()
if [ -n "${CDP_TEST_GATEWAY:-}" ]; then
  GATEWAY_MOUNT=(-v "$CDP_TEST_GATEWAY:/opt/cdp-gateway/cdp_gateway.py:ro")
fi

# coreutils timeout on Linux; macOS has none, perl's alarm does the same.
tmo() { if command -v timeout >/dev/null; then timeout "$@"; else perl -e 'alarm shift; exec @ARGV' "$@"; fi; }

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

run() {  # run <isolate 0|1> <expect isolated|open>
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --init --network "$NET" -e "CDP_GATEWAY_ISOLATE=$1" \
    ${GATEWAY_MOUNT[@]+"${GATEWAY_MOUNT[@]}"} "$CDP_IMAGE" "${FLAGS[@]}" about:blank >/dev/null
  for _ in $(seq 1 30); do
    docker exec "$NAME" wget -qO- http://127.0.0.1:9300/mc/health >/dev/null 2>&1 && break
    sleep 1
  done
  echo "== CDP_GATEWAY_ISOLATE=$1 (expect $2)"
  if ! tmo 120 docker run --rm --network "$NET" -v "$HERE/isolation_probe.mjs:/t/probe.mjs:ro" \
      --entrypoint node "$PW_IMAGE" /t/probe.mjs "http://$NAME:9300" "$2"; then
    echo "--- gateway log" >&2
    docker logs "$NAME" 2>&1 | grep cdp_gateway | tail -20 >&2 || true
    return 1
  fi
}

docker network create "$NET" >/dev/null
run 1 isolated
run 0 open
echo "PASS"
