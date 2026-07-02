#!/bin/bash
set -euo pipefail

# ============================================================
# Mission Control — Datenbank Backup
# Sichert die PostgreSQL-Datenbank aus Docker
#
# Verwendung:
#   ./backup.sh              → Erstellt ein Backup
#   ./backup.sh restore      → Stellt das letzte Backup wieder her
#   ./backup.sh restore <file> → Stellt ein bestimmtes Backup wieder her
# ============================================================

BACKUP_DIR="./backups"
CONTAINER="mission-control-db-1"
DB_NAME="mission_control"
DB_USER="mc"
KEEP_LAST=10  # Anzahl Backups die behalten werden

mkdir -p "$BACKUP_DIR"

# --- Backup ---
if [[ "${1:-}" != "restore" ]]; then
    TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
    FILENAME="${BACKUP_DIR}/mc_backup_${TIMESTAMP}.sql.gz"

    echo "Starte Backup..."
    docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$FILENAME"

    SIZE=$(du -h "$FILENAME" | cut -f1)
    echo "DB-Backup erstellt: $FILENAME ($SIZE)"

    # MC-Datenverzeichnis sichern (Agent-Configs, Vault, Skills, MCP, Secrets).
    # WICHTIG: ~/.mc ist die ECHTE Quelle (ADR-022). Frueher wurde faelschlich
    # ~/.openclaw gesichert — das ist nur noch ein Legacy-Ordner mit toten
    # Pre-Migration-Daten + Symlinks auf ~/.mc; tar folgt Symlinks NICHT, also
    # fehlten die echten Daten komplett im Backup. Korrigiert 2026-06-01.
    MC_DATA_DIR="$HOME/.mc"
    if [[ -d "$MC_DATA_DIR" ]]; then
        DATA_FILENAME="${BACKUP_DIR}/mc_data_${TIMESTAMP}.tar.gz"
        tar -czf "$DATA_FILENAME" \
            -C "$HOME" \
            --exclude='*/node_modules' \
            --exclude='*/.venv' \
            --exclude='*/__pycache__' \
            --exclude='*/.git' \
            .mc/ 2>/dev/null || true
        DATA_SIZE=$(du -h "$DATA_FILENAME" | cut -f1)
        echo "MC-Daten-Backup erstellt: $DATA_FILENAME ($DATA_SIZE)"
    fi

    # Alte Backups aufräumen (nur die letzten $KEEP_LAST behalten)
    BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/mc_backup_*.sql.gz 2>/dev/null | wc -l)
    if [[ "$BACKUP_COUNT" -gt "$KEEP_LAST" ]]; then
        ls -1t "$BACKUP_DIR"/mc_backup_*.sql.gz | tail -n +$((KEEP_LAST + 1)) | xargs rm -f
        echo "Alte DB-Backups aufgeräumt (behalte die letzten $KEEP_LAST)"
    fi

    DATA_COUNT=$(ls -1 "$BACKUP_DIR"/mc_data_*.tar.gz 2>/dev/null | wc -l)
    if [[ "$DATA_COUNT" -gt "$KEEP_LAST" ]]; then
        ls -1t "$BACKUP_DIR"/mc_data_*.tar.gz | tail -n +$((KEEP_LAST + 1)) | xargs rm -f
        echo "Alte MC-Daten-Backups aufgeräumt (behalte die letzten $KEEP_LAST)"
    fi

    echo "Fertig."

# --- Restore ---
else
    if [[ -n "${2:-}" ]]; then
        RESTORE_FILE="$2"
    else
        RESTORE_FILE=$(ls -1t "$BACKUP_DIR"/mc_backup_*.sql.gz 2>/dev/null | head -1)
    fi

    if [[ -z "$RESTORE_FILE" || ! -f "$RESTORE_FILE" ]]; then
        echo "Kein Backup gefunden."
        exit 1
    fi

    echo "ACHTUNG: Das überschreibt die aktuelle Datenbank!"
    echo "Backup: $RESTORE_FILE"
    read -p "Fortfahren? (j/n): " CONFIRM
    if [[ "$CONFIRM" != "j" ]]; then
        echo "Abgebrochen."
        exit 0
    fi

    echo "Stelle Backup wieder her..."
    gunzip -c "$RESTORE_FILE" | docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" --quiet
    echo "Datenbank wiederhergestellt aus: $RESTORE_FILE"
fi
