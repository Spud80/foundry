#!/usr/bin/env bash
# cron-entry.sh - cron-skall for memory-extract under CP schedule-takeover.
#
# Crontab (cron.d/memory-extract.cron) peker hit, IKKE direkte pa run.sh. Skallet
# sjekker om control-plane allerede har enqueuet dagens kjoring (cp_handled_today)
# og hopper i sa fall over - cronen er FALLBACK, CP eier WHEN. Selve jobben
# (run.sh) er uendret og invokeres av CP-lanen direkte; dette skallet er kun
# cron-siden, sa run.sh forblir ren WHAT uten modus-flagg.
#
# Fail-open: ENHVER feil i sjekken (CP ikke deployet, DB nede, sudo-feil) skal
# kjore extracten - en dag skal aldri droppes stille. Kun eksplisitt exit 0
# (= CP har enqueuet i dag) hopper over; exit 10 og alt annet betyr RUN.
set -uo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CP_DIR="/opt/control-plane"

if sudo runuser -u control-plane -- bash -c "cd ${CP_DIR} && set -a && . /etc/control-plane/control-plane.env && set +a && .venv/bin/python -m src.control_plane.cron_fallback foundry-memory-extract"; then
  exit 0
fi
exec "${JOB_DIR}/run.sh"
