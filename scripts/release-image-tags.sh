#!/usr/bin/env bash
# release-image-tags.sh <git ref> — the image tags a release ref is promoted to.
#
# Used by .github/workflows/release.yml (promote job) and
# backend/tests/test_release_image_tags.py. Prints one tag per line:
#
#   refs/tags/v1.2.3      → 1.2.3, 1.2, latest   (stable release)
#   refs/tags/v1.2.3-rc1  → 1.2.3-rc1            (pre-release: never latest/X.Y)
#   anything else         → nothing              (branch run, loose tag like v0.5)
#
# The version is written WITHOUT the "v": install.sh pins MC_IMAGE_TAG to
# "${tag#v}" (mc_image_tag) — the two must stay in step.
set -euo pipefail

ref="${1:-}"
name="${ref#refs/tags/}"
[ "$name" != "$ref" ] || exit 0          # not a tag at all

if [[ "$name" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "${name#v}"
  echo "${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
  echo latest
elif [[ "$name" =~ ^v[0-9]+\.[0-9]+\.[0-9]+-[0-9A-Za-z.-]+$ ]]; then
  echo "${name#v}"
fi
