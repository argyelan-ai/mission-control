#!/bin/bash
# ensure-checkout.sh — stable code checkout for HOST agents.
#
# Problem class (three incarnations by 2026-07: April-stale SOUL via symlink,
# mc CLI symlink into a random branch, Boss inheriting the operator's current
# feature branch): host agents that read scripts/libs from the operator's own
# working checkout run whatever branch happens to be checked out there. This
# script gives them a dedicated checkout that is pinned to a fixed ref and
# never used for development.
#
#   MC_CHECKOUT_PATH  where the stable checkout lives
#                     (default: $HOME/.mc/checkouts/mission-control)
#   MC_CHECKOUT_REF   branch/ref to pin to (default: main)
#   MC_REPO_PATH      the operator's MC checkout — its `origin` remote URL is
#                     used as clone source, so forks and private mirrors work
#                     without extra config. Fallback: the official repo URL.
#
# Offline-tolerant: if the network is down, an existing checkout is used
# as-is (warning only); a missing checkout is a hard error.
#
# Usage:  ensure-checkout.sh   → prints the checkout path on stdout.
set -eu

CHECKOUT="${MC_CHECKOUT_PATH:-$HOME/.mc/checkouts/mission-control}"
REF="${MC_CHECKOUT_REF:-main}"
OFFICIAL_URL="https://github.com/argyelan-ai/mission-control.git"

log() { echo "[ensure-checkout] $*" >&2; }

remote_url() {
    # Prefer the origin of the operator's checkout (fork/mirror aware).
    if [ -n "${MC_REPO_PATH:-}" ] && [ -d "${MC_REPO_PATH}/.git" ]; then
        git -C "$MC_REPO_PATH" remote get-url origin 2>/dev/null && return 0
    fi
    echo "$OFFICIAL_URL"
}

if [ ! -d "$CHECKOUT/.git" ]; then
    URL="$(remote_url)"
    log "cloning $URL ($REF) -> $CHECKOUT"
    mkdir -p "$(dirname "$CHECKOUT")"
    if ! git clone --branch "$REF" --single-branch "$URL" "$CHECKOUT" >&2; then
        log "ERROR: clone failed and no existing checkout at $CHECKOUT"
        exit 1
    fi
else
    # Dedicated checkout, agents never commit here — hard reset is safe and
    # keeps it byte-identical to origin/$REF.
    if git -C "$CHECKOUT" fetch origin "$REF" >&2 2>&1; then
        git -C "$CHECKOUT" checkout -q "$REF" 2>/dev/null || true
        git -C "$CHECKOUT" reset -q --hard "origin/$REF"
        git -C "$CHECKOUT" clean -qfd
    else
        log "WARN: fetch failed (offline?) — using existing checkout as-is"
    fi
fi

echo "$CHECKOUT"
