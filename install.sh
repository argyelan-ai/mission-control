#!/usr/bin/env bash
# Mission Control — one-line installer
#
#   curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
#
# Checks prerequisites, clones the repo, generates secrets (setup.sh),
# boots the stack, runs migrations and waits until the API is healthy.
# Interactive when a terminal is available (asks install dir, operator
# name, optional profiles); falls back to safe defaults otherwise.
#
# Flags:
#   --here             install into the current directory (expects an
#                      existing checkout — used by CI to test this script)
#   --dir <path>       target directory (default: ./mission-control)
#   --non-interactive  never prompt, use defaults/flags
#   --update           update an existing install (run inside the checkout):
#                      git pull, refresh images, restart, migrate
set -euo pipefail

REPO_URL="https://github.com/argyelan-ai/mission-control.git"
TARGET_DIR="./mission-control"
HERE=0
INTERACTIVE=1

UPDATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --here) HERE=1; INTERACTIVE=0 ;;
    --update) UPDATE=1; INTERACTIVE=0 ;;
    --dir) TARGET_DIR="$2"; shift ;;
    --non-interactive) INTERACTIVE=0 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done
# `curl | bash` has no usable stdin — prompt via /dev/tty when present.
if [ "$INTERACTIVE" = 1 ] && { [ ! -e /dev/tty ] || ! : </dev/tty; } 2>/dev/null; then
  INTERACTIVE=0
fi

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
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
OS="$(uname -s)"
hint_docker() {
  case "$OS" in
    Darwin) echo "   → install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/" ;;
    Linux)  echo "   → install Docker Engine: https://docs.docker.com/engine/install/ (or Docker Desktop under WSL2 on Windows)" ;;
  esac
}
command -v git >/dev/null     || fail "git is required. $( [ "$OS" = Darwin ] && echo 'Run: xcode-select --install' || echo 'Install it via your package manager (apt/dnf/pacman).')"
command -v docker >/dev/null  || { hint_docker; fail "docker is required."; }
docker info >/dev/null 2>&1   || fail "Docker is installed but the daemon is not running — start Docker and re-run."
docker compose version >/dev/null 2>&1 || { hint_docker; fail "Docker Compose v2 is required ('docker compose', not 'docker-compose')."; }
command -v openssl >/dev/null || fail "openssl is required (used to generate secrets)."
case "$OS" in Darwin|Linux) ;; *) fail "Unsupported platform '$OS'. On Windows, run this inside WSL2." ;; esac
say "✓ prerequisites ok (git, docker, compose v2, openssl)"

# ── Update-Modus: bestehende Installation aktualisieren ─────────────────────
if [ "$UPDATE" = 1 ]; then
  [ -f docker-compose.yml ] && [ -f .env ] || fail "--update expects to run inside an installed Mission Control directory."
  say "→ pulling latest code ..."
  git pull --ff-only
  if docker compose pull backend frontend >/dev/null 2>&1; then
    say "→ using updated prebuilt images ..."
    docker compose up -d
  else
    say "→ rebuilding locally ..."
    docker compose up --build -d
  fi
  say "✅ Update complete (migrations ran inside the backend on start)."
  exit 0
fi

# ── 2. Get the code ──────────────────────────────────────────────────────────
if [ "$HERE" = 1 ]; then
  [ -f docker-compose.yml ] && [ -f setup.sh ] || fail "--here expects to run inside a Mission Control checkout."
else
  ask "Install directory" "$TARGET_DIR"; TARGET_DIR="$REPLY"
  if [ -d "$TARGET_DIR" ]; then
    [ -f "$TARGET_DIR/docker-compose.yml" ] || fail "Directory '$TARGET_DIR' exists and is not a Mission Control checkout — choose another."
    say "✓ using existing checkout in $TARGET_DIR"
  else
    say "→ cloning into $TARGET_DIR ..."
    git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
  fi
  cd "$TARGET_DIR"
fi

# ── 3. Configure ─────────────────────────────────────────────────────────────
./setup.sh

# Portable in-place sed (BSD/macOS vs GNU) — mirrors setup.sh.
sed_i() { if sed --version >/dev/null 2>&1; then sed -i "$@"; else sed -i '' "$@"; fi }

ask "Your name (how agents address you)" "Operator"
if grep -q '^OPERATOR_NAME=' .env; then sed_i "s|^OPERATOR_NAME=.*|OPERATOR_NAME=$REPLY|" .env; else echo "OPERATOR_NAME=$REPLY" >> .env; fi

# GitHub integration (optional but recommended): MC creates one private repo
# per project and one branch per task; agents push their work and open PRs
# you review. Without it, tasks still run — just without version control.
say ""
say "GitHub integration (optional, recommended):"
say "  MC creates a private repo per project, a branch per task, and agents"
say "  open pull requests for your review. Skip now and connect later in"
say "  Settings → GitHub — everything else works without it."
# set_env_var KEY VALUE — replace-or-append without sed (a token containing
# sed metacharacters like & or \ would silently corrupt the .env otherwise).
set_env_var() {
  grep -v "^$1=" .env > .env.tmp 2>/dev/null || true
  mv .env.tmp .env
  printf '%s=%s\n' "$1" "$2" >> .env
}

ask "GitHub user/org for MC-created repos (empty = skip)" ""
GITHUB_OWNER_INPUT="$REPLY"
if [ -n "$GITHUB_OWNER_INPUT" ]; then
  set_env_var GITHUB_OWNER "$GITHUB_OWNER_INPUT"
  # Silent read — the token must not echo into the terminal/scrollback.
  if [ "$INTERACTIVE" = 1 ]; then
    printf 'GitHub token (fine-grained PAT or `gh auth token`; empty = set later in Settings → GitHub): ' >/dev/tty
    stty -echo </dev/tty 2>/dev/null || true
    IFS= read -r GH_TOKEN_INPUT </dev/tty || GH_TOKEN_INPUT=""
    stty echo </dev/tty 2>/dev/null || true
    printf '\n' >/dev/tty
  else
    GH_TOKEN_INPUT=""
  fi
  if [ -n "$GH_TOKEN_INPUT" ]; then
    set_env_var GH_TOKEN "$GH_TOKEN_INPUT"
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
if grep -q '^COMPOSE_PROFILES=' .env; then sed_i "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=$PROFILES|" .env; else echo "COMPOSE_PROFILES=$PROFILES" >> .env; fi

# ── 4. Boot ──────────────────────────────────────────────────────────────────
# Bevorzugt die offiziellen GHCR-Images (Minuten -> Sekunden); Fallback ist
# der lokale Build. --here (CI / Entwickler-Checkout) baut IMMER lokal —
# sonst wuerde die CI publizierte Images statt des Checkouts testen.
if [ "$HERE" = 0 ] && docker compose pull backend frontend >/dev/null 2>&1; then
  say "→ using prebuilt images, starting the stack ..."
  docker compose pull >/dev/null 2>&1 || true   # Rest (db/redis/caddy/qdrant)
  docker compose up -d
else
  say "→ building images locally (first build takes a few minutes) ..."
  docker compose up --build -d
fi

say "→ waiting for the API (migrations run inside the backend on start) ..."
for i in $(seq 1 60); do
  curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { docker compose logs backend --tail=30; fail "backend did not become healthy — logs above."; }
  sleep 2
done

echo
say "✅ Mission Control is running!"
echo
echo "  1. Open http://localhost and register the first admin user."
echo "  2. Recommended: make backup-schedule  (daily 03:00 backup of DB + data)"
echo "  3. Optional: python3 scripts/demo-seed.py  (populates a demo board)"
echo "  4. First agent: docs/setup/first-agent.md"
echo
if [ "$INTERACTIVE" = 1 ]; then
  case "$OS" in
    Darwin) open http://localhost >/dev/null 2>&1 || true ;;
    Linux)  command -v xdg-open >/dev/null && xdg-open http://localhost >/dev/null 2>&1 || true ;;
  esac
fi
