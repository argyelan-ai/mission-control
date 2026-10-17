#!/usr/bin/env bash
# test-host-uid.sh — prove the backend works when ~/.mc belongs to a uid != 1000.
#
# Runs the backend image against a throwaway Docker volume owned by uid 1001
# (a named volume lives on the Docker host's own Linux filesystem, so the
# real uid rules apply — also under Docker Desktop on macOS, where bind mounts
# would hide the problem). Nothing of the running stack is touched: no
# compose project, no bind mount of your ~/.mc, the volume is removed at the end.
#
#   bash scripts/test-host-uid.sh                    # image: ghcr.io/argyelan-ai/mc-backend:latest
#   MC_TEST_IMAGE=mission-control-backend bash scripts/test-host-uid.sh
#
# The entrypoint and backend/app of THIS checkout are mounted over the image,
# so an older published image tests the current code. Cases:
#   A  no PUID, ~/.mc owned by 1001 → check reports "not writable" + PUID hint
#      (the measured bug; also the sabotage probe for case B)
#   B  PUID=1001 PGID=1001         → writable, created dirs owned by 1001
#   C  no PUID                     → process still runs as uid 1000 (macOS default unchanged)
#   D  root-owned deliverables dir (what Docker creates for a missing bind
#      source) + PUID              → entrypoint hands it to 1001
#   E/F PUID=0 / PGID=0            → refused with a clear message
set -euo pipefail

IMAGE="${MC_TEST_IMAGE:-ghcr.io/argyelan-ai/mc-backend:latest}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
VOL="mc-uidtest-$$"
FAILS=0
# The entrypoint re-executes itself after dropping root, so it needs the exec
# bit (the image COPYs it with 755; a checkout may not have it).
TMPD="$(mktemp -d)"
cp "$REPO/backend/docker-entrypoint.sh" "$TMPD/docker-entrypoint.sh"
chmod 755 "$TMPD/docker-entrypoint.sh"

cleanup() { docker volume rm -f "$VOL" >/dev/null 2>&1 || true; rm -rf "$TMPD"; }
trap cleanup EXIT

pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }

# Run the image as root (like the new image does by default) with this
# checkout's entrypoint + app code; extra args = env flags, then the command.
run_mc() {
  docker run --rm --user 0 \
    -e HOME_HOST=/hosthome -e MC_SKIP_MIGRATIONS=1 \
    -v "$VOL:/hosthome" \
    -v "$TMPD/docker-entrypoint.sh:/docker-entrypoint.sh:ro" \
    -v "$REPO/backend/app:/app/app:ro" \
    --entrypoint /docker-entrypoint.sh \
    "$@"
}
# Plain helper container on the same volume (no entrypoint logic).
as_uid() { local who="$1"; shift; docker run --rm --user "$who" -v "$VOL:/hosthome" --entrypoint "$1" "$IMAGE" "${@:2}"; }

echo "image: $IMAGE"
docker volume create "$VOL" >/dev/null
as_uid 0 chown 1001:1001 /hosthome
as_uid 1001:1001 mkdir -p /hosthome/.mc

echo "A: ~/.mc owned by 1001, no PUID (today's failure)"
set +e
OUT_A=$(run_mc "$IMAGE" python -m app.services.mc_home_preflight 2>&1)
RC_A=$?
set -e
if [ "$RC_A" = 1 ] && printf '%s' "$OUT_A" | grep -q 'PUID=1001'; then
  pass "check fails readably and names PUID=1001"
else
  fail "expected exit 1 with a PUID=1001 hint (rc=$RC_A): $OUT_A"
fi

echo "B: PUID=1001 PGID=1001"
set +e
OUT_B=$(run_mc -e PUID=1001 -e PGID=1001 "$IMAGE" python -m app.services.mc_home_preflight 2>&1)
RC_B=$?
set -e
OWNER_B=$(as_uid 1001 stat -c %u /hosthome/.mc/vault 2>/dev/null || echo missing)
if [ "$RC_B" = 0 ] && [ "$OWNER_B" = 1001 ]; then
  pass "writable, ~/.mc/vault created and owned by 1001"
else
  fail "rc=$RC_B owner=$OWNER_B: $OUT_B"
fi

echo "C: no PUID → uid stays 1000"
UID_C=$(run_mc "$IMAGE" id -u 2>/dev/null | tail -1 || true)
[ "$UID_C" = 1000 ] && pass "process uid 1000 (unchanged default)" || fail "process uid $UID_C"
UID_C2=$(run_mc -e PUID=1001 -e PGID=1001 "$IMAGE" id -u 2>/dev/null | tail -1 || true)
[ "$UID_C2" = 1001 ] && pass "with PUID the process runs as 1001" || fail "process uid $UID_C2 with PUID=1001"

echo "D: root-owned deliverables/<slug> (Docker-created bind source)"
as_uid 0 mkdir -p /hosthome/.mc/deliverables/agent-x
as_uid 0 chown 0:0 /hosthome/.mc/deliverables /hosthome/.mc/deliverables/agent-x
run_mc -e PUID=1001 -e PGID=1001 "$IMAGE" true >/dev/null 2>&1 || true
OWNER_D=$(as_uid 1001 stat -c %u /hosthome/.mc/deliverables/agent-x || echo missing)
[ "$OWNER_D" = 1001 ] && pass "entrypoint gave it to 1001" || fail "still owned by $OWNER_D"

echo "E: PUID=0 is refused"
set +e
OUT_E=$(run_mc -e PUID=0 "$IMAGE" true 2>&1); RC_E=$?
set -e
[ "$RC_E" != 0 ] && printf '%s' "$OUT_E" | grep -q 'PUID=0' && pass "refused with a clear message" || fail "rc=$RC_E: $OUT_E"

echo "F: PGID=0 is refused"
set +e
OUT_F=$(run_mc -e PUID=1001 -e PGID=0 "$IMAGE" true 2>&1); RC_F=$?
set -e
[ "$RC_F" != 0 ] && printf '%s' "$OUT_F" | grep -q 'PGID=0' && pass "refused with a clear message" || fail "rc=$RC_F: $OUT_F"

echo
if [ "$FAILS" = 0 ]; then echo "host-uid test OK"; else echo "host-uid test: $FAILS failure(s)"; exit 1; fi
