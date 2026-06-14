#!/usr/bin/env bash
set -euo pipefail

CONTAINER="platform-postgres"
DB_USER="helios"
DB_NAME="helivex"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/helivex/pg}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.dump"

mkdir -p "$BACKUP_DIR"

docker exec "$CONTAINER" \
  pg_dump -U "$DB_USER" -Fc "$DB_NAME" \
  > "$DUMP_FILE"

echo "dump: $DUMP_FILE ($(du -sh "$DUMP_FILE" | cut -f1))"

# prune dumps older than RETENTION_DAYS
find "$BACKUP_DIR" -name "${DB_NAME}_*.dump" -mtime +"$RETENTION_DAYS" -delete

echo "ok: backup complete"
