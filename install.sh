#!/usr/bin/env bash
# Mission Control — one-line installer
#
#   curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
#
# Checks prerequisites, clones the LATEST RELEASE, generates secrets
# (setup.sh), pins the prebuilt images to that same release, boots the stack,
# runs migrations and waits until the API is healthy. Interactive when a
# terminal is available (asks install dir, operator name, optional profiles);
# falls back to safe defaults otherwise.
#
# Code and images always come from the same version. The prebuilt images on
# ghcr.io are only built for release tags (release.yml), so cloning `main`
# while pulling `:latest` would pair today's compose file with the code of the
# last release — e.g. a worker service whose module the old image lacks.
#
# Flags:
#   --channel <stable|edge>  stable (default): latest release tag + its
#                      prebuilt images. edge (alias: main): the main branch,
#                      images built locally — for developers, NOT recommended
#                      for users (unreleased, can break). Env: MC_CHANNEL.
#   --version <vX.Y.Z> install/update to exactly this release (stable).
#                      Env: MC_VERSION.
#   --here             install into the current directory (expects an
#                      existing checkout — used by CI to test this script).
#                      Always builds the checkout locally (edge semantics).
#   --dir <path>       target directory (default: ./mission-control)
#   --non-interactive  never prompt, use defaults/flags
#   --update           update an existing install (run inside the checkout):
#                      move to the latest release (or main on edge), refresh
#                      images, restart, migrate. Works from any version:
#                      curl -fsSL <url above> | bash -s -- --update
set -euo pipefail

REPO_URL="${MC_REPO_URL:-https://github.com/argyelan-ai/mission-control.git}"
RELEASE_API="${MC_RELEASE_API:-https://api.github.com/repos/argyelan-ai/mission-control/releases/latest}"
INSTALLER_URL="https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh"
FLEET_FILE="docker/docker-compose.agents.yml"

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Release helpers ──────────────────────────────────────────────────────────
# Pure functions, no side effects beyond stdout — backend/tests/
# test_install_release_pinning.py sources this file with
# MC_INSTALL_LIB_ONLY=1 and drives them directly.

# A release tag is strict vX.Y.Z. Pre-releases (v1.0.0-rc1) and loose tags
# (v0.5) are not releases: release.yml would not publish matching images.
mc_is_release_tag() { [[ "${1:-}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

# tag_name from a GitHub "releases/latest" JSON body on stdin (pretty-printed
# or minified). No jq dependency on purpose.
mc_tag_from_release_json() {
  sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed -n '1p'
}

# mc_version_gt vA vB — true when A is a strictly higher release than B.
# Numeric per field: v0.10.0 > v0.9.0 (a lexical sort gets this wrong).
mc_version_gt() {
  local a1 a2 a3 b1 b2 b3
  IFS=. read -r a1 a2 a3 <<<"${1#v}"
  IFS=. read -r b1 b2 b3 <<<"${2#v}"
  if ((10#$a1 != 10#$b1)); then ((10#$a1 > 10#$b1)); return; fi
  if ((10#$a2 != 10#$b2)); then ((10#$a2 > 10#$b2)); return; fi
  ((10#$a3 > 10#$b3))
}

# Highest strict release tag among the names on stdin (one per line).
mc_highest_release_tag() {
  local t best=""
  while IFS= read -r t; do
    mc_is_release_tag "$t" || continue
    if [ -z "$best" ] || mc_version_gt "$t" "$best"; then best="$t"; fi
  done
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

# The tag of the latest published release. GitHub API first (it knows which
# tag is a *published release*, not just a tag); when that fails — offline,
# rate-limited (60 requests/h per IP without a token), API outage — fall back
# to the highest vX.Y.Z tag in the repository via git. Returns 1 when both fail.
mc_latest_release_tag() {
  local json tag=""
  if json="$(curl -fsSL --max-time 15 -H 'Accept: application/vnd.github+json' "$RELEASE_API" 2>/dev/null)"; then
    tag="$(printf '%s\n' "$json" | mc_tag_from_release_json)"
    if mc_is_release_tag "$tag"; then printf '%s\n' "$tag"; return 0; fi
  fi
  warn "GitHub API returned no release (offline or rate-limited) — falling back to the repository's tags."
  tag="$(git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null \
          | sed 's|.*refs/tags/||' | mc_highest_release_tag)" || return 1
  printf '%s\n' "$tag"
}

# Strict release tags on stdin → newest first (numeric, no `sort -V`, which
# BSD/macOS sort lacks in older versions).
mc_sort_releases_desc() {
  local t i n tags=()
  while IFS= read -r t; do mc_is_release_tag "$t" && tags+=("$t"); done
  n=${#tags[@]}
  # insertion sort — a handful of tags
  for ((i = 1; i < n; i++)); do
    t="${tags[i]}"; local j=$((i - 1))
    while ((j >= 0)) && mc_version_gt "$t" "${tags[j]}"; do tags[j + 1]="${tags[j]}"; j=$((j - 1)); done
    tags[j + 1]="$t"
  done
  ((n == 0)) || printf '%s\n' "${tags[@]}"
}

# Registry the core images come from (override only for tests).
MC_IMAGE_REGISTRY="${MC_IMAGE_REGISTRY:-ghcr.io/argyelan-ai}"

# True when the core images (backend + frontend) of release <tag> exist on
# the registry — a manifest lookup, nothing is downloaded.
mc_images_published() {
  local v
  v="$(mc_image_tag "$1")"
  docker manifest inspect "$MC_IMAGE_REGISTRY/mc-backend:$v" >/dev/null 2>&1 \
    && docker manifest inspect "$MC_IMAGE_REGISTRY/mc-frontend:$v" >/dev/null 2>&1
}

# The release to install: the latest release whose images are published.
# A release is tagged before release.yml has finished (or after it failed);
# taking it would mean a local build on every install in that window. So a
# release without images is skipped for the next lower one (at most 3 back).
# When none has images (registry unreachable), the latest release is used —
# boot_stack then builds that same tag locally: slower, still consistent.
mc_pick_release() {
  local latest t n=0
  latest="$(mc_latest_release_tag)" || return 1
  while IFS= read -r t; do
    if mc_version_gt "$t" "$latest"; then continue; fi
    n=$((n + 1)); [ "$n" -le 4 ] || break
    if mc_images_published "$t"; then
      [ "$t" = "$latest" ] || warn "Release $latest has no published images yet (still building?) — installing $t instead."
      printf '%s\n' "$t"; return 0
    fi
  done < <( { printf '%s\n' "$latest"
              git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null | sed 's|.*refs/tags/||'
            } | mc_sort_releases_desc | uniq )
  warn "Could not confirm published images for $latest (registry unreachable?) — it will be built locally if the pull fails."
  printf '%s\n' "$latest"
}

# Image tag for a release tag: release.yml publishes semver WITHOUT the "v"
# (docker/metadata-action type=semver,pattern={{version}} → 0.2.0).
mc_image_tag() { printf '%s\n' "${1#v}"; }

mc_normalize_channel() {
  case "${1:-}" in
    stable|release) echo stable ;;
    edge|main|dev)  echo edge ;;
    *) return 1 ;;
  esac
}

# The release tag HEAD sits on exactly, or nothing (branch / between tags).
mc_current_release_tag() {
  local t
  t="$(git describe --tags --exact-match HEAD 2>/dev/null)" || return 1
  mc_is_release_tag "$t" || return 1
  printf '%s\n' "$t"
}

# .env helpers — replace-or-append without sed (a value containing sed
# metacharacters like & or \ would silently corrupt the .env otherwise).
# Written IN PLACE (`cat >`), never via `mv`/`sed -i`: backend and worker
# bind-mount .env as a single file, and a bind mount follows the inode — a
# replaced file (new inode) leaves the running containers reading the OLD
# one. backend/tests/test_install_release_pinning.py pins the inode.
mc_env_set() {
  local tmp
  tmp="$(mktemp)"
  grep -v "^$1=" .env > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}
mc_env_unset() {
  local tmp
  tmp="$(mktemp)"
  grep -v "^$1=" .env > "$tmp" 2>/dev/null || true
  cat "$tmp" > .env
  rm -f "$tmp"
}
mc_env_get() { if [ -f .env ]; then sed -n "s/^$1=//p" .env | tail -n 1; fi; }

# Pin channel + image tags in .env so `docker compose` resolves exactly the
# images that belong to the checked-out code.
#   stable → MC_IMAGE_TAG / MC_AGENT_IMAGE_TAG = release version (0.2.0)
#   edge   → MC_IMAGE_TAG=edge: no published image carries that tag, so the
#            core images are always the local build of this checkout and a
#            stray `docker compose pull` can never swap a release in under it.
#            Agent images stay on the default tag; build them locally with
#            scripts/build-agent-images.sh (local builds shadow the pull).
mc_write_pins() {
  local channel="$1" tag="${2:-}"
  mc_env_set MC_CHANNEL "$channel"
  if [ "$channel" = stable ]; then
    mc_env_set MC_IMAGE_TAG "$(mc_image_tag "$tag")"
    mc_env_set MC_AGENT_IMAGE_TAG "$(mc_image_tag "$tag")"
  else
    mc_env_set MC_IMAGE_TAG edge
    mc_env_unset MC_AGENT_IMAGE_TAG
  fi
}

# Does the migration chain of <ref> contain alembic revision <rev>? Used to
# refuse a switch that would put code BELOW the database (migrations are
# forward-only; an older backend cannot start on a newer schema).
mc_ref_has_revision() {
  local ref="$1" rev="$2"
  [[ "$rev" =~ ^[A-Za-z0-9_]+$ ]] || return 1
  git grep -qE "^revision(: *str)? *= *['\"]${rev}['\"]" "$ref" -- backend/alembic/versions 2>/dev/null
}

# The compose project name `docker compose` will use in the current
# directory: COMPOSE_PROJECT_NAME from the environment, then from .env, then
# the directory name — normalised the way compose does it.
mc_compose_project() {
  local p="${COMPOSE_PROJECT_NAME:-}"
  [ -n "$p" ] || p="$(mc_env_get COMPOSE_PROJECT_NAME)"
  [ -n "$p" ] || p="$(basename "$(pwd -P)")"
  printf '%s\n' "$p" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]//g'
}

# Never start a second Mission Control stack next to a running one — above
# all not next to the operator's own install. Two cases are refused:
#   * a stack with the SAME project name runs from ANOTHER directory: `up`
#     here would recreate its containers with this checkout's code;
#   * a stack with a DIFFERENT project name runs: a second stack fights it
#     for ports 80/8000 and doubles every agent.
# Only the stack of this very directory may be (re)started.
mc_guard_single_stack() {
  local proj here here_p p wd image
  proj="$(mc_compose_project)"
  here="$(pwd)"; here_p="$(pwd -P)"
  while IFS='|' read -r p wd image; do
    [ -n "$p" ] || continue
    case "$image" in *mc-backend*) ;; *) continue ;; esac
    if [ "$p" = "$proj" ] && { [ "$wd" = "$here" ] || [ "$wd" = "$here_p" ]; }; then continue; fi
    if [ "$p" = "$proj" ]; then
      fail "A Mission Control stack named '$p' is already running from ${wd:-another directory}. Starting here would replace its containers with this checkout. Update that install from its own directory, or set COMPOSE_PROJECT_NAME in this .env to a different name."
    fi
    fail "Another Mission Control stack ('$p' in ${wd:-an unknown directory}) is already running on this machine. A second stack would fight it for ports 80/8000. Stop it first (docker compose -p $p down) or install on another machine."
  done < <(docker ps --filter label=com.docker.compose.service=backend \
             --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Image}}' 2>/dev/null || true)
}

[ "${MC_INSTALL_LIB_ONLY:-0}" = 1 ] && return 0 2>/dev/null

# ── Arguments ────────────────────────────────────────────────────────────────
TARGET_DIR="./mission-control"
HERE=0
INTERACTIVE=1
UPDATE=0
CHANNEL_ARG="${MC_CHANNEL:-}"
VERSION="${MC_VERSION:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --here) HERE=1; INTERACTIVE=0 ;;
    --update) UPDATE=1; INTERACTIVE=0 ;;
    --dir) TARGET_DIR="$2"; shift ;;
    --channel) CHANNEL_ARG="$2"; shift ;;
    --version) VERSION="$2"; shift ;;
    --non-interactive) INTERACTIVE=0 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done
if [ -n "$CHANNEL_ARG" ]; then
  CHANNEL_ARG="$(mc_normalize_channel "$CHANNEL_ARG")" || fail "Unknown channel '${CHANNEL_ARG}' — use stable or edge."
fi
if [ -n "$VERSION" ]; then
  case "$VERSION" in v*) ;; *) VERSION="v$VERSION" ;; esac
  mc_is_release_tag "$VERSION" || fail "--version expects a release like v0.2.0, got '$VERSION'."
  [ "$CHANNEL_ARG" = edge ] && fail "--version and --channel edge contradict each other."
  CHANNEL_ARG=stable
fi
# `curl | bash` has no usable stdin — prompt via /dev/tty when present.
if [ "$INTERACTIVE" = 1 ] && { [ ! -e /dev/tty ] || ! : </dev/tty; } 2>/dev/null; then
  INTERACTIVE=0
fi

ask() { # ask "Frage" "default" -> REPLY
  local q="$1" d="$2"
  if [ "$INTERACTIVE" = 1 ]; then
    printf '%s [%s]: ' "$q" "$d" >/dev/tty
    IFS= read -r REPLY </dev/tty || REPLY=""
    [ -n "$REPLY" ] || REPLY="$d"
  else
    REPLY="$d"
  fi
}

say "🚀 Mission Control installer"
echo

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
# All checks first, then ONE report with a copy-paste fix per missing piece —
# not five rounds of "install X, re-run, now install Y". Nothing is installed
# automatically: the commands need sudo and change the system.
OS="$(uname -s)"
case "$OS" in Darwin|Linux) ;; *) fail "Unsupported platform '$OS'. On Windows, run this inside WSL2." ;; esac
DISTRO=""
if [ "$OS" = Linux ] && [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  DISTRO="$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")"
fi
PLATFORM=other
case "$OS:$DISTRO" in
  Darwin:*) PLATFORM=macos ;;
  *debian*|*ubuntu*|*raspbian*) PLATFORM=debian ;;
  *fedora*|*rhel*|*centos*) PLATFORM=fedora ;;
esac

fix_for() { # fix_for <tool> — print the install command(s) for this platform
  local d f m
  case "$1" in
    git)     d="sudo apt install -y git";     f="sudo dnf install -y git";     m="xcode-select --install" ;;
    curl)    d="sudo apt install -y curl";    f="sudo dnf install -y curl";    m="brew install curl" ;;
    openssl) d="sudo apt install -y openssl"; f="sudo dnf install -y openssl"; m="brew install openssl" ;;
    docker)
      d="curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker \$USER   # then log out and back in"
      f="curl -fsSL https://get.docker.com | sudo sh && sudo systemctl enable --now docker && sudo usermod -aG docker \$USER   # then log out and back in"
      m="brew install --cask docker   # then start Docker Desktop once (or: https://docs.docker.com/desktop/install/mac-install/)" ;;
    compose)
      d="sudo apt install -y docker-compose-plugin   # from Docker's apt repo; if apt cannot find it, install Docker via: curl -fsSL https://get.docker.com | sudo sh"
      f="sudo dnf install -y docker-compose-plugin   # from Docker's repo; or install Docker via: curl -fsSL https://get.docker.com | sudo sh"
      m="Docker Desktop ships Compose v2 — update Docker Desktop (brew upgrade --cask docker)" ;;
  esac
  case "$PLATFORM" in
    debian) echo "     Debian/Ubuntu/Raspberry Pi OS:  $d" ;;
    fedora) echo "     Fedora:  $f" ;;
    macos)  echo "     macOS:   $m" ;;
    *)      echo "     Debian/Ubuntu/Raspberry Pi OS:  $d"
            echo "     Fedora:  $f" ;;
  esac
}

MISSING=()
for tool in git curl openssl; do
  command -v "$tool" >/dev/null 2>&1 || MISSING+=("$tool")
done
DOCKER_PROBLEM=""
if ! command -v docker >/dev/null 2>&1; then
  MISSING+=(docker)
else
  if ! docker compose version >/dev/null 2>&1; then MISSING+=(compose); fi
  if ! DOCKER_INFO="$(docker info 2>&1)"; then
    case "$DOCKER_INFO" in
      *"permission denied"*)
        DOCKER_PROBLEM="Your user may not talk to Docker yet. Run:  sudo usermod -aG docker \$USER  — then log out and back in (or reboot)." ;;
      *)
        DOCKER_PROBLEM="Docker is installed but the daemon is not running. $( [ "$OS" = Darwin ] && echo 'Start Docker Desktop.' || echo 'Run:  sudo systemctl enable --now docker' )" ;;
    esac
  fi
fi
if [ "${#MISSING[@]}" -gt 0 ] || [ -n "$DOCKER_PROBLEM" ]; then
  printf '\033[1;31m✗ Missing prerequisites:\033[0m\n' >&2
  for tool in ${MISSING[@]+"${MISSING[@]}"}; do
    case "$tool" in
      compose) echo "   • Docker Compose v2 ('docker compose', not the old 'docker-compose')" >&2 ;;
      *)       echo "   • $tool" >&2 ;;
    esac
    fix_for "$tool" >&2
  done
  [ -n "$DOCKER_PROBLEM" ] && echo "   • $DOCKER_PROBLEM" >&2
  echo >&2
  fail "Install the missing pieces above, then re-run the installer."
fi
say "✓ prerequisites ok (git, curl, openssl, docker, compose v2)"

# Raspberry Pi OS (and some other ARM kernels) boot with the memory cgroup
# off: Docker then ignores every mem_limit in the compose file, so one runaway
# container can take the whole board down. Warn — fixing it needs a reboot.
if [ "$OS" = Linux ] && printf '%s' "${DOCKER_INFO:-}" | grep -q "No memory limit support"; then
  warn "Docker reports 'No memory limit support' — container memory limits will NOT apply."
  warn "  Fix (Raspberry Pi OS): add 'cgroup_enable=memory cgroup_memory=1' to the single line in"
  warn "  /boot/firmware/cmdline.txt, then reboot. Details: docs/setup/raspberry-pi.md"
fi


# Local build of the checked-out code — the one build path of this script
# (disk-space preflight first: without it the build dies mid-layer with a raw
# "no space left on device" after minutes). Releases older than the preflight
# library do not ship it; then the library comes from main — it is
# self-contained and version-independent, and a silently skipped preflight
# would be worse than none.
build_locally() {
  local lib=./docker/shared/disk-preflight.sh
  if [ ! -f "$lib" ]; then
    lib="$(mktemp)"
    curl -fsSL "${INSTALLER_URL%/install.sh}/docker/shared/disk-preflight.sh" -o "$lib" \
      || fail "This version predates the disk-space preflight and it could not be downloaded — check the network and re-run."
  fi
  # shellcheck source=docker/shared/disk-preflight.sh
  . "$lib"
  mc_disk_preflight
  docker compose up --build -d
  mc_build_cache_cleanup
}

# Prebuilt images for the pinned release, local build of the same checkout as
# fallback (registry down, image missing for this architecture). Both paths
# run the same version, so the fallback is slower, never inconsistent.
boot_stack() {
  local channel="$1"
  mc_guard_single_stack
  if [ "$channel" = stable ] && docker compose pull backend frontend >/dev/null 2>&1; then
    say "→ using prebuilt images ($(mc_env_get MC_IMAGE_TAG)), starting the stack ..."
    docker compose pull >/dev/null 2>&1 || true   # rest (db/redis/caddy/qdrant)
    docker compose up -d
  else
    [ "$channel" = stable ] && warn "Prebuilt images for $(mc_env_get MC_IMAGE_TAG) not pullable — building this release locally instead."
    say "→ building images locally (first build takes a few minutes) ..."
    build_locally
  fi
}

# with_fleet_carried <git command...> — run a code switch (checkout, pull,
# merge) and carry the operator's fleet file across it.
# docker/docker-compose.agents.yml describes YOUR fleet and rewrites itself at
# runtime, so it left version control — but older releases still track it.
# Either direction makes git refuse the switch (tracked + modified, or
# untracked here + tracked there) or, worse, delete the file without a word.
# So: park a copy in .git/, hand git a clean path (committed version back, or
# the untracked file out of the way), switch, put the copy back. This is what
# scripts/migrate-agents-yml.sh save/restore do — inline here, because the
# release being left may predate that script.
with_fleet_carried() {
  local park=""
  if [ -f "$FLEET_FILE" ]; then
    park="$(git rev-parse --git-dir)/mc-fleet-carry.yml"
    cp "$FLEET_FILE" "$park"
    if git ls-files --error-unmatch "$FLEET_FILE" >/dev/null 2>&1; then
      git checkout -q -- "$FLEET_FILE"
    else
      rm -f "$FLEET_FILE"
    fi
  fi
  if ! "$@"; then
    if [ -n "$park" ]; then cp "$park" "$FLEET_FILE"; rm -f "$park"; fi
    fail "git could not switch the code ($*) — local changes in tracked files? Check 'git status'. Your fleet file is unchanged."
  fi
  if [ -n "$park" ]; then cp "$park" "$FLEET_FILE"; rm -f "$park"; fi
}

fetch_tag() {
  local tag="$1" depth=()
  [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = true ] && depth=(--depth 1)
  git fetch -q ${depth[@]+"${depth[@]}"} "$REPO_URL" "+refs/tags/$tag:refs/tags/$tag" \
    || fail "Could not fetch release $tag from $REPO_URL."
}

resolve_release() { # -> TAG (explicit --version or latest release)
  if [ -n "$VERSION" ]; then TAG="$VERSION"; return; fi
  TAG="$(mc_pick_release)" || fail "Could not determine the latest release (no network to GitHub?). Re-run with --version vX.Y.Z, or --channel edge."
}

# ── Update mode: refresh an existing install ────────────────────────────────
if [ "$UPDATE" = 1 ]; then
  [ -f docker-compose.yml ] && [ -f .env ] || fail "--update expects to run inside an installed Mission Control directory."
  CURRENT_TAG="$(mc_current_release_tag || true)"
  ON_BRANCH="$(git symbolic-ref -q --short HEAD || true)"
  CHANNEL="${CHANNEL_ARG:-$(mc_env_get MC_CHANNEL)}"
  if [ -z "$CHANNEL" ]; then
    # Installed before release pinning: no MC_CHANNEL in .env. A checkout on
    # a release tag is stable; one on a branch (the old installer cloned
    # main) is offered the move to the latest release below, guarded by the
    # database check.
    CHANNEL=stable
  fi

  if [ "$CHANNEL" = stable ]; then
    resolve_release
    if [ -n "$CURRENT_TAG" ] && [ -z "$VERSION" ] && mc_version_gt "$CURRENT_TAG" "$TAG"; then
      # The newest release has no images yet and mc_pick_release fell back
      # below what is installed — nothing newer to go to, not a downgrade.
      say "✓ no newer release with published images than $CURRENT_TAG — staying."
      TAG="$CURRENT_TAG"
    fi
    fetch_tag "$TAG"
    if [ -n "$CURRENT_TAG" ] && [ "$TAG" = "$CURRENT_TAG" ]; then
      say "✓ already on $TAG — refreshing images and restarting."
    elif [ -n "$CURRENT_TAG" ] && mc_version_gt "$CURRENT_TAG" "$TAG"; then
      fail "This install runs $CURRENT_TAG, newer than $TAG — refusing to downgrade (migrations are forward-only)."
    else
      if [ -z "$CURRENT_TAG" ]; then
        # Not on a release (old main-tracking install or a dev checkout):
        # only switch when the database schema is one $TAG knows — otherwise
        # the release's backend would refuse to start on a newer schema.
        DB_REV="$(docker compose exec -T db psql -U mc -d mission_control -tAc \
                  'select version_num from alembic_version' 2>/dev/null | tr -d '[:space:]' || true)"
        if [ -z "$DB_REV" ]; then
          fail "This checkout is not on a release (${ON_BRANCH:-detached}) and the database is not reachable to check its schema. Start the stack (docker compose up -d db) and re-run, or stay on main: ./install.sh --update --channel edge"
        fi
        if ! mc_ref_has_revision "refs/tags/$TAG" "$DB_REV"; then
          fail "Your database (schema $DB_REV) is newer than release $TAG — switching would move the code below it. Stay on main for now: ./install.sh --update --channel edge  (and switch to stable with the next release)."
        fi
        say "→ moving this install from ${ON_BRANCH:-a non-release commit} to release $TAG (schema $DB_REV is part of it) ..."
      else
        say "→ updating $CURRENT_TAG → $TAG ..."
      fi
      with_fleet_carried git -c advice.detachedHead=false checkout -q "refs/tags/$TAG"
    fi
    mc_write_pins stable "$TAG"
  else
    say "→ edge channel: updating to the latest main (local build — not recommended for production use) ..."
    if [ -n "$ON_BRANCH" ] && [ "$ON_BRANCH" != main ]; then
      with_fleet_carried git pull -q --ff-only        # developer on a feature branch
    else
      git fetch -q "$REPO_URL" "+refs/heads/main:refs/remotes/origin/main"
      if [ -z "$ON_BRANCH" ]; then                     # coming from a release tag
        if git show-ref -q --verify refs/heads/main; then
          with_fleet_carried git checkout -q main
        else
          with_fleet_carried git checkout -q -b main origin/main
        fi
      fi
      with_fleet_carried git merge -q --ff-only origin/main
    fi
    mc_write_pins edge
  fi
  # The new version may need .env keys the old one did not know (e.g.
  # INTERNAL_BOOTSTRAP_SECRET): setup.sh never regenerates an existing .env,
  # it only backfills missing keys — appending, so the inode stays.
  ./setup.sh
  boot_stack "$CHANNEL"
  say "✅ Update complete — $( [ "$CHANNEL" = stable ] && echo "release $TAG" || echo "main @ $(git rev-parse --short HEAD)" ) (migrations ran inside the backend on start)."
  exit 0
fi

# ── 2. Get the code ──────────────────────────────────────────────────────────
TAG=""
if [ "$HERE" = 1 ]; then
  [ -f docker-compose.yml ] && [ -f setup.sh ] || fail "--here expects to run inside a Mission Control checkout."
  # CI / developer checkout: always the local build of exactly this code —
  # otherwise CI would test published images instead of the change under test.
  CHANNEL=edge
else
  ask "Install directory" "$TARGET_DIR"; TARGET_DIR="$REPLY"
  if [ -d "$TARGET_DIR" ]; then
    [ -f "$TARGET_DIR/docker-compose.yml" ] || fail "Directory '$TARGET_DIR' exists and is not a Mission Control checkout — choose another."
    cd "$TARGET_DIR"
    # Re-run on an existing checkout: keep its version, never move it here
    # (that is what --update is for, with its downgrade guards).
    if TAG="$(mc_current_release_tag)"; then CHANNEL=stable; else TAG=""; CHANNEL=edge; fi
    say "✓ using existing checkout in $TARGET_DIR ($( [ "$CHANNEL" = stable ] && echo "release $TAG" || echo "not a release — local build" )). To change version: ./install.sh --update"
  else
    CHANNEL="${CHANNEL_ARG:-stable}"
    if [ "$CHANNEL" = stable ]; then
      resolve_release
      say "→ cloning release $TAG into $TARGET_DIR ..."
      git -c advice.detachedHead=false clone -q --depth 1 --branch "$TAG" "$REPO_URL" "$TARGET_DIR"
    else
      warn "edge channel: installing the unreleased main branch, built locally. Not recommended for users — it can break between releases."
      say "→ cloning main into $TARGET_DIR ..."
      git clone -q --depth 1 "$REPO_URL" "$TARGET_DIR"
    fi
    cd "$TARGET_DIR"
  fi
fi

# ── 3. Configure ─────────────────────────────────────────────────────────────
./setup.sh
mc_write_pins "$CHANNEL" "$TAG"

ask "Your name (how agents address you)" "Operator"
mc_env_set OPERATOR_NAME "$REPLY"

# GitHub integration (optional but recommended): MC creates one private repo
# per project and one branch per task; agents push their work and open PRs
# you review. Without it, tasks still run — just without version control.
say ""
say "GitHub integration (optional, recommended):"
say "  MC creates a private repo per project, a branch per task, and agents"
say "  open pull requests for your review. Skip now and connect later in"
say "  Settings → GitHub — everything else works without it."

ask "GitHub user/org for MC-created repos (empty = skip)" ""
GITHUB_OWNER_INPUT="$REPLY"
if [ -n "$GITHUB_OWNER_INPUT" ]; then
  mc_env_set GITHUB_OWNER "$GITHUB_OWNER_INPUT"
  # Silent read — the token must not echo into the terminal/scrollback.
  if [ "$INTERACTIVE" = 1 ]; then
    # shellcheck disable=SC2016  # the backticks are literal text, not a command
    printf 'GitHub token (fine-grained PAT or `gh auth token`; empty = set later in Settings → GitHub): ' >/dev/tty
    stty -echo </dev/tty 2>/dev/null || true
    IFS= read -r GH_TOKEN_INPUT </dev/tty || GH_TOKEN_INPUT=""
    stty echo </dev/tty 2>/dev/null || true
    printf '\n' >/dev/tty
  else
    GH_TOKEN_INPUT=""
  fi
  if [ -n "$GH_TOKEN_INPUT" ]; then
    mc_env_set GH_TOKEN "$GH_TOKEN_INPUT"
    say "✓ GitHub configured ($GITHUB_OWNER_INPUT) — verify under Settings → GitHub after start"
  else
    say "✓ GitHub owner set ($GITHUB_OWNER_INPUT) — add the token later in Settings → GitHub"
  fi
fi

PROFILES=""
ask "Enable voice stack (LiveKit — needs API keys later)? y/N" "n"
case "$REPLY" in y|Y|yes) PROFILES="voice" ;; esac
ask "Enable browser sidecars (Playwright visual-verify)? y/N" "n"
case "$REPLY" in y|Y|yes) PROFILES="${PROFILES:+$PROFILES,}browser" ;; esac
mc_env_set COMPOSE_PROFILES "$PROFILES"

# ── 4. Boot ──────────────────────────────────────────────────────────────────
boot_stack "$CHANNEL"

say "→ waiting for the API (migrations run inside the backend on start) ..."
for i in $(seq 1 60); do
  curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { docker compose logs backend --tail=30; fail "backend did not become healthy — logs above."; }
  sleep 2
done

# The API answers 200 even when ~/.mc is not writable (status "degraded"), so
# the rest of the stack still starts — say it here instead of a silent success.
HEALTH=$(curl -sf http://localhost:8000/health 2>/dev/null || true)
case "$HEALTH" in
  *'"status":"degraded"'*)
    printf '\033[1;33m%s\033[0m\n' "⚠ Mission Control started, but the backend cannot write ~/.mc:" >&2
    printf '%s\n' "$HEALTH" | python3 -c 'import json,sys; d=json.load(sys.stdin).get("mc_home") or {}; print("  " + d.get("hint", "see docker compose logs backend"))' 2>/dev/null \
      || echo "  see: curl -s http://localhost:8000/health" >&2
    echo "  Guide: docs/handbook/operations/platforms.md#host-user-id-linux-nas" >&2
    ;;
esac

echo
say "✅ Mission Control is running! ($( [ "$CHANNEL" = stable ] && echo "release $TAG" || echo "edge — local build of $(git rev-parse --short HEAD 2>/dev/null || echo 'this checkout')" ))"
echo
echo "  1. Open http://localhost and register the first admin user."
echo "  2. Recommended: make backup-schedule  (daily 03:00 backup of DB + data)"
echo "  3. Optional: python3 scripts/demo-seed.py  (populates a demo board)"
echo "  4. First agent: docs/setup/first-agent.md"
echo "  Update later (from inside $(pwd)):"
echo "     curl -fsSL $INSTALLER_URL | bash -s -- --update"
echo
if [ "$INTERACTIVE" = 1 ]; then
  case "$OS" in
    Darwin) open http://localhost >/dev/null 2>&1 || true ;;
    Linux)  command -v xdg-open >/dev/null && xdg-open http://localhost >/dev/null 2>&1 || true ;;
  esac
fi
