#!/bin/bash
set -euo pipefail

# ============================================================
# Mission Control — run cli-bridge.py as a managed service
#
# The CLI bridge is the host-side helper that provisions agents
# (workspace + SOUL/TOOLS render, port 18792). Until now it had
# to be kept alive by hand in a terminal; this installs it as a
# supervised service that starts at login and respawns on crash.
#
#   ./scripts/cli-bridge-service.sh            → install + start
#   ./scripts/cli-bridge-service.sh --remove   → stop + uninstall
#   ./scripts/cli-bridge-service.sh --status   → is it running?
#
# macOS: launchd agent   ~/Library/LaunchAgents/com.mc.cli-bridge.plist
# Linux: systemd user unit  ~/.config/systemd/user/mc-cli-bridge.service
#        (headless boxes: `loginctl enable-linger $USER` keeps user
#        units alive without an open session)
#
# Env at install time (baked into the unit):
#   CLI_BRIDGE_PORT        non-default port (default 18792)
#   CLI_BRIDGE_LOG_DIR     log directory (default ~/.mc/logs/cli-bridge)
#   CLI_BRIDGE_BIND        listen address (default 127.0.0.1 — right for
#                          Docker Desktop on macOS; Linux: docker bridge IP,
#                          see docs/setup/cli-bridge-auth.md)
#   CLI_BRIDGE_TOKEN_FILE  shared-secret file (default
#                          ~/.mc/secrets/cli-bridge.token, created 0600 here
#                          if missing; the backend reads the same file)
#   CLI_BRIDGE_PYTHON      interpreter for the service (default: python3 on
#                          PATH); must be able to import `websockets`
#
# Older installs ran the same script under a hand-made launchd label. Install
# finds every LaunchAgent other than com.mc.cli-bridge whose plist runs
# scripts/cli-bridge.py (plus CLI_BRIDGE_LEGACY_LABELS, space-separated),
# stops it and moves its plist to ~/.mc/backups/launchd/<name>.bak-<date> -
# otherwise the old process keeps the port (open on all interfaces, no token)
# and the new service crash-loops.
# ============================================================

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE="$REPO_DIR/scripts/cli-bridge.py"
PORT="${CLI_BRIDGE_PORT:-18792}"
LOG_DIR="${CLI_BRIDGE_LOG_DIR:-$HOME/.mc/logs/cli-bridge}"
PYTHON_BIN="${CLI_BRIDGE_PYTHON:-$(command -v python3)}"
BACKUP_DIR="$HOME/.mc/backups/launchd"
PLIST="$HOME/Library/LaunchAgents/com.mc.cli-bridge.plist"
UNIT="$HOME/.config/systemd/user/mc-cli-bridge.service"

# Bind: env wins; otherwise what the installed unit was set up with (so
# `--status` checks the address the service really uses); else loopback.
installed_bind() {
  if [[ -f "$PLIST" ]]; then
    /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:CLI_BRIDGE_BIND" "$PLIST" 2>/dev/null || true
  elif [[ -f "$UNIT" ]]; then
    sed -n 's/^Environment=CLI_BRIDGE_BIND=//p' "$UNIT" | head -1
  fi
}
BIND="${CLI_BRIDGE_BIND:-$(installed_bind)}"
BIND="${BIND:-127.0.0.1}"
# Where to probe: a wildcard bind is reachable on loopback.
PROBE_HOST="$BIND"
[[ "$PROBE_HOST" == "0.0.0.0" ]] && PROBE_HOST="127.0.0.1"
TOKEN_FILE="${CLI_BRIDGE_TOKEN_FILE:-$HOME/.mc/secrets/cli-bridge.token}"

# Shared secret: create it once (0600) before the service starts. Prints the
# path only — the token value never goes to the terminal or a log.
init_token() {
  CLI_BRIDGE_TOKEN_FILE="$TOKEN_FILE" CLI_BRIDGE_LOG_DIR="$LOG_DIR" \
    "$PYTHON_BIN" "$BRIDGE" --init-token >/dev/null
  echo "cli-bridge token file: $TOKEN_FILE (0600)"
}

# /ping is the only endpoint without the token — liveness, nothing else.
status() {
  if curl -sf "http://${PROBE_HOST}:${PORT}/ping" >/dev/null 2>&1; then
    echo "cli-bridge: RUNNING (${PROBE_HOST}:${PORT})"
    return 0
  fi
  echo "cli-bridge: NOT reachable on ${PROBE_HOST}:${PORT}"
  return 1
}

# Who listens on the port (empty = free). "command pid address" per line.
listeners() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $1, $2, $9}'
  elif command -v ss >/dev/null 2>&1; then
    ss -Hltnp "sport = :$PORT" 2>/dev/null | awk '{print $6, "-", $4}'
  fi
}

# Wait until nothing listens on PORT any more; fail loudly if something does.
wait_port_free() {
  local i
  for ((i = 0; i < ${CLI_BRIDGE_PORT_WAIT:-10}; i++)); do
    [[ -z "$(listeners)" ]] && return 0
    sleep 1
  done
  [[ -z "$(listeners)" ]] && return 0
  echo "ERROR: port ${PORT} is still in use - stop this process first, then re-run:" >&2
  listeners >&2
  exit 1
}

preflight_python() {
  if ! "$PYTHON_BIN" -c "import websockets" 2>/dev/null; then
    echo "ERROR: $PYTHON_BIN cannot import websockets (terminal attach needs it)." >&2
    echo "       $PYTHON_BIN -m pip install --user websockets   or set CLI_BRIDGE_PYTHON" >&2
    exit 1
  fi
}

# macOS only: stop older labels that run the same helper and park their plist
# outside LaunchAgents (so launchd never loads it again). Idempotent: a label
# that is neither loaded nor installed is skipped silently.
legacy_labels() {
  local f
  for f in "$HOME"/Library/LaunchAgents/*.plist; do
    [[ -f "$f" ]] || continue
    [[ "$f" == "$PLIST" ]] && continue
    grep -q "scripts/cli-bridge\.py" "$f" && basename "$f" .plist
  done
  for f in ${CLI_BRIDGE_LEGACY_LABELS:-}; do echo "$f"; done
}

retire_legacy_labels() {
  local label plist stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  for label in $(legacy_labels | sort -u); do
    plist="$HOME/Library/LaunchAgents/${label}.plist"
    if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
      launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
      echo "Stopped legacy launchd service ${label}."
    fi
    if [[ -f "$plist" ]]; then
      mkdir -p "$BACKUP_DIR"
      mv "$plist" "$BACKUP_DIR/${label}.plist.bak-${stamp}"
      echo "Moved legacy ${plist} -> $BACKUP_DIR/${label}.plist.bak-${stamp}"
    fi
  done
}

# After start: the helper must listen on BIND only.
check_listen_address() {
  local got
  got="$(listeners | awk '{print $3}' | sort -u | tr '\n' ' ')"
  echo "Listening on: ${got:-<nothing>} (expected ${BIND}:${PORT})"
  if [[ -n "$got" && "$got" != "${BIND}:${PORT} " ]]; then
    echo "WARNING: listener address differs from CLI_BRIDGE_BIND=${BIND}" >&2
  fi
}

case "$(uname -s)" in
  Darwin)
    if [[ "${1:-}" == "--status" ]]; then status; exit $?; fi
    if [[ "${1:-}" == "--remove" ]]; then
        launchctl bootout "gui/$(id -u)/com.mc.cli-bridge" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed cli-bridge service ($PLIST)."
        exit 0
    fi
    preflight_python
    mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
    init_token
    retire_legacy_labels
    launchctl bootout "gui/$(id -u)/com.mc.cli-bridge" 2>/dev/null || true
    wait_port_free
    # launchd does NOT inherit a login-shell PATH — without Homebrew paths
    # the bridge cannot find `tmux` and every provision would fail.
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mc.cli-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${BRIDGE}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>CLI_BRIDGE_PORT</key>
        <string>${PORT}</string>
        <key>CLI_BRIDGE_LOG_DIR</key>
        <string>${LOG_DIR}</string>
        <key>CLI_BRIDGE_BIND</key>
        <string>${BIND}</string>
        <key>CLI_BRIDGE_TOKEN_FILE</key>
        <string>${TOKEN_FILE}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/service.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/service.err.log</string>
</dict>
</plist>
EOF
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart "gui/$(id -u)/com.mc.cli-bridge" 2>/dev/null || true
    sleep 2
    status || {
      echo "Service installed but health check failed — see ${LOG_DIR}/service.err.log"
      exit 1
    }
    check_listen_address
    echo "Installed launchd service com.mc.cli-bridge (starts at login, respawns on crash)."
    ;;
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    if [[ "${1:-}" == "--status" ]]; then status; exit $?; fi
    if [[ "${1:-}" == "--remove" ]]; then
        systemctl --user disable --now mc-cli-bridge.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload
        echo "Removed cli-bridge service ($UNIT)."
        exit 0
    fi
    preflight_python
    mkdir -p "$LOG_DIR" "$UNIT_DIR"
    init_token
    systemctl --user stop mc-cli-bridge.service 2>/dev/null || true
    wait_port_free
    cat > "$UNIT" <<EOF
[Unit]
Description=Mission Control CLI bridge (agent provisioning helper)
After=network.target

[Service]
ExecStart=${PYTHON_BIN} ${BRIDGE}
WorkingDirectory=${REPO_DIR}
Environment=CLI_BRIDGE_PORT=${PORT}
Environment=CLI_BRIDGE_LOG_DIR=${LOG_DIR}
Environment=CLI_BRIDGE_BIND=${BIND}
Environment=CLI_BRIDGE_TOKEN_FILE=${TOKEN_FILE}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now mc-cli-bridge.service
    sleep 2
    status || {
      echo "Service installed but health check failed — journalctl --user -u mc-cli-bridge"
      exit 1
    }
    check_listen_address
    echo "Installed systemd user unit mc-cli-bridge (headless boxes: loginctl enable-linger $USER)."
    if [[ "$BIND" == "127.0.0.1" ]]; then
      echo "NOTE: bound to 127.0.0.1. On Linux the backend container reaches the host via"
      echo "      host-gateway (the docker bridge address, usually 172.17.0.1), not loopback."
      echo "      Re-run with CLI_BRIDGE_BIND=<that address> — see docs/setup/cli-bridge-auth.md."
    fi
    ;;
  *)
    echo "Unsupported OS: $(uname -s) — run scripts/cli-bridge.py under your own supervisor." >&2
    exit 1
    ;;
esac
