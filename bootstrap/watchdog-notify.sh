#!/usr/bin/env bash
# watchdog-notify.sh - foundry watchdog-entrypoint, stand-alone.
#
# Installeres til ~/.config/foundry/watchdog-notify.sh av setup-linux.sh,
# sammen med notify-core.sh i samme dir. Ingen repo-avhengighet etter
# setup-linux.sh er kjort - fungerer hvis ~/foundry/ slettes/flyttes.
#
# To modi:
#   1. Default (kalt av cron uten args): sjekker ~/foundry/logs/auto-update.log
#      for OK-linje innenfor siste WINDOW_MIN min (default 45). Hvis ikke
#      fersk-OK, sender Telegram-varsel via notify-core.
#   2. Direct-send (kalt med ett arg): tar arg som meldings-streng og
#      sender direkte. Brukes av tests, manuell debug, og av andre cron-jobber
#      som vil sende ad-hoc varsel uten egen wrapper.
#
# Avvik fra PLAN-foundry: PLAN line 154 antyder watchdog-notify er en ren
# notify-entrypoint og at sjekken bor i cron-linja. Denne implementeringen
# legger sjekken inn i scriptet for at cron skal forbli en enkel one-liner
# og for at acceptance #4 (kjorer uten ~/foundry/-tre) skal kunne testes
# direkte uten cron-wrapper. Watchdog-notify-navnet er beholdt per file map.

set -uo pipefail

LOG_FILE="${FOUNDRY_AUTO_UPDATE_LOG:-${HOME}/foundry/logs/auto-update.log}"
WINDOW_MIN="${FOUNDRY_WATCHDOG_WINDOW_MIN:-45}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify-core.sh"

# Direct-send mode
if [ $# -gt 0 ]; then
  enqueue_msg "$1" >/dev/null
  drain_queue
  exit $?
fi

# Default: check freshness of auto-update.log.
# Hvis loggfila ikke eksisterer (Phase 500 ikke deployet enno) eller er
# tom: exit silent. Watchdog er forst aktiv naar noe begynner a skrive
# til loggen. Dette unngar at watchdog akkumulerer falske alarmer i
# Phase 200-til-Phase 500-vinduet.
if [ ! -s "$LOG_FILE" ]; then
  exit 0
fi

recent="$(find "$LOG_FILE" -mmin "-${WINDOW_MIN}" -print 2>/dev/null || true)"
if [ -n "$recent" ] && tail -n 1 "$LOG_FILE" 2>/dev/null | grep -q '^OK'; then
  exit 0
fi

last_line="$(tail -n 1 "$LOG_FILE" 2>/dev/null || echo '<tom>')"
enqueue_msg "foundry: auto-update.log mangler fersk OK-linje siste ${WINDOW_MIN} min (siste linje: ${last_line})" >/dev/null
drain_queue
