#!/usr/bin/env bash
# Install/refresh all helivex systemd *user* units from this repo directory into
# ~/.config/systemd/user, reload, and (re)enable services + timers. Idempotent.
# Run after editing any unit here so the live config matches version control.
#
#   bash ops/systemd/install.sh
#
# Requires linger so user services survive logout:  loginctl enable-linger "$USER"
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/.config/systemd/user"
mkdir -p "$DST"

cp -v "$SRC"/helivex-*.service "$SRC"/helivex-*.timer "$SRC"/helivex.target "$DST"/ 2>/dev/null || true

systemctl --user daemon-reload

# Long-running services (via the target) + standalone timers.
# l2recorder is retired (superseded by helivex-orderbook-md-adapter.timer, which
# reads OKX LIVE from iris/md instead of the recorder's OKX DEMO) — not enabled
# here. Its unit file is kept on disk for rollback; enable it manually if needed.
systemctl --user enable helivex.target
for svc in gw web paper monitor cf; do
  systemctl --user enable "helivex-$svc.service" 2>/dev/null || true
done

# Retired direct-collector timers, superseded by the *-md-adapter timers below
# (iris/md consolidation). Unit files kept on disk for rollback only — NOT
# enabled by this script, so a re-install can't silently resurrect them.
#   ohlcv-4h/5m (swap)   -> helivex-ohlcv-md-adapter.timer      (12a790c)
#   ohlcv-1h (spot)      -> helivex-ohlcv-md-adapter.timer      (spot follow-up)
#   ingest-funding-binance -> helivex-funding-md-adapter.timer  (f708e9f, never
#     actually installed live, but the file exists here — must stay excluded)
RETIRED_TIMERS=(
  helivex-ingest-ohlcv-4h.timer
  helivex-ingest-ohlcv-5m.timer
  helivex-ingest-ohlcv-1h.timer
  helivex-ingest-funding-binance.timer
)
for t in "$SRC"/helivex-*.timer; do
  name="$(basename "$t")"
  skip=false
  for r in "${RETIRED_TIMERS[@]}"; do
    [ "$name" = "$r" ] && skip=true && break
  done
  if $skip; then
    echo "skip (retired): $name"
    continue
  fi
  systemctl --user enable --now "$name"
done

echo "installed. start everything with:  systemctl --user start helivex.target"
echo "current state:"
systemctl --user list-units 'helivex-*' --no-pager
