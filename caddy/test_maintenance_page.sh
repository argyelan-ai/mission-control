#!/usr/bin/env bash
# Behaviour test for the maintenance page (caddy/maintenance/503.html) and the
# `handle_errors` block that serves it, in every Caddyfile that ships it:
#   - Caddyfile                    (the default install)
#   - caddy/Caddyfile.tls.example  (the HTTPS template operators copy)
#   - e2e/stack/Caddyfile          (journeys stack)
#
# Each file runs in a throwaway Caddy container (same image as
# docker-compose.yml) with nothing behind it, so every upstream is a dial
# error — exactly what a stopped backend/frontend looks like during a restart.
# The real `caddy validate` runs first, so a syntax error fails here and not
# at deploy time.
#
# What it pins down (and the sabotage that turns each case red):
#   page     : a browser page load (Accept: text/html…) gets 502 + Retry-After
#              + the page. Sabotage: delete the handle_errors block.
#   bare     : fetch/API/script callers (Accept: application/json, */*, none)
#              get 502 + Retry-After with an EMPTY body — never 2 KB of HTML
#              in an error toast or an agent's tool error. Sabotage: drop the
#              `header Accept *text/html*` line from the page matcher.
#   retry    : Retry-After reaches API callers too. Sabotage: move the
#              `header … Retry-After` line into the page handle.
#   lang     : the page speaks the language MC itself would pick: the
#              NEXT_LOCALE cookie first, then the first supported language in
#              Accept-Language, case-insensitive (frontend-v2/src/i18n/
#              request.ts, pickLocale). Sabotage: read Accept-Language only.
#   internal : /api/v1/internal/* stays 403 (no error page, no proxy).
#
# Needs: docker, curl, openssl (for a dummy cert for the TLS template).
# Usage: caddy/test_maintenance_page.sh   (exit 0 = all green)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
IMAGE="${CADDY_IMAGE:-caddy:2-alpine}"
MAINT="$HERE/maintenance"

TMP="$(mktemp -d)"
CONTAINERS=""
cleanup() {
    for c in $CONTAINERS; do docker rm -f "$c" >/dev/null 2>&1 || :; done
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

NAV_ACCEPT='Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'

# start_caddy CADDYFILE [extra docker args...] → sets PORT
start_caddy() {
    local file="$1" name
    shift
    docker run --rm -v "$file:/etc/caddy/Caddyfile:ro" -v "$MAINT:/etc/caddy/maintenance:ro" "$@" \
        "$IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >"$TMP/validate.log" 2>&1 \
        || { cat "$TMP/validate.log" >&2; fail "caddy validate"; return 1; }
    pass "caddy validate"
    name="mc-caddy-maint-test-$$-$RANDOM"
    docker run -d --name "$name" -p 127.0.0.1::80 \
        -v "$file:/etc/caddy/Caddyfile:ro" -v "$MAINT:/etc/caddy/maintenance:ro" "$@" \
        "$IMAGE" >/dev/null
    CONTAINERS="$CONTAINERS $name"
    PORT="$(docker port "$name" 80/tcp | head -1 | sed 's/.*://')"
    local _ code
    for _ in $(seq 1 50); do
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || :)"
        [ "$code" != "000" ] && return 0
        sleep 0.2
    done
    docker logs "$name" >&2 || :
    fail "caddy did not come up"
    return 1
}

# probe PATH [curl args...] → sets CODE SIZE CTYPE RETRY LANG_ATTR
probe() {
    local path="$1"
    shift
    local out
    out="$(curl -s -o "$TMP/body" -D "$TMP/headers" -w '%{http_code} %{size_download}' \
        "$@" "http://127.0.0.1:$PORT$path")"
    CODE="${out% *}"
    SIZE="${out#* }"
    RETRY="$(tr -d '\r' <"$TMP/headers" | awk 'tolower($1)=="retry-after:"{print $2}')"
    CTYPE="$(tr -d '\r' <"$TMP/headers" | awk 'tolower($1)=="content-type:"{print $2}' | tr -d ';')"
    LANG_ATTR="$(grep -o '<html lang="[a-z]*">' "$TMP/body" | sed 's/.*lang="\([a-z]*\)".*/\1/' || :)"
}

run_cases() {
    # page — a browser navigation gets the page
    probe /settings -H "$NAV_ACCEPT" -H 'Accept-Language: en-US,en;q=0.9'
    expect "page: status" "$CODE" 502
    expect "page: Retry-After" "$RETRY" 5
    expect "page: content-type" "$CTYPE" text/html
    expect "page: rendered (lang attribute)" "$LANG_ATTR" en
    if grep -q 'http-equiv="refresh" content="5"' "$TMP/body"; then pass "page: reloads itself"; else fail "page: no meta refresh"; fi
    if grep -q '{{' "$TMP/body"; then fail "page: unrendered template markup in the body"; else pass "page: template fully rendered"; fi
    if grep -Eq 'id="mc-slow"[^>]*hidden' "$TMP/body"; then pass "page: slow hint present, hidden by default"; else fail "page: slow hint missing or visible without JS"; fi

    probe /api/v1/tasks -H "$NAV_ACCEPT"
    expect "page: also for an /api/* URL opened in the browser" "$CODE/$CTYPE" 502/text/html

    # bare — fetch / API / script callers get no HTML
    probe /api/v1/system/update/status -H 'Accept: application/json'
    expect "bare: JSON caller status" "$CODE" 502
    expect "bare: JSON caller body bytes" "$SIZE" 0
    expect "bare: JSON caller Retry-After" "$RETRY" 5
    probe /settings -H 'Accept: */*'
    expect "bare: fetch default (*/*) body bytes" "$CODE/$SIZE" 502/0
    expect "bare: fetch default Retry-After" "$RETRY" 5
    probe /api/v1/health -H 'Accept:'
    expect "bare: no Accept header (scripts) body bytes" "$CODE/$SIZE" 502/0
    probe /_next/data/x.json -H 'Accept: text/x-component'
    expect "bare: RSC fetch body bytes" "$CODE/$SIZE" 502/0
    probe /health -I -H "$NAV_ACCEPT"
    expect "HEAD: status + Retry-After" "$CODE/$RETRY" 502/5

    # internal stays closed
    probe /api/v1/internal/bootstrap -H "$NAV_ACCEPT"
    expect "internal: still 403" "$CODE" 403

    # lang — same choice as MC's pickLocale (cookie first, then first
    # supported Accept-Language tag, case-insensitive, default en)
    probe /settings -H "$NAV_ACCEPT" -H 'Accept-Language: en-US' -b NEXT_LOCALE=de
    expect "lang: cookie de beats en browser" "$LANG_ATTR" de
    probe /settings -H "$NAV_ACCEPT" -H 'Accept-Language: de-CH,de;q=0.9' -b NEXT_LOCALE=en
    expect "lang: cookie en beats de browser" "$LANG_ATTR" en
    probe /settings -H "$NAV_ACCEPT" -H 'Accept-Language: DE-CH'
    expect "lang: upper-case tag" "$LANG_ATTR" de
    probe /settings -H "$NAV_ACCEPT" -H 'Accept-Language: fr-CH, de;q=0.9, en;q=0.8'
    expect "lang: first supported tag (de after fr)" "$LANG_ATTR" de
    probe /settings -H "$NAV_ACCEPT" -H 'Accept-Language: fr-CH, en;q=0.9, de;q=0.8'
    expect "lang: first supported tag (en after fr)" "$LANG_ATTR" en
    probe /settings -H "$NAV_ACCEPT" -H 'Accept-Language: de' -b NEXT_LOCALE=xx
    expect "lang: unknown cookie value falls through" "$LANG_ATTR" de
    probe /settings -H "$NAV_ACCEPT"
    expect "lang: nothing known → en" "$LANG_ATTR" en
}

# 1. shipped default
CASE_FILE="Caddyfile"
if start_caddy "$REPO/Caddyfile"; then run_cases; fi

# 2. HTTPS template, made concrete: placeholder hostname + a throwaway cert
CASE_FILE="caddy/Caddyfile.tls.example"
mkdir -p "$TMP/certs"
sed 's/<your-hostname>/mc-test.invalid/g' "$HERE/Caddyfile.tls.example" >"$TMP/Caddyfile.tls"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=mc-test.invalid" \
    -keyout "$TMP/certs/mc-test.invalid.key" -out "$TMP/certs/mc-test.invalid.crt" >/dev/null 2>&1
chmod 0644 "$TMP/certs/"*
if start_caddy "$TMP/Caddyfile.tls" -v "$TMP/certs:/etc/caddy/certs:ro"; then run_cases; fi

# 3. journeys stack
CASE_FILE="e2e/stack/Caddyfile"
if start_caddy "$REPO/e2e/stack/Caddyfile"; then run_cases; fi

echo
if [ "$FAILS" -ne 0 ]; then
    echo "$FAILS check(s) failed" >&2
    exit 1
fi
echo "all maintenance-page checks passed"
