#!/usr/bin/env bash
# Rebuild the helivex stack on a FRESH host — the recovery path for the single-host
# SPOF. Compute can't be made HA without a second machine, but this makes a rebuild
# a guided, repeatable ~30-min procedure instead of tribal knowledge. Run from the
# cloned repo root. It automates what it safely can and STOPS at each manual step.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "MISSING: $1 — install it first"; exit 1; }; }

say "0. Prereqs"
for c in git python3 docker node npm; do need "$c"; done
echo "ok: git python3 docker node npm present"

say "1. Secrets (.env is NOT in git)"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. EDIT IT NOW with: OKX_* keys, HELIVEX_AUDIT_*"
  echo "keys, TG_* token, HELIVEX_GW_TOKEN, DASH_USER/DASH_PASS, OKX_WS_PROXY."
  echo "Then: chmod 600 .env  &&  re-run this script."
  exit 0
fi
chmod 600 .env
echo "ok: .env present (0600)"

say "2. Python env + platform 3O libs"
[ -d venv ] || python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q -U pip
echo ">> Install requirements + the platform/3O editable libs (oprim/oskill/omodul)."
echo "   (These live in ../../platform/3O — clone that repo too if absent.)"

say "3. Postgres (platform-postgres docker container on :5434)"
if ! docker ps --format '{{.Names}}' | grep -q '^platform-postgres$'; then
  echo ">> Start the platform-postgres container (shared with helios). Then re-run."
  exit 0
fi
echo "ok: platform-postgres running"

say "4. Restore the latest DB dump"
DUMP="$(ls -t "$HOME"/backups/helivex/pg/helivex_*.dump /mnt/c/helivex_backups/helivex_*.dump 2>/dev/null | head -1 || true)"
if [ -n "$DUMP" ]; then
  echo ">> Latest dump: $DUMP"
  echo ">> Restore with the TimescaleDB procedure (see ops/backup/restore_drill.sh for"
  echo "   pre/post_restore). Into the REAL db:  createdb helivex; pre_restore; pg_restore; post_restore."
else
  echo "!! No dump found in ~/backups or /mnt/c — DB will start empty (forward-collect resumes)."
fi

say "5. Frontend build"
( cd helivex-web && npm ci && npm run build )

say "6. systemd user units + linger"
loginctl enable-linger "$USER" || true
bash ops/systemd/install.sh

say "7. Start everything"
echo ">> systemctl --user start helivex.target"
echo ">> Verify: systemctl --user list-units 'helivex-*'  &&  curl -s localhost:8765/health"
echo
echo "Done. Residual SPOF: this is still one host. True HA needs Postgres + a node"
echo "on a second machine — a hardware/provisioning decision, documented in ops/RUNBOOK.md."
