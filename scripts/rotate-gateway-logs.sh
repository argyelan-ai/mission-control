#!/bin/bash
# rotate-gateway-logs.sh
#
# Truncates OpenClaw Gateway logs safely while the daemon is running.
# openclaw-gateway haelt die log files mit offenen FDs — wir koennen sie
# nicht loeschen oder umbenennen (sonst schreibt der Daemon in die alte
# Datei ohne Pfad). Stattdessen: Inhalt ueber `: > file` leeren. Der
# Daemon schreibt weiter an dieselbe inode, nur von Byte 0 an.
#
# Optional: Die letzten N Zeilen werden vorher in ein dated Backup
# kopiert, damit frische Errors fuer Debugging bleiben.
#
# Usage:
#   ./scripts/rotate-gateway-logs.sh            # truncate, keep last 500 lines as backup
#   ./scripts/rotate-gateway-logs.sh 1000       # keep last 1000 lines
#   ./scripts/rotate-gateway-logs.sh 0          # truncate without backup
#
# Installiere als daily-cron via `crontab -e`:
#   0 4 * * * ${HOME}/Workspace/Projects/mission-control/scripts/rotate-gateway-logs.sh

set -euo pipefail

LOG_DIR="${HOME}/.openclaw/logs"
BACKUP_LINES="${1:-500}"

if [ ! -d "$LOG_DIR" ]; then
    echo "ERROR: $LOG_DIR nicht gefunden" >&2
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)

for f in gateway.log gateway.err.log; do
    file="$LOG_DIR/$f"
    if [ ! -f "$file" ]; then
        continue
    fi

    size_before=$(du -h "$file" | awk '{print $1}')

    # Backup der letzten N Zeilen (falls BACKUP_LINES > 0)
    if [ "$BACKUP_LINES" -gt 0 ]; then
        backup="$LOG_DIR/${f%.log}.${TIMESTAMP}.tail.log"
        tail -n "$BACKUP_LINES" "$file" > "$backup" 2>/dev/null || true
        echo "  backup: $backup ($(du -h "$backup" 2>/dev/null | awk '{print $1}'))"
    fi

    # In-place truncate — macht den open FD auf Byte 0 zurueckgesetzt
    : > "$file"
    echo "  truncated: $f ($size_before → 0B)"
done

# Aeltere backups (> 7 Tage) loeschen
find "$LOG_DIR" -name "gateway*.tail.log" -mtime +7 -delete 2>/dev/null || true

echo "Done."
