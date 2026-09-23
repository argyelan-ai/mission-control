#!/bin/sh
# Install the head launcher on this host (opt-in, docs/specs/head-launcher.md §13).
#
#   scripts/head/install-head-starter.sh            copy files + render the plist
#
# It does NOT load the launchd job — that stays an explicit operator step,
# printed at the end. Uninstall: launchctl bootout gui/$(id -u)/com.mc.head-starter
# and remove $MC_HOME/bin/mc-head.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
MC_HOME=${MC_HOME:-"$HOME/.mc"}
mkdir -p "$MC_HOME/bin" "$MC_HOME/heads/spool" "$MC_HOME/logs"
chmod 700 "$MC_HOME/heads"
cp "$HERE/mc-head" "$HERE/head.sb" "$HERE/claude-head-settings.json" "$MC_HOME/bin/"
chmod 755 "$MC_HOME/bin/mc-head"
PLIST="$HOME/Library/LaunchAgents/com.mc.head-starter.plist"
sed -e "s|__MC_HOME__|$MC_HOME|g" -e "s|__HOME__|$HOME|g" \
  "$HERE/com.mc.head-starter.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
echo "Installed $MC_HOME/bin/mc-head and $PLIST"
echo "Before the first real repo (spec §9): put the heads' own GitHub token into"
echo "  $MC_HOME/heads/gh-token (chmod 600). Scratch repos go into $MC_HOME/heads/scratch-repos."
echo "Load the watcher when you are ready:"
echo "  launchctl bootstrap gui/\$(id -u) $PLIST"
