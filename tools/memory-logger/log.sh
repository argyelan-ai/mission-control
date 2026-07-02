#!/bin/bash
# memory-logger/log.sh — Snapshot aller MC-Container + Per-Prozess RSS
# Output: CSV in ~/Library/Logs/openclaw-memlog/snapshots-YYYY-MM-DD.csv

set -u

# Detect timeout binary (macOS: gtimeout via coreutils; Linux: timeout)
if command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT="gtimeout 10"
elif command -v timeout >/dev/null 2>&1; then
    TIMEOUT="timeout 10"
else
    TIMEOUT=""  # no timeout available, accept the risk
fi

LOG_DIR="$HOME/Library/Logs/openclaw-memlog"
DATE=$(date '+%Y-%m-%d')
TS=$(date '+%Y-%m-%dT%H:%M:%S')
CSV="$LOG_DIR/snapshots-$DATE.csv"

mkdir -p "$LOG_DIR"

# Header beim ersten Schreiben
if [ ! -f "$CSV" ]; then
  echo "timestamp,container,total_mb,claude_mb,bun_mb,node_mb,playwright_count" > "$CSV"
fi

# Alle MC-Agent-Container abklappern
for c in $(docker ps --filter "name=mc-agent" --format "{{.Names}}"); do
  total=$($TIMEOUT docker stats --no-stream --format "{{.MemUsage}}" "$c" 2>/dev/null | awk '{print $1}' | sed 's/MiB//;s/GiB/*1024/' | bc 2>/dev/null || echo "0")

  claude=$($TIMEOUT docker exec "$c" sh -c '
    for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do
      name=$(awk "/^Name:/ {print \$2}" /proc/$pid/status 2>/dev/null)
      rss=$(awk "/^VmRSS:/ {print \$2}" /proc/$pid/status 2>/dev/null)
      [ "$name" = "claude" ] && echo "$((rss/1024))"
    done
  ' 2>/dev/null | head -1)

  bun=$($TIMEOUT docker exec "$c" sh -c '
    total=0
    for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do
      name=$(awk "/^Name:/ {print \$2}" /proc/$pid/status 2>/dev/null)
      rss=$(awk "/^VmRSS:/ {print \$2}" /proc/$pid/status 2>/dev/null)
      [ "$name" = "bun" ] && total=$((total + rss/1024))
    done
    echo $total
  ' 2>/dev/null)

  node=$($TIMEOUT docker exec "$c" sh -c '
    total=0
    for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do
      name=$(awk "/^Name:/ {print \$2}" /proc/$pid/status 2>/dev/null)
      rss=$(awk "/^VmRSS:/ {print \$2}" /proc/$pid/status 2>/dev/null)
      [ "$name" = "node" ] && total=$((total + rss/1024))
    done
    echo $total
  ' 2>/dev/null)

  pw=$($TIMEOUT docker exec "$c" sh -c '
    ps -ef 2>/dev/null | grep -c "[p]laywright"
  ' 2>/dev/null)

  echo "$TS,$c,${total:-0},${claude:-0},${bun:-0},${node:-0},${pw:-0}" >> "$CSV"
done
