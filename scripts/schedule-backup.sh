#!/bin/bash
set -euo pipefail

# ============================================================
# Mission Control — install a daily automatic backup schedule
#
# Runs ./backup.sh every day at 03:00 (DB dump + ~/.mc archive,
# keeps the last 10). Idempotent — safe to run again.
#
#   ./scripts/schedule-backup.sh            → install
#   ./scripts/schedule-backup.sh --remove   → uninstall
#
# macOS: launchd agent  ~/Library/LaunchAgents/com.mc.backup.plist
# Linux: crontab entry  (tagged "# mission-control-backup")
# ============================================================

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$REPO_DIR/backups/backup.log"
MARKER="mission-control-backup"

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.mc.backup.plist"
    if [[ "${1:-}" == "--remove" ]]; then
        launchctl unload "$PLIST" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed daily backup schedule ($PLIST)."
        exit 0
    fi
    mkdir -p "$REPO_DIR/backups" "$HOME/Library/LaunchAgents"
    # launchd does NOT inherit a login shell PATH — without Homebrew/
    # Docker Desktop paths, `docker compose` inside backup.sh fails silently.
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mc.backup</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO_DIR/backup.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Daily backup installed: 03:00 via launchd ($PLIST)"
    echo "Log: $LOG_FILE"
    ;;

  Linux)
    if [[ "${1:-}" == "--remove" ]]; then
        (crontab -l 2>/dev/null | grep -v "$MARKER") | crontab -
        echo "Removed daily backup schedule (crontab)."
        exit 0
    fi
    mkdir -p "$REPO_DIR/backups"
    CRON_LINE="0 3 * * * cd $REPO_DIR && ./backup.sh >> $LOG_FILE 2>&1 # $MARKER"
    (crontab -l 2>/dev/null | grep -v "$MARKER"; echo "$CRON_LINE") | crontab -
    echo "Daily backup installed: 03:00 via crontab"
    echo "Log: $LOG_FILE"
    ;;

  *)
    echo "Unsupported OS: $(uname -s) — schedule backup.sh manually." >&2
    exit 1
    ;;
esac
