#!/bin/sh
# Install the nightly journey run on this host (docs/journeys.md).
#
#   e2e/install-journeys.sh          copy the runner + render the plist
#
# It does NOT load the launchd job — that stays an explicit operator step,
# printed at the end. Uninstall: launchctl bootout gui/$(id -u)/com.mc.journeys,
# then remove $MC_HOME/bin/mc-journeys and the plist.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
MC_HOME=${MC_HOME:-"$HOME/.mc"}
REPO=${MC_JOURNEYS_REPO:-$(git -C "$HERE" remote get-url origin)}
for tool in docker node npm git python3 openssl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing: $tool" >&2; exit 1; }
done
mkdir -p "$MC_HOME/bin" "$MC_HOME/journeys" "$MC_HOME/logs"
cp "$HERE/run-journeys.sh" "$MC_HOME/bin/mc-journeys"
chmod 755 "$MC_HOME/bin/mc-journeys"
PLIST="$HOME/Library/LaunchAgents/com.mc.journeys.plist"
sed -e "s|__MC_HOME__|$MC_HOME|g" -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO|g" \
  "$HERE/com.mc.journeys.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
echo "Installed $MC_HOME/bin/mc-journeys and $PLIST (repo: $REPO)"
echo "Try one run by hand first:  $MC_HOME/bin/mc-journeys"
echo "Then load the nightly job:  launchctl bootstrap gui/\$(id -u) $PLIST"
