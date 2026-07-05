#!/usr/bin/env bash
# Build agent docker images.
#
# Syncs shared sources into each agent image's build context:
#   - scripts/mc-cli/                → {ctx}/mc-cli/           (gitignored working copy)
#   - docker/shared/poll.sh          → {ctx}/poll.sh           (gitignored working copy)
#   - docker/shared/recycler-lib.sh  → {ctx}/recycler-lib.sh   (gitignored working copy)
#
# Rationale: Dockerfile COPY paths must be local to the build context, so we
# keep ONE canonical source per shared file (mc-cli/, shared/poll.sh) and
# materialize a copy into each image dir just before `docker build`. This
# eliminated a Drift-Bug: PR #75 landed the Bracketed-Paste End-Marker fix
# only in mc-agent-base/poll.sh, not mc-claude-agent — a regression invisible
# until 8 claude-agents would hang on paste-mode.
#
# Usage:
#   scripts/build-agent-images.sh              # build both images
#   scripts/build-agent-images.sh claude       # only mc-claude-agent
#   scripts/build-agent-images.sh openclaude   # only mc-agent-base (Sparky)
#   scripts/build-agent-images.sh --no-cache   # pass-through to docker

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI_SRC="$ROOT/scripts/mc-cli"
SHARED_POLL_SRC="$ROOT/docker/shared/poll.sh"
SHARED_RECYCLER_LIB_SRC="$ROOT/docker/shared/recycler-lib.sh"
VERSIONS_MANIFEST="$ROOT/docker/cli-versions.json"

# Reads docker/cli-versions.json (Single Source of Truth for pinned CLI
# versions) and exports OPENCLAUDE_VERSION / CLAUDE_VERSION / OMP_VERSION /
# OMP_SHA256. Env-var overrides win over the manifest, so callers can still
# do `OMP_VERSION=16.3.0 scripts/build-agent-images.sh omp` without touching
# the file.
read_manifest() {
  local manifest_json
  manifest_json="$(python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
print(data["openclaude"]["version"])
print(data["claude"]["version"])
print(data["omp"]["version"])
print(data["omp"]["sha256"])
' "$VERSIONS_MANIFEST")"

  local manifest_openclaude manifest_claude manifest_omp manifest_omp_sha256
  { read -r manifest_openclaude; read -r manifest_claude; read -r manifest_omp; read -r manifest_omp_sha256; } <<<"$manifest_json"

  OPENCLAUDE_VERSION="${OPENCLAUDE_VERSION:-$manifest_openclaude}"
  CLAUDE_VERSION="${CLAUDE_VERSION:-$manifest_claude}"
  OMP_VERSION="${OMP_VERSION:-$manifest_omp}"
  OMP_SHA256="${OMP_SHA256:-$manifest_omp_sha256}"
}

read_manifest

WHICH="both"
DOCKER_ARGS=()
for arg in "$@"; do
  case "$arg" in
    claude|openclaude|omp|mc-omp-agent|both|all) WHICH="$arg" ;;
    -*|--*) DOCKER_ARGS+=("$arg") ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

sync_cli_into() {
  local dst="$1/mc-cli"
  rm -rf "$dst"
  mkdir -p "$dst"
  # Copy contents of mc-cli/, not the dir itself, so Dockerfile can COPY ./mc-cli
  cp -R "$CLI_SRC/." "$dst/"
  # Strip pyc / __pycache__ to keep the build context lean
  find "$dst" -type d -name __pycache__ -prune -exec rm -rf {} +
  find "$dst" -type f -name '*.pyc' -delete
}

sync_poll_into() {
  local dst="$1/poll.sh"
  cp "$SHARED_POLL_SRC" "$dst"
  chmod +x "$dst"
}

sync_recycler_lib_into() {
  local dst="$1/recycler-lib.sh"
  cp "$SHARED_RECYCLER_LIB_SRC" "$dst"
  # recycler-lib.sh is sourced (not executed), so the chmod is optional —
  # but keep it consistent with poll.sh.
  chmod +x "$dst"
}

build_image() {
  local tag="$1" ctx="$2"
  shift 2
  local version_args=("$@")
  echo "→ Syncing mc-cli into $ctx"
  sync_cli_into "$ctx"
  echo "→ Syncing shared poll.sh into $ctx"
  sync_poll_into "$ctx"
  echo "→ Syncing shared recycler-lib.sh into $ctx"
  sync_recycler_lib_into "$ctx"
  echo "→ Building $tag from $ctx"
  docker build "${version_args[@]}" "${DOCKER_ARGS[@]+"${DOCKER_ARGS[@]}"}" -t "$tag" "$ctx"
}

# omp image (ADR-045): a headless bridge, no poll.sh / recycler-lib.sh — it ships
# its own bridge.py + omp-recycler.sh. Only the mc CLI is materialised, so the
# `mc ack|finish|blocked` contract is byte-identical to the rest of the fleet.
build_image_omp() {
  local tag="$1" ctx="$2"
  shift 2
  local version_args=("$@")
  echo "→ Syncing mc-cli into $ctx"
  sync_cli_into "$ctx"
  echo "→ Building $tag from $ctx"
  docker build "${version_args[@]}" "${DOCKER_ARGS[@]+"${DOCKER_ARGS[@]}"}" -t "$tag" "$ctx"
}

case "$WHICH" in
  claude)
    build_image mc-claude-agent:latest "$ROOT/docker/mc-claude-agent" --build-arg "CLAUDE_VERSION=$CLAUDE_VERSION"
    ;;
  openclaude)
    build_image mc-agent-base:latest "$ROOT/docker/mc-agent-base" --build-arg "OPENCLAUDE_VERSION=$OPENCLAUDE_VERSION"
    ;;
  omp|mc-omp-agent)
    build_image_omp mc-omp-agent:latest "$ROOT/docker/omp-bridge" --build-arg "OMP_VERSION=$OMP_VERSION" --build-arg "OMP_SHA256=$OMP_SHA256"
    ;;
  both)
    build_image mc-claude-agent:latest "$ROOT/docker/mc-claude-agent" --build-arg "CLAUDE_VERSION=$CLAUDE_VERSION"
    build_image mc-agent-base:latest "$ROOT/docker/mc-agent-base" --build-arg "OPENCLAUDE_VERSION=$OPENCLAUDE_VERSION"
    ;;
  all)
    build_image mc-claude-agent:latest "$ROOT/docker/mc-claude-agent" --build-arg "CLAUDE_VERSION=$CLAUDE_VERSION"
    build_image mc-agent-base:latest "$ROOT/docker/mc-agent-base" --build-arg "OPENCLAUDE_VERSION=$OPENCLAUDE_VERSION"
    build_image_omp mc-omp-agent:latest "$ROOT/docker/omp-bridge" --build-arg "OMP_VERSION=$OMP_VERSION" --build-arg "OMP_SHA256=$OMP_SHA256"
    ;;
esac

echo "✓ Done."
