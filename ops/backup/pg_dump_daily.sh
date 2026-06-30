#!/usr/bin/env bash
# Daily pg_dump of the helivex DB, with integrity verification and an off-host
# copy. Run by helivex-backup.timer (systemd user, Persistent=true → catches up a
# missed run after an overnight host shutdown — plain cron silently skipped those).
set -euo pipefail

CONTAINER="platform-postgres"
DB_USER="helios"
DB_NAME="helivex"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/helivex/pg}"
# Off-host copy: on WSL2, /mnt/c is the Windows volume — a different disk that
# survives a WSL distro reset. Override with OFFHOST_DIR= for a real remote.
OFFHOST_DIR="${OFFHOST_DIR:-/mnt/c/helivex_backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.dump"

mkdir -p "$BACKUP_DIR"

docker exec "$CONTAINER" \
  pg_dump -U "$DB_USER" -Fc "$DB_NAME" \
  > "$DUMP_FILE"

# Integrity check: a custom-format dump must list its TOC without error, or it is
# not a restorable backup (catches truncation / a mid-dump container restart).
if ! docker exec -i "$CONTAINER" pg_restore --list < "$DUMP_FILE" > /dev/null 2>&1; then
  echo "FATAL: dump failed integrity check (pg_restore --list): $DUMP_FILE" >&2
  rm -f "$DUMP_FILE"
  exit 1
fi

echo "dump: $DUMP_FILE ($(du -sh "$DUMP_FILE" | cut -f1)) — integrity OK"

# Off-host copy (best-effort; warn but don't fail the local backup if absent).
if mkdir -p "$OFFHOST_DIR" 2>/dev/null; then
  cp -f "$DUMP_FILE" "$OFFHOST_DIR/" && echo "off-host copy: $OFFHOST_DIR/$(basename "$DUMP_FILE")"
  find "$OFFHOST_DIR" -name "${DB_NAME}_*.dump" -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || true
else
  echo "WARN: off-host dir $OFFHOST_DIR unavailable — local copy only" >&2
fi

# prune local dumps older than RETENTION_DAYS
find "$BACKUP_DIR" -name "${DB_NAME}_*.dump" -mtime +"$RETENTION_DAYS" -delete

echo "ok: backup complete"
