#!/bin/bash
# Set up the local STT server (Parakeet v3 on Apple Silicon) as a launchd
# service. Idempotent — re-running updates the venv and reloads the service.
#
# What this gives you: MC transcribes your voice messages ON THIS MACHINE.
# Point MC at it with STT_BASE_URL=http://host.docker.internal:8585/v1 in .env.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
# The service must not run out of the git checkout: a branch switch or a pruned
# worktree silently invalidates the path, and launchd then retries forever
# (this happened — 7527 failed starts, local dictation dead for two weeks).
# So install into a location no git operation can move.
DIR="$HOME/.mc/stt-server"
VENV="$DIR/.venv"
PLIST_SRC="$DIR/com.mc.stt.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.mc.stt.plist"
LOG_DIR="$HOME/.mc/logs"

# ── Preconditions ─────────────────────────────────────────────────────────
if [ "$(uname -m)" != "arm64" ]; then
    echo "ERROR: MLX needs Apple Silicon (this is $(uname -m))." >&2
    exit 1
fi
if ! command -v ffmpeg >/dev/null; then
    echo "ERROR: ffmpeg missing — install with: brew install ffmpeg" >&2
    exit 1
fi
PY=python3.12
command -v $PY >/dev/null || PY=python3

# ── Install into the stable location ──────────────────────────────────────
if [ "$SRC" != "$DIR" ]; then
    echo "==> installing to $DIR (out of reach of git)"
    mkdir -p "$DIR"
    cp "$SRC/server.py" "$SRC/setup.sh" "$SRC/com.mc.stt.plist" "$SRC/README.md" "$DIR/"
    chmod +x "$DIR/setup.sh"
fi

# ── Venv ──────────────────────────────────────────────────────────────────
echo "==> venv + dependencies ($PY)"
[ -d "$VENV" ] || $PY -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install parakeet-mlx fastapi 'uvicorn[standard]' python-multipart

# ── launchd ───────────────────────────────────────────────────────────────
echo "==> launchd service"
mkdir -p "$LOG_DIR"
# The plist template carries __DIR__ placeholders so the repo can live
# anywhere; rendered here with the real path.
sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

# ── Proof, not vibes: wait for health, then transcribe a real clip ────────
echo "==> waiting for the model (first run downloads ~2 GB) …"
for i in $(seq 1 120); do
    if curl -sf http://127.0.0.1:8585/health >/dev/null 2>&1; then break; fi
    sleep 5
done
curl -sf http://127.0.0.1:8585/health || {
    echo "ERROR: server not healthy — check $LOG_DIR/stt-server.log" >&2
    exit 1
}
echo
echo "==> self-test: synthesising German speech and transcribing it"
TMP=$(mktemp -d)
say -v Anna "Guten Tag, dies ist ein Test der lokalen Transkription." -o "$TMP/test.aiff"
RESULT=$(curl -sf -F "file=@$TMP/test.aiff" http://127.0.0.1:8585/v1/audio/transcriptions)
rm -rf "$TMP"
echo "    $RESULT"
echo "$RESULT" | grep -qi "transkription" || {
    echo "WARNUNG: Selbsttest-Transkript sieht falsch aus — bitte pruefen." >&2
    exit 1
}
echo
echo "OK. Nächster Schritt: in der MC-.env setzen und Backend neu starten:"
echo "    STT_BASE_URL=http://host.docker.internal:8585/v1"
