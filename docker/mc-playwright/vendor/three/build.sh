#!/usr/bin/env bash
# Rebuild the pinned three.js the bench recorder serves locally (lib:three-local).
#
# The recorder blocks every network request (record_network.py). A page that
# asks a CDN for three.js gets THIS copy instead — the same one for every
# contestant. Nothing here is fetched at image build or at record time.
#
#   three.module.min.js   upstream build/three.module.min.js, verbatim (ES module)
#   three.global.min.js   classic <script> build: window.THREE = three + addons
#                         (r160+ no longer ships build/three.min.js, so it is
#                         bundled here with esbuild)
#   addons/<dir>/<File>.js  upstream examples/jsm files, verbatim — the small
#                         set models use for scenes that record well (orbit
#                         camera, bloom post-processing, noise, sky). Any addon
#                         not in this folder stays blocked.
#   LICENSE               three.js MIT licence
#   MANIFEST.json         versions + sha256 of every served file
#
# Usage: bash docker/mc-playwright/vendor/three/build.sh   (needs node + npm)
set -euo pipefail

THREE_VERSION="0.170.0"
ESBUILD_VERSION="0.24.0"
REV="$(echo "$THREE_VERSION" | cut -d. -f2)"
ADDONS=(
  controls/OrbitControls.js
  postprocessing/EffectComposer.js
  postprocessing/RenderPass.js
  postprocessing/UnrealBloomPass.js
  postprocessing/OutputPass.js
  postprocessing/ShaderPass.js
  postprocessing/Pass.js
  postprocessing/MaskPass.js
  shaders/CopyShader.js
  shaders/LuminosityHighPassShader.js
  shaders/OutputShader.js
  math/ImprovedNoise.js
  math/SimplexNoise.js
  geometries/RoundedBoxGeometry.js
  utils/BufferGeometryUtils.js
  objects/Sky.js
  environments/RoomEnvironment.js
)

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

(cd "$WORK" && npm pack "three@${THREE_VERSION}" >/dev/null && tar xzf "three-${THREE_VERSION}.tgz")
PKG="$WORK/package"

rm -rf "$HERE/addons"
cp "$PKG/build/three.module.min.js" "$HERE/three.module.min.js"
cp "$PKG/LICENSE" "$HERE/LICENSE"
for a in "${ADDONS[@]}"; do
  mkdir -p "$HERE/addons/$(dirname "$a")"
  cp "$PKG/examples/jsm/$a" "$HERE/addons/$a"
done

# Classic global build: a plain (writable) object, like the old build/three.js.
mkdir -p "$WORK/build/node_modules"
mv "$PKG" "$WORK/build/node_modules/three"
{
  echo "export * from 'three';"
  for a in "${ADDONS[@]}"; do echo "export * from 'three/addons/$a';"; done
} > "$WORK/build/all.js"
cat > "$WORK/build/entry.js" <<'EOF'
import * as NS from './all.js';
window.THREE = Object.assign({}, NS);
EOF
(cd "$WORK/build" && npx --yes "esbuild@${ESBUILD_VERSION}" entry.js --bundle --minify \
  --format=iife --log-level=warning \
  --banner:js="/* three.js r${REV} + addons, classic global build for the bench recorder. Copyright 2010-2024 Three.js Authors. SPDX-License-Identifier: MIT */" \
  --outfile="$HERE/three.global.min.js")

python3 - "$HERE" "$THREE_VERSION" "$ESBUILD_VERSION" <<'EOF'
import hashlib, json, sys
from pathlib import Path
here, version, esb = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
files = sorted(p for p in here.rglob("*.js"))
manifest = {
    "three_version": version,
    "revision": version.split(".")[1],
    "esbuild_version": esb,
    "source": f"npm three@{version}",
    "files": {str(p.relative_to(here)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
}
(here / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
EOF
echo "three.js r${REV} vendored into $HERE"
