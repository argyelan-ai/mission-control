#!/usr/bin/env bash
# Behaviour test, in every Caddyfile that ships:
#   - Caddyfile                    (the default install)
#   - caddy/Caddyfile.tls.example  (the HTTPS template operators copy)
# 1. The security headers Caddy adds must not overwrite a stricter value the
#    backend already chose.
# 2. Caddy's log lines never carry a credential from a URL.
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
#   redact  : a client that hangs up mid-download (a media player after its
#             first GET, a preview closed while loading) makes the proxy log
#             "aborting with incomplete response" with the request URI and
#             headers. That line must show neither the folder view's token
#             (the path segment after /deliverable-view/, in the URI and in
#             the Referer, and the token by its own shape `dv1.<a>.<b>`) nor
#             a `?ticket=` / `?token=` / `?access_token=` value.
#             Sabotage: put back the query-only `request>uri query` filter.
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
# A path ending in big.bin streams 32 MiB, so a client can hang up while the
# proxy is still copying the answer.
mkdir -p "$TMP/srv"
head -c 33554432 /dev/zero >"$TMP/srv/big.bin"
cat >"$TMP/Caddyfile.backend" <<'STUB'
:8000 {
	@view path /api/v1/deliverable-view/*
	header @view Referrer-Policy no-referrer
	@big path */big.bin
	handle @big {
		root * /srv
		rewrite * /big.bin
		file_server
	}
	handle {
		respond "ok" 200
	}
}
STUB
name="mc-caddy-hdr-backend-$$"
docker run -d --name "$name" --network "$NET" --network-alias backend \
    -v "$TMP/Caddyfile.backend:/etc/caddy/Caddyfile:ro" -v "$TMP/srv:/srv:ro" "$IMAGE" >/dev/null
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
    CADDY="$name"
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

# Stand-ins for credentials. Each one must never show up in Caddy's log.
VIEW_TOKEN="dv1.PATHTOKENCLAIMS00000000.PATHTOKENSIGNATURE000000"
REF_TOKEN="dv1.REFTOKENCLAIMS000000000.REFTOKENSIGNATURE0000000"
SHAPE_TOKEN="dv1.SHAPETOKENCLAIMS0000000.SHAPETOKENSIGNATURE00000"
SECRETS="PATHTOKEN REFTOKEN SHAPETOKEN TICKETVALUE QUERYTOKEN ACCESSVALUE"

# redaction: hang up early on a big answer, then read Caddy's own log.
redaction() {
    local base="$SCHEME://$HOST:$PORT" log secret uri_want ref_want i
    local path="/api/v1/deliverable-view/$VIEW_TOKEN/media/big.bin"
    local query="x=1&ticket=TICKETVALUE&token=QUERYTOKEN&access_token=ACCESSVALUE&next=%2Fv%2F$SHAPE_TOKEN%2Fa"
    curl -sk --resolve "mc-test.invalid:$PORT:127.0.0.1" \
        -H "Referer: $base/api/v1/deliverable-view/$REF_TOKEN/index.html" \
        "$base$path?$query" 2>/dev/null | head -c 1024 >/dev/null || :
    log=""
    for i in $(seq 1 50); do
        log="$(docker logs "$CADDY" 2>&1)"
        case "$log" in *'"uri"'*) break ;; esac
        sleep 0.2
    done
    case "$log" in
        *'"uri"'*) pass "redact: the hang-up wrote a log line with the request URI" ;;
        *) fail "redact: no log line with the request URI (the hang-up was not logged)"; return 0 ;;
    esac
    for secret in $SECRETS; do
        case "$log" in
            *"$secret"*) fail "redact: '$secret' is in Caddy's log" ;;
            *) pass "redact: '$secret' is not in Caddy's log" ;;
        esac
    done
    uri_want='"uri":"/api/v1/deliverable-view/REDACTED/media/big.bin?x=1&ticket=REDACTED&token=REDACTED&access_token=REDACTED&next=%2Fv%2FREDACTED%2Fa"'
    ref_want="\"Referer\":[\"$base/api/v1/deliverable-view/REDACTED/index.html\"]"
    case "$log" in
        *"$uri_want"*) pass "redact: the URI keeps its file path and other parameters" ;;
        *) fail "redact: logged URI is not $uri_want" ;;
    esac
    case "$log" in
        *"$ref_want"*) pass "redact: the Referer keeps its file path" ;;
        *) fail "redact: logged Referer is not $ref_want" ;;
    esac
}

run_cases() {
    expect "keep: the folder view keeps the backend's no-referrer" \
        "$(referrer /api/v1/deliverable-view/tok/index.html)" no-referrer
    expect "default: other answers get strict-origin-when-cross-origin" \
        "$(referrer /api/v1/tasks)" strict-origin-when-cross-origin
    redaction
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
