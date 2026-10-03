#!/usr/bin/env bash
# Sabotage tests for docker/cdp-browser/healthcheck.sh.
#
# Runs against a mock CDP server (test_mock_cdp_server.py) that fakes the
# HTTP /json/version endpoint and the WS Target.getTargets roundtrip, so
# these tests need only websocat + python3 + bash — no real Chromium.
# Wired into CI via .github/workflows/ci.yml ("Docker Build Check" job),
# same pattern as backend/tests/test_context_detect.sh: mount into a real
# alpine container instead of trusting the Ubuntu host's own tools.
#
# W2 (Rex' review on PR #544): `websocat -1 -n` reads exactly ONE WS
# message and exits. If Chromium sends an unsolicited event before the
# id-matched response, the check reads the event, greps for "id":1, and
# fails even though the real answer was 0.2s away (Rex measured exit 1
# after 0.16s while the response arrived at 0.36s). test_event_before_
# response_stays_green is the regression guard for the fix (dropping -1
# so websocat keeps forwarding messages until the timeout, see
# healthcheck.sh's comment for the full reasoning).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HEALTHCHECK="${HEALTHCHECK_BIN:-$HERE/healthcheck.sh}"
MOCK="$HERE/test_mock_cdp_server.py"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

wait_for_port() {
    local port="$1" i
    for i in $(seq 1 50); do
        (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
        sleep 0.1
    done
    return 1
}

# run_case NAME MODE EXPECTED_EXIT MAX_SECONDS
run_case() {
    local name="$1" mode="$2" expected="$3" max_seconds="$4" port server_pid
    port=$((10000 + (RANDOM % 20000)))

    python3 "$MOCK" "$port" "$mode" &
    server_pid=$!
    wait_for_port "$port" || { kill "$server_pid" 2>/dev/null || true; fail "$name: mock server never came up on $port"; }

    local start end elapsed rc
    start=$(date +%s)
    set +e
    # These cases test the CDP roundtrip only — the mock server doesn't run
    # cdp-gateway on :9300, so skip that leg here (covered separately by
    # test_gateway_check_* below).
    CDP_HOST="127.0.0.1:$port" GATEWAY_CHECK=0 "$HEALTHCHECK"
    rc=$?
    set -e
    end=$(date +%s)
    elapsed=$((end - start))

    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true

    [ "$rc" -eq "$expected" ] || fail "$name: expected exit $expected, got $rc (${elapsed}s)"
    [ "$elapsed" -le "$max_seconds" ] || fail "$name: took ${elapsed}s, expected <= ${max_seconds}s"
    pass "$name (exit $rc, ${elapsed}s)"
}

test_healthy_roundtrip_is_green() {
    run_case "healthy roundtrip" "healthy" 0 3
}

test_event_before_response_stays_green() {
    # The exact W2 case: an unsolicited event arrives ~0.2s before the
    # id-matched response. Must not false-positive to unhealthy.
    run_case "event-before-response (W2)" "event-first" 0 3
}

test_no_response_still_times_out_red() {
    # No response ever, only events — must still fail, and the 5s WS
    # timeout (unchanged by the W2 fix) must still be what kills it.
    # Bound generously (8s = 3s HTTP + 5s WS worst case) so this doesn't
    # flake on a loaded CI runner.
    run_case "no-response wedge" "no-response" 1 8
}

test_healthy_roundtrip_is_green
test_event_before_response_stays_green
test_no_response_still_times_out_red

# ── PR B1: the gateway leg (GATEWAY_CHECK=1, the real default) ────────────

start_fake_gateway_health() {
    # Minimal stdlib HTTP server answering GET /mc/health 200, on the exact
    # port healthcheck.sh probes (9300 is hardcoded there — same as Chromium's
    # debug port is effectively fixed per-container).
    # stdout/stderr MUST be redirected away from the parent's terminal here:
    # this function's output is captured via `gw_pid=$(start_fake_gateway_health 9300)`,
    # and a background child that inherits that $()-pipe's stdout keeps it
    # open forever (it's a server, it never exits) — so the command
    # substitution never sees EOF and hangs the whole test suite (found by
    # running this under `bash -x`: it froze silently right after this call
    # with no further trace output, every time).
    local port="$1"
    python3 - "$port" >/dev/null 2>&1 <<'PY' &
import http.server, sys
port = int(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
    echo $!
}

test_gateway_check_green_when_gateway_healthy() {
    local cdp_port gw_pid rc
    cdp_port=$((10000 + (RANDOM % 20000)))
    python3 "$MOCK" "$cdp_port" healthy &
    local mock_pid=$!
    wait_for_port "$cdp_port" || fail "gateway-green: mock CDP never came up"
    gw_pid=$(start_fake_gateway_health 9300)
    wait_for_port 9300 || fail "gateway-green: fake gateway never came up"

    set +e
    CDP_HOST="127.0.0.1:$cdp_port" "$HEALTHCHECK"
    rc=$?
    set -e
    kill "$mock_pid" "$gw_pid" 2>/dev/null || true
    wait "$mock_pid" "$gw_pid" 2>/dev/null || true
    [ "$rc" -eq 0 ] || fail "gateway-green: expected exit 0, got $rc"
    pass "gateway check green when gateway answers /mc/health"
}

test_gateway_check_red_when_gateway_dead() {
    # Sabotage probe for the GATEWAY_CHECK addition itself: CDP side is
    # perfectly healthy, nothing listens on 9300 -> must still fail red.
    local cdp_port rc
    cdp_port=$((10000 + (RANDOM % 20000)))
    python3 "$MOCK" "$cdp_port" healthy &
    local mock_pid=$!
    wait_for_port "$cdp_port" || fail "gateway-red: mock CDP never came up"

    set +e
    CDP_HOST="127.0.0.1:$cdp_port" "$HEALTHCHECK"
    rc=$?
    set -e
    kill "$mock_pid" 2>/dev/null || true
    wait "$mock_pid" 2>/dev/null || true
    [ "$rc" -ne 0 ] || fail "gateway-red: expected non-zero exit with no gateway listening, got 0"
    pass "gateway check red when gateway is unreachable (sabotage probe)"
}

test_gateway_check_green_when_gateway_healthy
test_gateway_check_red_when_gateway_dead

echo "PASS: all healthcheck sabotage tests green"
