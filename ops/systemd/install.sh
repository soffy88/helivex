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
systemctl --user enable helivex.target
for svc in gw web paper monitor l2recorder cf; do
  systemctl --user enable "helivex-$svc.service" 2>/dev/null || true
done
for t in "$SRC"/helivex-*.timer; do
  systemctl --user enable --now "$(basename "$t")"
done

echo "installed. start everything with:  systemctl --user start helivex.target"
echo "current state:"
systemctl --user list-units 'helivex-*' --no-pager
