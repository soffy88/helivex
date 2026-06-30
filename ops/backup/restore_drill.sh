#!/usr/bin/env bash
# Restore drill: prove the latest pg_dump is actually restorable into a scratch DB.
# Uses the TimescaleDB pre/post-restore procedure (plain pg_restore mishandles
# hypertables + continuous aggregates — the circular-FK / --disable-triggers hint
# the audit flagged). Verifies a few row counts, then drops the scratch DB.
#
# Run manually after changing the backup, or on a schedule to catch silent rot.
set -euo pipefail

CONTAINER="platform-postgres"
DB_USER="helios"
SRC_DB="helivex"
TEST_DB="helivex_restore_test"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/helivex/pg}"
DUMP="${1:-$(ls -t "$BACKUP_DIR"/${SRC_DB}_*.dump | head -1)}"

echo "restore drill using: $DUMP"

docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB;"
docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "CREATE DATABASE $TEST_DB;"
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$TEST_DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$TEST_DB" -tAc "SELECT timescaledb_pre_restore();" >/dev/null

docker exec -i "$CONTAINER" pg_restore -U "$DB_USER" -d "$TEST_DB" --no-owner < "$DUMP" 2>&1 \
  | grep -vE "already exists|continuous_agg|--disable-triggers|warning: errors ignored" || true

docker exec "$CONTAINER" psql -U "$DB_USER" -d "$TEST_DB" -tAc "SELECT timescaledb_post_restore();" >/dev/null

echo "=== restored row counts (compare to source) ==="
for tbl in market_data.ohlcv_1h market_data.binance_funding_history paper.signals paper.fills; do
  src=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$SRC_DB"  -tAc "SELECT count(*) FROM $tbl;" 2>/dev/null || echo "ERR")
  rst=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$TEST_DB" -tAc "SELECT count(*) FROM $tbl;" 2>/dev/null || echo "ERR")
  flag="OK"; [ "$src" != "$rst" ] && flag="MISMATCH"
  printf "  %-40s src=%s restored=%s  [%s]\n" "$tbl" "$src" "$rst" "$flag"
done

docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "DROP DATABASE $TEST_DB;" >/dev/null
echo "ok: restore drill complete (scratch DB dropped)"
