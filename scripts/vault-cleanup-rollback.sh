#!/usr/bin/env bash
# scripts/vault-cleanup-rollback.sh
# Reverse a vault cleanup run via tarball restore.
#
# Usage: ./scripts/vault-cleanup-rollback.sh <run-id>
# Example: ./scripts/vault-cleanup-rollback.sh 20260515-103044-abc123
#
# Steps:
#   1. Locate tarball ~/.mc/backups/vault-pre-cleanup-<run-id>-*.tar.gz
#   2. Extract to temp dir, rsync over the live vault
#   3. Clear archived_at columns in Postgres for the affected board_memory rows
set -euo pipefail

RUN_ID="${1:?usage: $0 <run-id>}"
VAULT="${HOME}/.mc/vault"
BACKUP_GLOB="${HOME}/.mc/backups/vault-pre-cleanup-${RUN_ID}-*.tar.gz"

TARBALL=$(ls -1 ${BACKUP_GLOB} 2>/dev/null | head -1 || true)
if [[ -z "${TARBALL}" ]]; then
  echo "No tarball found matching: ${BACKUP_GLOB}" >&2
  echo "Available backups:" >&2
  ls -1 "${HOME}/.mc/backups/" 2>/dev/null || echo "  (no backups dir)" >&2
  exit 2
fi

echo "Restoring vault from tarball: ${TARBALL}"
TMP=$(mktemp -d)
trap "rm -rf '${TMP}'" EXIT

tar -xzf "${TARBALL}" -C "${TMP}"
if [[ ! -d "${TMP}/vault" ]]; then
  echo "Tarball does not contain a 'vault/' top-level directory — aborting" >&2
  exit 3
fi

# Optional safety: rename current vault to a side-folder before overwriting
SAFE=""
if [[ -d "${VAULT}" ]]; then
  SAFE="${HOME}/.mc/vault.pre-rollback-$(date -u +%Y%m%d-%H%M%S)"
  echo "Moving current live vault to: ${SAFE}"
  mv "${VAULT}" "${SAFE}"
fi

rsync -a "${TMP}/vault/" "${VAULT}/"
echo "Vault restored to: ${VAULT}"

# Clear archived_at in Postgres
echo "Clearing archived_at columns in board_memory..."
docker compose exec -T db psql -U mc mission_control -c "
  UPDATE board_memory
  SET archived_at = NULL, archive_reason = NULL, archive_bucket = NULL
  WHERE archived_at IS NOT NULL;
"

echo "Rollback complete."
echo "  Restored vault: ${VAULT}"
echo "  Backed-up live vault: ${SAFE:-<none>}"
echo "  Tarball preserved: ${TARBALL}"
