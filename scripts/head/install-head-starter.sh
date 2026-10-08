#!/bin/sh
# Install the head launcher on this host (opt-in, docs/specs/head-launcher.md §13).
#
#   scripts/head/install-head-starter.sh              copy files + render the plist
#   scripts/head/install-head-starter.sh --gc-apply   same, PLUS arm `mc-head gc`'s
#                                                      deletion (MC_HEAD_GC_APPLY=1 in
#                                                      the plist's own environment —
#                                                      see docs/specs/head-launcher.md §13)
#
# Without --gc-apply (the default, also a re-install without the flag):
# MC_HEAD_GC_APPLY is ABSENT from the plist, so `mc-head gc` stays a dry-run
# report forever — the backend has no switch that can turn this on, by design.
#
# It does NOT load the launchd job — that stays an explicit operator step,
# printed at the end. Uninstall: launchctl bootout gui/$(id -u)/com.mc.head-starter
# and remove $MC_HOME/bin/mc-head.
set -eu
GC_APPLY=0
for arg in "$@"; do
  case "$arg" in
    --gc-apply) GC_APPLY=1 ;;
    *) echo "install-head-starter.sh: unknown argument '$arg' (only --gc-apply is accepted)" >&2; exit 2 ;;
  esac
done
HERE=$(cd "$(dirname "$0")" && pwd)
MC_HOME=${MC_HOME:-"$HOME/.mc"}
mkdir -p "$MC_HOME/bin" "$MC_HOME/heads/spool" "$MC_HOME/logs"
chmod 700 "$MC_HOME/heads"
cp "$HERE/mc-head" "$HERE/head.sb" "$HERE/claude-head-settings.json" "$MC_HOME/bin/"
# omp heads reach their browser session through this relay (ADR-088); mc-head
# looks for it next to itself.
cp "$HERE/../../docker/omp-bridge/cdp_relay.py" "$MC_HOME/bin/"
chmod 755 "$MC_HOME/bin/mc-head"
PLIST="$HOME/Library/LaunchAgents/com.mc.head-starter.plist"
# python3 (not sed/awk) for the __GC_APPLY_ENV__ substitution: it is a
# MULTI-LINE block when armed, and portable multi-line text through a
# shell-quoted -v on BSD/macOS awk is unreliable (confirmed: "awk: newline
# in string" on this exact host). python3 is already a hard dependency of
# mc-head itself (the shebang above every command this installer copies).
python3 - "$HERE/com.mc.head-starter.plist.template" "$PLIST" "$MC_HOME" "$HOME" "$GC_APPLY" <<'PYEOF'
import sys

tmpl_path, out_path, mc_home, home, gc_apply = sys.argv[1:6]
text = open(tmpl_path).read().replace("__MC_HOME__", mc_home).replace("__HOME__", home)
block = (
    "        <key>MC_HEAD_GC_APPLY</key>\n"
    "        <string>1</string>\n"
    if gc_apply == "1" else ""
)
text = text.replace("        <!-- __GC_APPLY_ENV__ -->\n", block)
open(out_path, "w").write(text)
PYEOF
# plutil is macOS-only (launchd itself is too, so a real install always has
# it); the python3 plistlib fallback exists so this script's own exit code
# stays meaningful under test on Linux CI (review finding on PR #751).
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PLIST" >/dev/null
else
  python3 -c 'import plistlib,sys; plistlib.load(open(sys.argv[1], "rb"))' "$PLIST"
fi
echo "Installed $MC_HOME/bin/mc-head and $PLIST"
echo "Before the first real repo (spec §9): put the heads' own GitHub token into"
echo "  $MC_HOME/heads/gh-token (chmod 600). Scratch repos go into $MC_HOME/heads/scratch-repos."
if [ "$GC_APPLY" = 1 ]; then
  echo "mc-head gc deletion is ARMED (MC_HEAD_GC_APPLY=1 in the plist)."
else
  echo "mc-head gc stays dry-run only (no MC_HEAD_GC_APPLY in the plist) — re-run with --gc-apply to arm deletion."
fi
echo "Load the watcher when you are ready:"
echo "  launchctl bootstrap gui/\$(id -u) $PLIST"
