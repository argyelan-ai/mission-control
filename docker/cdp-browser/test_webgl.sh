#!/usr/bin/env bash
# Runtime test for the shared agent browser (cdp-browser): WebGL must work and
# must SURVIVE GPU-process crashes, and healthchecks must not leave zombies.
#
# Incident 2026-10-02: an agent built a WebGL scene and could never see it —
# the long-running cdp-browser had lost WebGL for good. Its log showed 63 x
# "GPU process exited unexpectedly: exit_code=9"; Chromium stops restarting
# the GPU process after 3 crashes and then reports getContext('webgl') = null
# until the browser restarts. `--disable-gpu-process-crash-limit` removes that
# limit. Same container: 71 716 zombie `timeout` processes from the
# healthcheck (orphans re-parented to Chromium as PID 1, never reaped) — PIDs
# come out of the Docker VM's shared pid space, so that is a slow fork bomb
# for every container. `init: true` puts a reaper in front.
#
# The flags and the init setting are read from docker-compose.yml, so this
# tests the configuration that actually ships, not a copy of it.
#
# Usage: test_webgl.sh [image] [compose-file]
# Sabotage knobs (prove the test can fail):
#   CDP_TEST_DROP_FLAG=--disable-gpu-process-crash-limit   remove one flag
#   CDP_TEST_NO_INIT=1                                     run without init
set -euo pipefail

IMAGE="${1:-mc-cdp-browser-test}"
COMPOSE="${2:-docker-compose.yml}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="cdp-webgl-test-$$"
GPU_KILLS=4   # one more than Chromium's default crash limit (3)

read_compose() {
  python3 - "$COMPOSE" "$1" <<'PY'
import sys, yaml
svc = yaml.safe_load(open(sys.argv[1]))["services"]["cdp-browser"]
if sys.argv[2] == "init":
    print("1" if svc.get("init") is True else "0")
else:
    print("\n".join(a for a in svc["command"] if a != "about:blank"))
PY
}

# while-read instead of mapfile: also runs on macOS' bash 3.2
FLAGS=()
while IFS= read -r f; do
  [ -n "$f" ] && [ "$f" != "${CDP_TEST_DROP_FLAG:-}" ] && FLAGS+=("$f")
done < <(read_compose flags)
INIT_ARG=()
if [ "$(read_compose init)" = "1" ] && [ -z "${CDP_TEST_NO_INIT:-}" ]; then
  INIT_ARG=(--init)
fi

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; docker logs "$NAME" 2>&1 | grep -v -e 'bus.cc' -e 'GPU stall' | tail -10 >&2 || true; exit 1; }

docker run -d --name "$NAME" ${INIT_ARG[@]+"${INIT_ARG[@]}"} \
  -v "$HERE/webgl-probe.html:/probe/webgl-probe.html:ro" \
  "$IMAGE" "${FLAGS[@]}" about:blank >/dev/null

cdp() { docker exec "$NAME" wget -qO- "http://127.0.0.1:9223$1" 2>/dev/null; }

for _ in $(seq 1 30); do cdp /json/version >/dev/null && break; sleep 1; done
cdp /json/version >/dev/null || fail "CDP endpoint never came up"

# Open the probe page via a browser-level CDP command (same WS path the
# healthcheck uses), then read the result from the page title over HTTP.
probe() {
  local ws title
  ws=$(cdp /json/version | sed -n 's/.*"webSocketDebuggerUrl": *"\([^"]*\)".*/\1/p')
  # timeout ends websocat (no -1, see healthcheck.sh); capture instead of
  # `grep -q` so pipefail doesn't trip on the SIGPIPE/timeout exit code.
  local resp
  resp=$(echo '{"id":1,"method":"Target.createTarget","params":{"url":"file:///probe/webgl-probe.html"}}' \
    | docker exec -i "$NAME" timeout 3 websocat -n "$ws" 2>/dev/null || true)
  case "$resp" in *'"id":1'*) ;; *) fail "Target.createTarget got no answer" ;; esac
  for _ in $(seq 1 20); do
    title=$(cdp /json/list | grep -o '"title": *"WEBGL=[^"]*"' | tail -1 | sed 's/.*"\(WEBGL=[^"]*\)"/\1/' || true)
    [ -n "$title" ] && break; sleep 0.5
  done
  # close the probe tabs again so the next probe reads a fresh title
  for id in $(cdp /json/list | grep -B8 'webgl-probe.html' | sed -n 's/.*"id": *"\([^"]*\)".*/\1/p' || true); do
    cdp "/json/close/$id" >/dev/null || true
  done
  echo "$title"
}

expect_webgl() {
  local got; got=$(probe)
  echo "  $1: $got"
  [ "$got" = "WEBGL=1 TRI=0,255,0,255 BG=255,0,0,255" ] || fail "$1: WebGL not rendering (got '${got:-nothing}')"
}

echo "cdp-browser WebGL test (image $IMAGE, init=${INIT_ARG[*]+${INIT_ARG[*]}})"
expect_webgl "fresh browser"

for i in $(seq 1 "$GPU_KILLS"); do
  docker exec -u root "$NAME" pkill -9 -f 'type=gpu-process' || true
  sleep 2
done
expect_webgl "after $GPU_KILLS GPU-process crashes"

for _ in 1 2 3 4 5; do docker exec "$NAME" /usr/local/bin/cdp-healthcheck.sh 2>/dev/null || fail "healthcheck red"; done
sleep 6   # let the orphaned `timeout 5 websocat` children exit
zombies=$(docker exec "$NAME" ps -o stat | grep -c '^Z' || true)
echo "  zombies after 5 healthchecks: $zombies"
[ "$zombies" -eq 0 ] || fail "$zombies zombie processes — nothing reaps healthcheck orphans (init: true missing?)"

echo "PASS"
