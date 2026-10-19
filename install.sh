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
#   --remote           MC will be opened from other devices: after the first
#                      admin is created (in the terminal), listen on the
#                      network and set PUBLIC_HOST (address auto-detected)
#   --public-host <a>  same, with this address (IP or name.local)
#   --local            localhost only, never ask (the default without a TTY)
#   --update           update an existing install (run inside the checkout):
#                      move to the latest release (or main on edge), refresh
#                      images, restart, migrate. Works from any version:
#                      curl -fsSL <url above> | bash -s -- --update
#                      Takes a backup (./backup.sh) before switching, stops
#                      when agents are working, waits for /health and says
#                      "Update failed" with the way back when it is not.
#   --no-backup        --update without the backup first (on purpose only)
#   --ignore-running-tasks  --update even while tasks are in progress
#                      (without a terminal the update stops otherwise)
#   --bridge           set up (or repair) the cli-bridge service in an
#                      existing install (run inside the checkout). The bridge
#                      runs host agents and is the door for the update button
#                      in Settings → System. On Linux it listens on the
#                      gateway of MC's Docker network and asks before it
#                      enables linger (sudo). After a fresh install or an
#                      update it is offered; unattended: MC_BRIDGE=yes|no,
#                      MC_BRIDGE_LINGER=yes.
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

# Shared helpers: the .env functions (mc_env_get / mc_env_set /
# mc_env_unset — ONE implementation, written in place so the inode that
# backend and worker bind-mount stays; backend/tests/
# test_install_release_pinning.py pins it) and the remote-access step. They
# live in scripts/lib/remote-access.sh because setup.sh and
# scripts/open-network.sh need them too. Taken from next to this script
# (same version); under `curl | bash` there is no such file, so it comes from
# main — the same place this installer was fetched from. A release checkout
# older than the library is never used for it (that would mix versions).
mc_load_lib() {
  local self="${BASH_SOURCE[0]:-}" lib=""
  if [ -n "$self" ] && [ -f "$self" ]; then
    lib="$(cd "$(dirname "$self")" && pwd)/scripts/lib/remote-access.sh"
  fi
  if [ -z "$lib" ] || [ ! -f "$lib" ]; then
    lib="$(mktemp)"
    curl -fsSL "${INSTALLER_URL%/install.sh}/scripts/lib/remote-access.sh" -o "$lib" \
      || fail "Could not download the installer's helper library (scripts/lib/remote-access.sh) — check the network and re-run."
  fi
  # shellcheck source=scripts/lib/remote-access.sh
  . "$lib"
}
mc_load_lib

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

# ── Health + update safety helpers ──────────────────────────────────────────
# Shared by the fresh install and --update; backend/tests/test_install_update.py
# drives them (and the whole --update path) against a fake docker in PATH.

# How long to wait for /health after the stack is up. The backend's
# healthcheck grants a slow first boot start_period: 300s (migrations on an
# SD card, docker-compose.yml); the wait must outlast it, not give up at 2 min.
MC_HEALTH_TIMEOUT="${MC_HEALTH_TIMEOUT:-360}"

# mc_check_health_timeout — a non-number would break the arithmetic below
# (and bash would then report success); say it in plain words instead.
mc_check_health_timeout() {
  case "$MC_HEALTH_TIMEOUT" in
    ''|*[!0-9]*) fail "MC_HEALTH_TIMEOUT must be a whole number of seconds (got '$MC_HEALTH_TIMEOUT')." ;;
  esac
}

# mc_wait_healthy — 0 as soon as the backend answers /health, 1 when it has
# not answered within MC_HEALTH_TIMEOUT seconds.
mc_wait_healthy() {
  local deadline=$((SECONDS + MC_HEALTH_TIMEOUT))
  until curl -sf --max-time 5 "$(_mc_api)/health" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep "${MC_HEALTH_INTERVAL:-2}"
  done
}

# The API answers 200 even when ~/.mc is not writable (status "degraded"), so
# the rest of the stack still starts — say it instead of a silent success.
mc_warn_if_degraded() {
  local health
  health=$(curl -sf --max-time 5 "$(_mc_api)/health" 2>/dev/null || true)
  case "$health" in
    *'"status":"degraded"'*)
      printf '\033[1;33m%s\033[0m\n' "⚠ Mission Control started, but the backend cannot write ~/.mc:" >&2
      printf '%s\n' "$health" | python3 -c 'import json,sys; d=json.load(sys.stdin).get("mc_home") or {}; print("  " + d.get("hint", "see docker compose logs backend"))' 2>/dev/null \
        || echo "  see: curl -s $(_mc_api)/health" >&2
      echo "  Guide: docs/handbook/operations/platforms.md#host-user-id-linux-nas" >&2
      ;;
  esac
}

# mc_db_value <sql> — one value from the running database, "" when the
# database is not reachable. Read-only queries only. stdin is closed:
# `docker compose exec -T` forwards it and under `curl | bash` would eat
# the rest of this script.
mc_db_value() {
  docker compose exec -T db psql -U mc -d mission_control -tAc "$1" </dev/null 2>/dev/null | tr -d '[:space:]' || true
}

# mc_running_task_count — tasks an agent is working on right now ("" = unknown).
mc_running_task_count() {
  mc_db_value "select count(*) from tasks where status = 'in_progress'"
}

# mc_backup_dirs — the folders backup.sh may have written to, one per line.
# backup.sh owns the setting; this mirrors it only to name the new file:
# MC_BACKUP_DIR from the environment, then from .env, default ~/mc-backups
# (relative paths are relative to this directory), plus ./backups where
# backup.sh up to 0.3.x writes — the update runs the OLD release's script.
mc_backup_dirs() {
  local d="${MC_BACKUP_DIR:-}"
  [ -n "$d" ] || d="$(mc_env_get MC_BACKUP_DIR)"
  d="${d:-$HOME/mc-backups}"
  d="${d/#\~/$HOME}"
  printf '%s\n' "$d" ./backups
}

# mc_newest_backup <marker> — newest database backup written after <marker>
# (a file touched right before backup.sh ran), or nothing.
mc_newest_backup() {
  local marker="$1" dir
  while IFS= read -r dir; do
    [ -d "$dir" ] || continue
    find "$dir" -maxdepth 1 -type f -name 'mc_backup_*.sql.gz' -newer "$marker" 2>/dev/null
  done < <(mc_backup_dirs) | sort | tail -n 1
}

# ── cli-bridge: host helper and update door (ADR-094) ───────────────────────
# The bridge runs host agents and is the door through which Settings →
# System updates MC (scripts/mc_update_door.py). It is a service in this
# user's account, so it is only set up after a yes.
# backend/tests/test_install_bridge_setup.py drives these functions.

# Compose fixes the name of MC's default network (docker-compose.yml).
MC_BRIDGE_NETWORK="${MC_BRIDGE_NETWORK:-mission-control_default}"
MC_BRIDGE_PORT="${CLI_BRIDGE_PORT:-18792}"

mc_is_ipv4() { [[ "${1:-}" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; }

# mc_docker_gateway_ip — the gateway of MC's Docker network. On Linux the
# backend container reaches the host there; the LAN does not.
mc_docker_gateway_ip() {
  local gw
  gw="$(docker network inspect "$MC_BRIDGE_NETWORK" --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' 2>/dev/null \
          | tr ' ' '\n' | sed -n '/./{p;q;}')" || true
  mc_is_ipv4 "$gw" || return 1
  printf '%s\n' "$gw"
}

mc_user() { id -un 2>/dev/null || printf '%s\n' "${USER:-}"; }

# Linger: the user's services keep running without a login session and start
# at boot. Readable without a session (the update preflight checks the same).
mc_linger_enabled() { [ -e "${MC_LINGER_DIR:-/var/lib/systemd/linger}/$(mc_user)" ]; }

# mc_confirm "question" — true only on an explicit yes typed at the terminal
# (MC_TTY, default /dev/tty). No terminal, Enter or anything else → no.
mc_confirm() {
  local tty="${MC_TTY:-/dev/tty}" reply=""
  { [ -r "$tty" ] && : <"$tty"; } 2>/dev/null || return 1
  printf '%s [y/N]: ' "$1" >&2
  IFS= read -r reply <"$tty" || reply=""
  case "$reply" in y|Y|yes|Yes|YES) return 0 ;; *) return 1 ;; esac
}

mc_bridge_hint() {
  echo "  Update helper (cli-bridge) not set up — Settings → System can only update MC with it."
  echo "  Later, from inside $(pwd):  ./install.sh --bridge"
}

# mc_bridge_setup ask|yes|batch — run inside the install folder.
#   ask   ask before setting up the bridge, then (Linux) offer linger
#   yes   set up the bridge (the user asked for it), offer linger
#   batch set up the bridge without any prompt; linger only with
#         MC_BRIDGE_LINGER=yes (an explicit setting for unattended installs)
mc_bridge_setup() {
  local mode="${1:-ask}" os bind="127.0.0.1" gw="" url user
  os="$(uname -s)"
  if [ "$mode" = ask ]; then
    echo
    say "Update helper (cli-bridge): runs host agents and lets you update MC from Settings → System."
    if ! mc_confirm "Set it up as a service in your account now?"; then
      mc_bridge_hint
      return 0
    fi
  fi
  command -v python3 >/dev/null 2>&1 \
    || { warn "python3 is missing — the cli-bridge needs it. Install python3, then: ./install.sh --bridge"; return 1; }
  if [ "$os" = Linux ]; then
    # 127.0.0.1 is not reachable from the backend container on Linux.
    gw="$(mc_docker_gateway_ip)" \
      || { warn "Docker network $MC_BRIDGE_NETWORK not found (or no IPv4 gateway) — start MC first (docker compose up -d), then: ./install.sh --bridge"; return 1; }
    bind="$gw"
  fi
  say "→ installing the cli-bridge service (listens on $bind:$MC_BRIDGE_PORT, token required) ..."
  CLI_BRIDGE_BIND="$bind" ./scripts/cli-bridge-service.sh \
    || { warn "The cli-bridge service could not be installed (output above). Fix it, then: ./install.sh --bridge"; return 1; }
  if [ "$os" = Linux ]; then
    url="http://$gw:$MC_BRIDGE_PORT"
    if [ "$(mc_env_get FREE_CODE_BRIDGE_URL)" != "$url" ]; then
      mc_env_set FREE_CODE_BRIDGE_URL "$url"
      say "→ pointing backend and worker at the bridge ($url) ..."
      docker compose up -d backend mc-worker </dev/null \
        || warn "Could not restart backend + worker — run: docker compose up -d backend mc-worker"
    fi
    if ! mc_linger_enabled; then
      user="$(mc_user)"
      echo "  Without 'linger' the bridge stops when you log out and does not start after a reboot —"
      echo "  an update interrupted by a power cut could then not finish on its own."
      if { [ "$mode" != batch ] && mc_confirm "Enable linger now (runs: sudo loginctl enable-linger $user)?"; } \
         || { [ "$mode" = batch ] && [ "${MC_BRIDGE_LINGER:-}" = yes ]; }; then
        if sudo loginctl enable-linger "$user" </dev/null && mc_linger_enabled; then
          say "✓ linger is on — the bridge survives logout and reboot"
        else
          warn "linger is still off — run on this machine: sudo loginctl enable-linger $user"
        fi
      else
        warn "linger is off — when you are ready, run on this machine: sudo loginctl enable-linger $user"
      fi
    fi
  fi
  say "✓ cli-bridge set up"
}

# mc_bridge_installed — the service unit of the bridge exists.
mc_bridge_installed() {
  [ -f "$HOME/Library/LaunchAgents/com.mc.cli-bridge.plist" ] || [ -f "$HOME/.config/systemd/user/mc-cli-bridge.service" ]
}

# mc_bridge_offer — after an install or an update: set the bridge up (asked,
# or MC_BRIDGE=yes|no for unattended runs); an installed bridge on Linux
# follows its network gateway (compose down/up can move it).
mc_bridge_offer() {
  local interactive="${1:-0}" gw
  if mc_bridge_installed; then
    [ "$(uname -s)" = Linux ] || return 0
    gw="$(mc_docker_gateway_ip)" || return 0
    if [ "$(mc_env_get FREE_CODE_BRIDGE_URL)" != "http://$gw:$MC_BRIDGE_PORT" ]; then
      say "→ MC's Docker network moved to $gw — moving the cli-bridge with it ..."
      mc_bridge_setup batch || true
    fi
    return 0
  fi
  case "${MC_BRIDGE:-}" in
    yes) mc_bridge_setup batch || true ;;
    no)  mc_bridge_hint ;;
    *)   if [ "$interactive" = 1 ]; then mc_bridge_setup ask || true; else mc_bridge_hint; fi ;;
  esac
}

[ "${MC_INSTALL_LIB_ONLY:-0}" = 1 ] && return 0 2>/dev/null

# ── Arguments ────────────────────────────────────────────────────────────────
TARGET_DIR="./mission-control"
HERE=0
INTERACTIVE=1
UPDATE=0
BRIDGE_ONLY=0
NO_BACKUP=0
IGNORE_RUNNING=0
NONINTERACTIVE_FLAG=0
CHANNEL_ARG="${MC_CHANNEL:-}"
VERSION="${MC_VERSION:-}"
MC_REMOTE="${MC_REMOTE:-}"
MC_PUBLIC_HOST="${MC_PUBLIC_HOST:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --here) HERE=1; INTERACTIVE=0 ;;
    --update) UPDATE=1; INTERACTIVE=0 ;;
    --bridge) BRIDGE_ONLY=1 ;;
    --no-backup) NO_BACKUP=1 ;;
    --ignore-running-tasks) IGNORE_RUNNING=1 ;;
    --dir) TARGET_DIR="$2"; shift ;;
    --channel) CHANNEL_ARG="$2"; shift ;;
    --version) VERSION="$2"; shift ;;
    --remote) MC_REMOTE=yes ;;
    --public-host) MC_REMOTE=yes; MC_PUBLIC_HOST="$2"; shift ;;
    --local) MC_REMOTE=no ;;
    --non-interactive) INTERACTIVE=0; NONINTERACTIVE_FLAG=1 ;;
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
mc_check_health_timeout
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

if [ "$BRIDGE_ONLY" = 1 ]; then
  [ -f docker-compose.yml ] && [ -f .env ] && [ -x scripts/cli-bridge-service.sh ] \
    || fail "--bridge expects to run inside an installed Mission Control directory (with scripts/cli-bridge-service.sh)."
  if [ "$NONINTERACTIVE_FLAG" = 1 ]; then mc_bridge_setup batch; else mc_bridge_setup yes; fi
  exit $?
fi

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
    docker compose up -d || {
      # Usually a slow first boot (migrations on an SD card) that outlasted
      # the backend's start period. The stack is safe to start again.
      warn "The stack did not come up in time — on slow machines the first start can take several minutes."
      say "→ trying once more ..."
      docker compose up -d || fail "Mission Control did not start. Check: docker compose logs backend --tail 50 — a re-run of this installer is safe and keeps everything done so far."
    }
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
# Order: checks (nothing changes) → backup → switch code + pins → setup.sh →
# boot → wait for /health. Only a healthy backend is reported as success; any
# exit after the switch prints "Update failed" with the backup and the way
# back (mc_update_on_exit). Kept small on purpose: the update engine planned
# for the in-app button (scripts/mc_update.py) takes these steps over.
UPDATE_PHASE=check        # check → switched → finished
UPDATE_BACKUP=""          # file written by backup.sh, "" when skipped/unknown
UPDATE_BACKUP_NOTE=""
UPDATE_BACKUP_RAN=0       # 1 = backup.sh succeeded (even if its file was not found)
UPDATE_OLD_REF=""         # what to check out to go back
UPDATE_DB_REV_BEFORE=""   # alembic revision before the switch ("" = unknown)
UPDATE_ENV_COPY=".env.backup-pre-update"

mc_update_on_exit() {
  local rc=$? rev_after
  [ "$rc" != 0 ] || return 0
  if [ "$UPDATE_PHASE" = check ]; then
    printf '\033[1;31m%s\033[0m\n' "✗ Update aborted — nothing was changed (see the reason above)." >&2
    return 0
  fi
  [ "$UPDATE_PHASE" = switched ] || return 0
  rev_after="$(mc_db_value 'select version_num from alembic_version')"
  {
    echo
    printf '\033[1;31m%s\033[0m\n' "✗ Update failed — Mission Control is not confirmed running on the new version."
    echo "  Logs:    docker compose logs backend --tail 50"
    if [ -n "$UPDATE_BACKUP" ]; then
      echo "  Backup taken right before the switch: $UPDATE_BACKUP"
    else
      echo "  Backup: ${UPDATE_BACKUP_NOTE:-none}"
    fi
    echo "  Re-running the same update is safe. To go back to ${UPDATE_OLD_REF}:"
    echo "    git checkout -q ${UPDATE_OLD_REF}"
    echo "    cp ${UPDATE_ENV_COPY} .env              # the .env from before the update"
    if [ -n "$UPDATE_DB_REV_BEFORE" ] && [ "$rev_after" = "$UPDATE_DB_REV_BEFORE" ]; then
      echo "    # database schema unchanged ($UPDATE_DB_REV_BEFORE) — no restore needed"
    elif [ -n "$UPDATE_BACKUP" ]; then
      echo "    ./backup.sh restore $UPDATE_BACKUP   # database as it was before the update"
    elif [ "$UPDATE_BACKUP_RAN" = 1 ]; then
      echo "    ./backup.sh restore <the mc_backup_….sql.gz file backup.sh named above>"
    elif [ "$NO_BACKUP" = 1 ]; then
      echo "    # the database may already be migrated — no backup from this run (--no-backup)"
    else
      echo "    # the database may already be migrated and there is no backup from this run"
    fi
    echo "    docker compose up -d"
    echo "  (If git refuses because of $FLEET_FILE: move that file aside, check out, move it back.)"
  } >&2
}

# Stop before anything changes while agents are working: the restart cuts
# their runs off mid-task. A terminal may answer; without one the update stops.
mc_update_check_running_tasks() {
  local n
  [ "$IGNORE_RUNNING" = 1 ] && return 0
  n="$(mc_running_task_count)"
  case "$n" in
    ''|*[!0-9]*)
      # Without the database there is no backup either — say it once, here.
      [ "$NO_BACKUP" = 1 ] || fail "The database is not reachable — the update backs it up first, so it has to run. Start it (docker compose up -d db) and re-run, or update without a backup: --no-backup"
      warn "Could not count running tasks (database not reachable) — continuing because of --no-backup."
      return 0 ;;
    0) return 0 ;;
  esac
  warn "$n task(s) are in progress right now — the update restarts Mission Control and interrupts them."
  if [ "$NONINTERACTIVE_FLAG" = 0 ] && [ -e /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
    printf 'Update anyway? y/N: ' >/dev/tty
    IFS= read -r REPLY </dev/tty || REPLY=""
    case "$REPLY" in y|Y|yes) return 0 ;; esac
  fi
  fail "Stopped: let the tasks finish (or pause them) and re-run. To update anyway: --ignore-running-tasks"
}

# Backup right before the switch — the update does not start without one
# unless --no-backup was given. Only backup.sh's command line and exit code
# are relied on; it decides where and how it writes.
mc_update_backup() {
  local marker backup_in=/dev/null
  if [ "$NO_BACKUP" = 1 ]; then
    warn "--no-backup: updating WITHOUT a backup first."
    UPDATE_BACKUP_NOTE="none — skipped with --no-backup"
    return 0
  fi
  [ -x ./backup.sh ] || fail "./backup.sh not found — cannot take the backup before the update. Run with --no-backup to update without one."
  say "→ backing up before the update (./backup.sh) ..."
  marker="$(mktemp)"
  # backup.sh may ask for a passphrase: give it the terminal when there is
  # one, never the installer's own stdin (`curl | bash` = the script itself).
  if [ "$NONINTERACTIVE_FLAG" = 0 ] && [ -e /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
    backup_in=/dev/tty
  fi
  if ! ./backup.sh <"$backup_in"; then
    rm -f "$marker"
    fail "The backup failed — the update did not start. Is the database running (docker compose up -d db)? Fix the backup (output above) and re-run, or update without one: --no-backup"
  fi
  UPDATE_BACKUP_RAN=1
  UPDATE_BACKUP="$(mc_newest_backup "$marker")"
  rm -f "$marker"
  if [ -n "$UPDATE_BACKUP" ]; then
    say "✓ backup: $UPDATE_BACKUP"
  else
    UPDATE_BACKUP_NOTE="written by ./backup.sh — the file is named in its output above (not found in: $(mc_backup_dirs | paste -sd ' ' -))"
    say "✓ backup done — see the file named in backup.sh's output above"
  fi
}

# Last step before the code changes: remember the way back, then back up.
mc_update_prepare_switch() {
  UPDATE_OLD_REF="${CURRENT_TAG:-$(git rev-parse --short HEAD)}"
  UPDATE_DB_REV_BEFORE="$(mc_db_value 'select version_num from alembic_version')"
  mc_update_backup
  # The old .env, for the way back (setup.sh may add keys, pins change).
  # Git-ignored (.env.backup*), owner-only like .env itself.
  rm -f "$UPDATE_ENV_COPY"
  (umask 077 && cp .env "$UPDATE_ENV_COPY")
  UPDATE_PHASE=switched
}

if [ "$UPDATE" = 1 ]; then
  trap mc_update_on_exit EXIT
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
  mc_update_check_running_tasks

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
      mc_update_prepare_switch
    elif [ -n "$CURRENT_TAG" ] && mc_version_gt "$CURRENT_TAG" "$TAG"; then
      fail "This install runs $CURRENT_TAG, newer than $TAG — refusing to downgrade (migrations are forward-only)."
    else
      if [ -z "$CURRENT_TAG" ]; then
        # Not on a release (old main-tracking install or a dev checkout):
        # only switch when the database schema is one $TAG knows — otherwise
        # the release's backend would refuse to start on a newer schema.
        DB_REV="$(mc_db_value 'select version_num from alembic_version')"
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
      mc_update_prepare_switch
      with_fleet_carried git -c advice.detachedHead=false checkout -q "refs/tags/$TAG"
    fi
    mc_write_pins stable "$TAG"
  else
    say "→ edge channel: updating to the latest main (local build — not recommended for production use) ..."
    mc_update_prepare_switch
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
  say "→ waiting for the API (migrations run inside the backend on start; up to $((MC_HEALTH_TIMEOUT / 60)) min on slow machines) ..."
  mc_wait_healthy || fail "The backend did not become healthy within ${MC_HEALTH_TIMEOUT}s."
  mc_warn_if_degraded
  UPDATE_PHASE=finished
  say "✅ Update complete — $( [ "$CHANNEL" = stable ] && echo "release $TAG" || echo "main @ $(git rev-parse --short HEAD)" ) is running and healthy."
  [ -n "$UPDATE_BACKUP" ] && echo "  Backup from before the update: $UPDATE_BACKUP"
  # --update never prompts; MC_BRIDGE=yes sets the bridge up unattended.
  mc_bridge_offer 0 || true
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
# The remote-access question is asked below, with the other questions.
MC_SETUP_VIA_INSTALLER=1 ./setup.sh
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

# Remote access: decided now, applied only AFTER the first admin exists (the
# stack starts on 127.0.0.1; until an admin is registered anyone on the LAN
# could claim that account). Sets MC_REMOTE_HOST, writes nothing.
MC_INTERACTIVE="$INTERACTIVE" MC_REMOTE="$MC_REMOTE" MC_PUBLIC_HOST="$MC_PUBLIC_HOST" \
  mc_remote_decide .env

# ── 4. Boot ──────────────────────────────────────────────────────────────────
boot_stack "$CHANNEL"

say "→ waiting for the API (migrations run inside the backend on start; up to $((MC_HEALTH_TIMEOUT / 60)) min on slow machines) ..."
mc_wait_healthy || { docker compose logs backend --tail=30; fail "backend did not become healthy within ${MC_HEALTH_TIMEOUT}s — logs above. A re-run of this installer is safe."; }
mc_warn_if_degraded

# Remote access, step 2: first admin (terminal, over localhost), then open.
MC_INTERACTIVE="$INTERACTIVE" mc_remote_finish "$MC_REMOTE_HOST" .env

# The update helper: asked here, never installed without a yes.
mc_bridge_offer "$INTERACTIVE" || true

MC_URL=$(mc_access_url .env)
echo
say "✅ Mission Control is running! ($( [ "$CHANNEL" = stable ] && echo "release $TAG" || echo "edge — local build of $(git rev-parse --short HEAD 2>/dev/null || echo 'this checkout')" ))"
echo
# Everything after the install happens in the setup wizard (/setup): AI
# provider key, a starter board or demo crew, the first agent. Without a
# login it sends to /login first and comes back after the registration.
if mc_admin_exists; then
  echo "  1. Open $MC_URL/setup and log in — the setup wizard takes you from there."
else
  echo "  1. Open $MC_URL/setup and register the first admin — the setup wizard starts right after."
fi
if [ "$MC_URL" = "http://localhost" ] && mc_is_headless; then
  echo "     (no browser here: use the SSH tunnel shown above → http://localhost:8080/setup)"
fi
echo "  2. Recommended: make backup-schedule  (daily 03:00 backup of DB + data)"
echo "  Update later (from inside $(pwd)):"
echo "     curl -fsSL $INSTALLER_URL | bash -s -- --update"
echo
if [ "$INTERACTIVE" = 1 ] && ! mc_is_headless; then
  case "$OS" in
    Darwin) open "$MC_URL/setup" >/dev/null 2>&1 || true ;;
    Linux)  command -v xdg-open >/dev/null && xdg-open "$MC_URL/setup" >/dev/null 2>&1 || true ;;
  esac
fi
