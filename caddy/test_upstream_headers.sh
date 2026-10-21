#!/usr/bin/env bash
# Behaviour test: the security headers Caddy adds must not overwrite a
# stricter value the backend already chose, in every Caddyfile that sets them:
#   - Caddyfile                    (the default install)
#   - caddy/Caddyfile.tls.example  (the HTTPS template operators copy)
#
# Why: the HTML preview's folder view (backend routers/deliverable_view.py)
# and the bench viewer answer with `Referrer-Policy: no-referrer`, so the
# agent-written page they serve sends no Referer (MC's host name) to the CDNs
# it loads. The `(common)` header block is deferred (it deletes `Server`), so
# a plain `Referrer-Policy …` there REPLACED the backend's value at write
# time. `?Referrer-Policy …` only sets the default when the backend set none.
#
# Each Caddyfile runs in a throwaway Caddy container next to a stub "backend"
# (another Caddy) on a private Docker network. Nothing of the live stack is
# touched.
#
# What it pins down (and the sabotage that turns it red):
#   keep    : /api/v1/deliverable-view/* keeps the backend's no-referrer.
#             Sabotage: drop the `?` in front of Referrer-Policy.
#   default : a backend answer without the header still gets
#             strict-origin-when-cross-origin. Sabotage: delete the line.
#
# Needs: docker, curl, openssl. Usage: caddy/test_upstream_headers.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
IMAGE="${CADDY_IMAGE:-caddy:2-alpine}"
MAINT="$HERE/maintenance"

TMP="$(mktemp -d)"
NET="mc-caddy-hdr-test-$$"
CONTAINERS=""
cleanup() {
    for c in $CONTAINERS; do docker rm -f "$c" >/dev/null 2>&1 || :; done
    docker network rm "$NET" >/dev/null 2>&1 || :
    rm -rf "$TMP"
}
trap cleanup EXIT

FAILS=0
CASE_FILE=""
pass() { echo "PASS [$CASE_FILE] $1"; }
fail() { echo "FAIL [$CASE_FILE] $1" >&2; FAILS=$((FAILS + 1)); }
expect() { # expect LABEL ACTUAL WANTED
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: got '$2', want '$3'"; fi
}

docker network create "$NET" >/dev/null

# Stub backend: the view route sets no-referrer, everything else sets none.
cat >"$TMP/Caddyfile.backend" <<'STUB'
:8000 {
	@view path /api/v1/deliverable-view/*
	header @view Referrer-Policy no-referrer
	respond "ok" 200
}
STUB
name="mc-caddy-hdr-backend-$$"
docker run -d --name "$name" --network "$NET" --network-alias backend \
    -v "$TMP/Caddyfile.backend:/etc/caddy/Caddyfile:ro" "$IMAGE" >/dev/null
CONTAINERS="$CONTAINERS $name"

# start_caddy CADDYFILE CONTAINER_PORT [extra docker args...] → sets PORT
start_caddy() {
    local file="$1" cport="$2" name _ code
    shift 2
    docker run --rm -v "$file:/etc/caddy/Caddyfile:ro" -v "$MAINT:/etc/caddy/maintenance:ro" "$@" \
        "$IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >"$TMP/validate.log" 2>&1 \
        || { cat "$TMP/validate.log" >&2; fail "caddy validate"; return 1; }
    pass "caddy validate"
    name="mc-caddy-hdr-test-$$-$RANDOM"
    docker run -d --name "$name" --network "$NET" -p "127.0.0.1::$cport" \
        -v "$file:/etc/caddy/Caddyfile:ro" -v "$MAINT:/etc/caddy/maintenance:ro" "$@" \
        "$IMAGE" >/dev/null
    CONTAINERS="$CONTAINERS $name"
    PORT="$(docker port "$name" "$cport/tcp" | head -1 | sed 's/.*://')"
    for _ in $(seq 1 50); do
        code="$(curl -sk -o /dev/null -w '%{http_code}' --resolve "mc-test.invalid:$PORT:127.0.0.1" \
            "$SCHEME://$HOST:$PORT/api/v1/tasks" || :)"
        [ "$code" = "200" ] && return 0
        sleep 0.2
    done
    docker logs "$name" >&2 || :
    fail "caddy did not come up (last status $code)"
    return 1
}

# referrer PATH → the Referrer-Policy value(s), one per line
referrer() {
    curl -sk -o /dev/null -D - --resolve "mc-test.invalid:$PORT:127.0.0.1" "$SCHEME://$HOST:$PORT$1" \
        | tr -d '\r' | awk 'tolower($1)=="referrer-policy:"{print $2}' | paste -sd, -
}

run_cases() {
    expect "keep: the folder view keeps the backend's no-referrer" \
        "$(referrer /api/v1/deliverable-view/tok/index.html)" no-referrer
    expect "default: other answers get strict-origin-when-cross-origin" \
        "$(referrer /api/v1/tasks)" strict-origin-when-cross-origin
}

# 1. shipped default (plain http)
CASE_FILE="Caddyfile"
SCHEME=http HOST=127.0.0.1
if start_caddy "$REPO/Caddyfile" 80; then run_cases; fi

# 2. HTTPS template, made concrete: placeholder hostname + a throwaway cert
CASE_FILE="caddy/Caddyfile.tls.example"
SCHEME=https HOST=mc-test.invalid
mkdir -p "$TMP/certs"
sed 's/<your-hostname>/mc-test.invalid/g' "$HERE/Caddyfile.tls.example" >"$TMP/Caddyfile.tls"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=mc-test.invalid" \
    -keyout "$TMP/certs/mc-test.invalid.key" -out "$TMP/certs/mc-test.invalid.crt" >/dev/null 2>&1
chmod 0644 "$TMP/certs/"*
if start_caddy "$TMP/Caddyfile.tls" 443 -v "$TMP/certs:/etc/caddy/certs:ro"; then run_cases; fi

echo
if [ "$FAILS" -ne 0 ]; then
    echo "$FAILS check(s) failed" >&2
    exit 1
fi
echo "all upstream-header checks passed"
