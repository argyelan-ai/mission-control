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
# ============================================================

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE="$REPO_DIR/scripts/cli-bridge.py"
PORT="${CLI_BRIDGE_PORT:-18792}"
LOG_DIR="${CLI_BRIDGE_LOG_DIR:-$HOME/.mc/logs/cli-bridge}"
PYTHON_BIN="$(command -v python3)"
BIND="${CLI_BRIDGE_BIND:-127.0.0.1}"
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
  if curl -sf "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1; then
    echo "cli-bridge: RUNNING (port ${PORT})"
    return 0
  fi
  echo "cli-bridge: NOT reachable on port ${PORT}"
  return 1
}

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.mc.cli-bridge.plist"
    if [[ "${1:-}" == "--status" ]]; then status; exit $?; fi
    if [[ "${1:-}" == "--remove" ]]; then
        launchctl bootout "gui/$(id -u)/com.mc.cli-bridge" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed cli-bridge service ($PLIST)."
        exit 0
    fi
    mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
    init_token
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
    launchctl bootout "gui/$(id -u)/com.mc.cli-bridge" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart "gui/$(id -u)/com.mc.cli-bridge" 2>/dev/null || true
    sleep 2
    status || {
      echo "Service installed but health check failed — see ${LOG_DIR}/service.err.log"
      exit 1
    }
    echo "Installed launchd service com.mc.cli-bridge (starts at login, respawns on crash)."
    ;;
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT="$UNIT_DIR/mc-cli-bridge.service"
    if [[ "${1:-}" == "--status" ]]; then status; exit $?; fi
    if [[ "${1:-}" == "--remove" ]]; then
        systemctl --user disable --now mc-cli-bridge.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload
        echo "Removed cli-bridge service ($UNIT)."
        exit 0
    fi
    mkdir -p "$LOG_DIR" "$UNIT_DIR"
    init_token
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
