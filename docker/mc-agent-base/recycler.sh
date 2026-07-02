#!/bin/bash
# recycler.sh — claude-process Watchdog (Phase 3, MEM-01).
# Lebt in tmux Window 2. Pollt alle 60s den claude-Prozess in Window 0
# und respawnt ihn (`tmux respawn-pane -t {session}:0 -k`) wenn:
#   1. idle ≥ 15 min (mtime von /home/agent/.claude/last-task.marker), ODER
#   2. claude RSS > $RECYCLER_RSS_MB_THRESHOLD MB (Default 1500), ODER
#   3. (debounce: nur wenn letzter Recycle ≥ 5 min her ist)
#
# Guard: TASK_LOCK_FILE (/home/agent/.task-active.lock) — wenn poll.sh einen
# aktiven Task hat (Datei existiert + poll.sh läuft), wird idle-Kill geblockt.
# Stale-Lock-Schutz: wenn poll.sh nicht mehr läuft (pgrep), Lock ignorieren.
#
# Window 1 (poll.sh) wird NICHT angefasst — Tasks-Polling laeuft durch.
# Kill-Switch: AGENT_RECYCLER_ENABLED=false → no-op via exec sleep infinity.
#
# Sparky-Scope: pgrep -x claude (exact basename). openclaude matched NICHT
# → Sparky no-op silent. Der Operator kann RECYCLER_PROCESS_NAME setzen falls er
# Sparky einbeziehen will (deferred per CONTEXT Open Question 1).
#
# Marker-Bootstrap: immer unconditional touch $MARKER beim Start → jeder neu
# gestartete Container bekommt ein frisches Idle-Fenster. Stale mtime auf dem
# persistenten Host-Mount kann sonst sofortigen idle-Recycle auslösen (Fix 2026-06-27).
#
# Logs an PID-1 stdout (Pitfall 6): echo ... >> /proc/1/fd/1 damit
# docker logs mc-agent-{slug} die Zeilen sieht.
#
# Siehe ADR-024 (Claude-Process Recycling) fuer Design + Rollback.

set -euo pipefail

# Shared process-inspection helpers (proc_rss_mb / proc_state / proc_alive).
# Synced from docker/shared/recycler-lib.sh via scripts/build-agent-images.sh.
# shellcheck source=/dev/null
. "$(dirname "$0")/recycler-lib.sh"

SESSION="${AGENT_NAME:-agent}"
INTERVAL="${RECYCLER_INTERVAL_SECONDS:-60}"
IDLE_THRESHOLD_MIN="${RECYCLER_IDLE_MIN:-15}"
RSS_THRESHOLD_MB="${RECYCLER_RSS_MB_THRESHOLD:-1500}"
DEBOUNCE_MIN="${RECYCLER_DEBOUNCE_MIN:-5}"
PROCESS_NAME="${RECYCLER_PROCESS_NAME:-claude}"
MARKER="/home/agent/.claude/last-task.marker"
RECYCLE_MARKER="/tmp/recycler-last-recycle.epoch"
TASK_LOCK_FILE="/home/agent/.task-active.lock"

# ── V5 Input Validation: RSS_THRESHOLD_MB must be a positive integer ≥100 ──
if ! [[ "$RSS_THRESHOLD_MB" =~ ^[0-9]+$ ]] || [ "$RSS_THRESHOLD_MB" -lt 100 ]; then
    echo "[agent-recycler] FATAL: RECYCLER_RSS_MB_THRESHOLD must be positive integer >=100, got: $RSS_THRESHOLD_MB" >> /proc/1/fd/1
    exec sleep infinity
fi

# ── Log helper: write to PID-1 stdout so docker logs sees it (Pitfall 6) ──
log() {
    echo "[agent-recycler $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> /proc/1/fd/1
}

# ── Kill-switch: strict parse, fail-closed (V5: anything != "true" disables) ──
if [ "${AGENT_RECYCLER_ENABLED:-true}" != "true" ]; then
    log "disabled (AGENT_RECYCLER_ENABLED=${AGENT_RECYCLER_ENABLED:-unset}) — sleeping forever"
    exec sleep infinity
fi

log "starting (session=$SESSION interval=${INTERVAL}s idle=${IDLE_THRESHOLD_MIN}min rss_max=${RSS_THRESHOLD_MB}MB debounce=${DEBOUNCE_MIN}min process=$PROCESS_NAME)"

# ── Bootstrap marker: always refresh mtime on (re)start ──
# The marker lives on a persistent host-mount (~/.mc/agents/<slug>/claude-config →
# /home/agent/.claude) and survives container recreates with its old mtime intact.
# A conditional [ -f ] || touch would skip the refresh → the recycler sees an idle
# window of "hours since last session" and fires an immediate idle-recycle, killing
# the agent mid-task (live incident 2026-06-26, idle_min=2279).
# Unconditional touch gives every freshly (re)started container a clean slate.
mkdir -p "$(dirname "$MARKER")"; touch "$MARKER"

do_recycle() {
    local trigger="$1" rss="$2" idle="$3"
    # Re-read mtime IMMEDIATELY before kill (Pitfall 3 — race window).
    local now last fresh_idle
    now=$(date +%s)
    last=$(stat -c %Y "$MARKER" 2>/dev/null || echo "$now")
    fresh_idle=$(( (now - last) / 60 ))
    if [ "$trigger" = "idle" ] && [ "$fresh_idle" -lt "$IDLE_THRESHOLD_MIN" ]; then
        log "abort recycle: dispatch arrived during decision (idle was ${idle}min, now ${fresh_idle}min)"
        return
    fi
    # Bug 3 fix (2026-05-13): log "recycled" NACH erfolgreichem respawn,
    # nicht davor. Vorher loggte Recycler "recycled claude (rss_mb=0,...)"
    # auch wenn tmux respawn-pane fehlschlug oder die PID nie passte → die
    # Logs widersprachen der Realitaet (Sparky lief munter weiter trotz
    # "recycled"-Eintrag).
    if ! tmux respawn-pane -t "${SESSION}:0" -k 2>/dev/null; then
        log "ERROR: tmux respawn-pane failed — session=$SESSION may be missing (no-op, claude still running)"
        return
    fi
    log "recycled claude (trigger=${trigger}, rss_mb=${rss}, idle_min=${idle})"
    echo "$now" > "$RECYCLE_MARKER" 2>/dev/null || true
}

# ── Main loop ──
while true; do
    sleep "$INTERVAL"

    # Cooldown / debounce check (Pattern 5)
    if [ -f "$RECYCLE_MARKER" ]; then
        LAST_RECYCLE=$(cat "$RECYCLE_MARKER" 2>/dev/null || echo 0)
        NOW=$(date +%s)
        SINCE_LAST_MIN=$(( (NOW - LAST_RECYCLE) / 60 ))
        if [ "$SINCE_LAST_MIN" -lt "$DEBOUNCE_MIN" ]; then
            continue
        fi
    fi

    # Find target process PID — exact basename match (Pitfall 4)
    PID=$(pgrep -x "$PROCESS_NAME" 2>/dev/null | head -1 || true)
    if [ -z "$PID" ]; then
        # process not running yet (mid-restart, or Sparky's openclaude case) — skip
        continue
    fi
    # Process inspection via /proc — works on both BusyBox (Alpine) and GNU userlands.
    # proc_alive returns false for zombies/dead/missing — replaces the old kill -0 check
    # which couldn't distinguish "alive but stopped" from "zombie".
    STATE=$(proc_state "$PID")
    if ! proc_alive "$PID"; then
        log "skip: PID=$PID state=$STATE (not alive — zombie/dead/missing, process=$PROCESS_NAME)"
        continue
    fi

    # RSS in MB via /proc/$PID/status VmRSS line.
    # Bug fix 2026-05-17: previously `ps -o rss= -p $PID` on BusyBox failed
    # (no -p flag) → false-positive "rss-unreadable (zombie/dead)" every minute
    # on Alpine-based agents. /proc is distribution-agnostic.
    RSS_MB=$(proc_rss_mb "$PID")
    if [ -z "$RSS_MB" ]; then
        log "skip: PID=$PID VmRSS unreadable (kernel thread? proc-entry race?). state=$STATE process=$PROCESS_NAME"
        continue
    fi

    # Idle calc
    NOW=$(date +%s)
    LAST=$(stat -c %Y "$MARKER" 2>/dev/null || echo "$NOW")
    IDLE_MIN=$(( (NOW - LAST) / 60 ))

    # Task-Lock-Guard: wenn poll.sh einen aktiven Task hat UND noch läuft,
    # idle-Kill blocken. Stale-Lock-Schutz: Lock nur respektieren wenn poll.sh
    # noch läuft (verhindert ewigen Block nach poll.sh-Crash).
    if [ "$IDLE_MIN" -ge "$IDLE_THRESHOLD_MIN" ] && [ -f "$TASK_LOCK_FILE" ]; then
        LOCK_TASK_ID=$(cat "$TASK_LOCK_FILE" 2>/dev/null || echo "")
        if [ -n "$LOCK_TASK_ID" ] && pgrep -f "poll.sh" > /dev/null 2>&1; then
            log "abort recycle: active task=$LOCK_TASK_ID (lock file present, poll.sh running) — idle=${IDLE_MIN}min"
            continue
        fi
        log "stale lock detected (task=$LOCK_TASK_ID, poll.sh not running) — proceeding with recycle"
    fi

    # Decision (idle takes precedence — primary path; threshold is safety net)
    if [ "$IDLE_MIN" -ge "$IDLE_THRESHOLD_MIN" ]; then
        do_recycle "idle" "$RSS_MB" "$IDLE_MIN"
    elif [ "$RSS_MB" -gt "$RSS_THRESHOLD_MB" ]; then
        do_recycle "threshold" "$RSS_MB" "$IDLE_MIN"
    fi
done
