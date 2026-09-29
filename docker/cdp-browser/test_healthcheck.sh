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
    CDP_HOST="127.0.0.1:$port" "$HEALTHCHECK"
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

echo "PASS: all healthcheck sabotage tests green"
