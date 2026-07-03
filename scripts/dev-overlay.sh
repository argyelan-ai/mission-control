#!/usr/bin/env bash
# dev-overlay — apply/collect a private overlay repo onto this checkout.
#
# Maintainers (or anyone) can keep private verticals, local docs and
# machine-specific config in a separate private repo and sync it into the
# working tree. The overlay's paths are gitignored here, so they can never
# be committed to the public repo by accident.
#
#   scripts/dev-overlay.sh apply   <overlay-dir>   # overlay -> checkout
#   scripts/dev-overlay.sh collect <overlay-dir>   # checkout -> overlay
#   scripts/dev-overlay.sh status  <overlay-dir>   # show drift
#
# The overlay repo contains an `overlay.manifest` with lines:
#   <overlay-path> -> <checkout-path>
set -euo pipefail
MODE="${1:?apply|collect|status}"; OVERLAY="${2:?path to overlay repo}"
ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="$OVERLAY/overlay.manifest"
[ -f "$MANIFEST" ] || { echo "no overlay.manifest in $OVERLAY" >&2; exit 1; }

while IFS= read -r line; do
  case "$line" in \#*|"") continue ;; esac
  src="${line%% -> *}"; dst="${line##* -> }"
  case "$MODE" in
    apply)
      mkdir -p "$ROOT/$(dirname "$dst")"
      rsync -a --delete "$OVERLAY/$src" "$ROOT/$(dirname "$dst")/" 2>/dev/null \
        || rsync -a "$OVERLAY/$src" "$ROOT/$dst"
      ;;
    collect)
      if [ -e "$ROOT/$dst" ]; then
        mkdir -p "$OVERLAY/$(dirname "$src")"
        rsync -a --delete "$ROOT/$dst" "$OVERLAY/$(dirname "$src")/" 2>/dev/null \
          || rsync -a "$ROOT/$dst" "$OVERLAY/$src"
      fi
      ;;
    status)
      if [ -e "$ROOT/$dst" ] && [ -e "$OVERLAY/$src" ]; then
        diff -rq "$OVERLAY/$src" "$ROOT/$dst" >/dev/null 2>&1 || echo "DRIFT: $dst"
      elif [ -e "$OVERLAY/$src" ]; then echo "MISSING im Checkout: $dst"
      fi
      ;;
  esac
done < "$MANIFEST"
echo "dev-overlay: $MODE ok"
