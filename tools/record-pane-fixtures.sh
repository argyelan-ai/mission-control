#!/usr/bin/env bash
# record-pane-fixtures.sh — capture a live tmux pane from a running CLI agent
# container into a golden fixture for the adapter TCK (backend/tests/test_adapter_tck.py).
#
# Why this exists: claude-cli 2.1 broke every scraping heuristic at once
# (NBSP prompt, new spinners, collapse chips) → 6 production bugs fixed live.
# Golden pane fixtures let us regression-test the scraping layer of every
# adapter against REAL terminal output, so the next CLI update is caught by a
# red test instead of a broken fleet. Onboarding a new CLI = record fixtures,
# the TCK picks the directory up automatically.
#
# Usage:
#   tools/record-pane-fixtures.sh <container> <tmux-target> <cli-name> <state>
#
#   <container>    docker container name, e.g. mc-agent-freecode
#   <tmux-target>  tmux target inside the container, e.g. freecode:0
#   <cli-name>     adapter/binary name — becomes the fixture dir + version probe
#                  (claude | openclaude | omp | grok | ...)
#   <state>        idle | working | crashed  (the turn-state the pane shows)
#
# Writes (idempotent — deliberately overwrites):
#   backend/tests/fixtures/panes/<cli-name>/<state>.txt      plain capture (-p)
#   backend/tests/fixtures/panes/<cli-name>/<state>.esc.txt  capture with escapes (-e)
#   backend/tests/fixtures/panes/<cli-name>/meta.json        cli version + image + date
#
# Recording a `working` fixture: submit a SHORT prompt via tmux send-keys and
# capture WHILE the spinner runs. Keep interactions minimal — see the fleet
# guardrails in the W2.1 handoff. This script only captures; the caller drives
# any send-keys.

set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "usage: $0 <container> <tmux-target> <cli-name> <state>" >&2
    exit 2
fi

CONTAINER="$1"
TARGET="$2"
CLI_NAME="$3"
STATE="$4"

case "$STATE" in
    idle|working|crashed) : ;;
    *) echo "error: state must be idle|working|crashed, got '$STATE'" >&2; exit 2 ;;
esac

# Resolve the repo root from this script's location so it works from any cwd
# (agent threads reset cwd between calls).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURE_DIR="$ROOT/backend/tests/fixtures/panes/$CLI_NAME"
mkdir -p "$FIXTURE_DIR"

# Fail loudly if the container is not running — a fixture recorded from a dead
# pane would be silently empty.
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    echo "error: container '$CONTAINER' is not running" >&2
    exit 1
fi

plain_file="$FIXTURE_DIR/$STATE.txt"
esc_file="$FIXTURE_DIR/$STATE.esc.txt"

# Plain capture (what detect_turn_state / detect_pane_ui actually scrape).
docker exec "$CONTAINER" tmux capture-pane -t "$TARGET" -p > "$plain_file"
# Escape-code variant (secondary, for debugging colour/spinner state).
docker exec "$CONTAINER" tmux capture-pane -t "$TARGET" -p -e > "$esc_file"

if [ ! -s "$plain_file" ]; then
    echo "error: captured pane is empty — is '$TARGET' the right tmux target?" >&2
    exit 1
fi

# Probe the CLI version from inside the container. cli-name IS the binary name.
cli_version="$(docker exec "$CONTAINER" "$CLI_NAME" --version 2>/dev/null | head -1 || echo "unknown")"
container_image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || echo "unknown")"
recorded_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Bless the ui-detect heuristic output for this pane. claude-cli 2.1 dropped its
# box glyphs and now looks identical to openclaude in a bare pane, so the
# override-free heuristic can misidentify it (which is exactly why the image
# bakes PANE_UI_OVERRIDE). We record what the heuristic ACTUALLY returns as a
# golden value — a future change that flips the classification of a real pane
# then shows up as a red TCK + a visible meta.json diff for a human to bless.
expected_ui=""
UI_LIB="$ROOT/docker/mc-agent-base/lib/ui-detect.sh"
if [ -f "$UI_LIB" ]; then
    _stub="$(mktemp -d)"
    cat > "$_stub/tmux" <<STUB
#!/usr/bin/env bash
[ "\${1:-}" = "capture-pane" ] && { cat "$plain_file"; exit 0; }
exit 0
STUB
    chmod +x "$_stub/tmux"
    # shellcheck source=/dev/null
    expected_ui="$(PATH="$_stub:$PATH" PANE_UI_OVERRIDE= bash -c "source '$UI_LIB'; detect_pane_ui target:0" 2>/dev/null || echo "")"
    rm -rf "$_stub"
fi

# Update meta.json: preserve the union of recorded states, refresh metadata.
# true_runtime is the ground truth (== cli-name); expected_ui is the blessed
# heuristic output (may differ from true_runtime → documented ambiguity).
python3 - "$FIXTURE_DIR/meta.json" "$CLI_NAME" "$cli_version" "$CONTAINER" "$container_image" "$recorded_at" "$STATE" "$expected_ui" <<'PY'
import json
import sys

meta_path, cli_name, cli_version, container, image, recorded_at, state, expected_ui = sys.argv[1:9]

try:
    with open(meta_path) as f:
        meta = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    meta = {}

states = set(meta.get("states", []))
states.add(state)

meta.update({
    "cli_name": cli_name,
    "true_runtime": cli_name,
    "cli_version": cli_version,
    "container": container,
    "container_image": image,
    "recorded_at": recorded_at,
    "states": sorted(states),
})
# Only (re)bless expected_ui when we could compute it (idle/working panes).
if expected_ui:
    meta["expected_ui"] = expected_ui

with open(meta_path, "w") as f:
    json.dump(meta, f, indent=2)
    f.write("\n")
PY

echo "recorded: $CLI_NAME/$STATE  (version: $cli_version)"
echo "  -> $plain_file"
echo "  -> $esc_file"
echo "  -> $FIXTURE_DIR/meta.json"
