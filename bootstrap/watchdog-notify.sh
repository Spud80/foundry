#!/usr/bin/env bash
# watchdog-notify.sh - foundry watchdog-entrypoint, stand-alone.
#
# Installeres til ~/.config/foundry/watchdog-notify.sh av setup-linux.sh,
# sammen med notify-core.sh i samme dir. Ingen repo-avhengighet etter
# setup-linux.sh er kjort - fungerer hvis ~/foundry/ slettes/flyttes.
#
# To modi:
#   1. Default (kalt av cron uten args): kjor to uavhengige sjekker:
#      a) ~/foundry/logs/auto-update.log: OK-linje innenfor siste
#         WINDOW_MIN min (default 45)
#      b) ~/.audit-state.json: last_run innenfor siste AUDIT_WINDOW_MIN min
#         (default 1500 = 25h; gir 1h grace forbi forventet daglig 04:00-cron)
#      Hver sjekk emitter Telegram-varsel uavhengig hvis stale.
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

AUDIT_STATE="${AUDIT_STATE_FILE:-${HOME}/.audit-state.json}"
AUDIT_WINDOW_MIN="${FOUNDRY_AUDIT_WINDOW_MIN:-1500}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify-core.sh"

# Direct-send mode
if [ $# -gt 0 ]; then
  enqueue_msg "$1" >/dev/null
  drain_queue
  exit $?
fi

# === Check 1: auto-update.log freshness ===
# Hvis loggfila ikke eksisterer (Phase 500 ikke deployet enno) eller er
# tom: skip silent. Watchdog er forst aktiv naar noe begynner a skrive
# til loggen.
if [ -s "$LOG_FILE" ]; then
  recent="$(find "$LOG_FILE" -mmin "-${WINDOW_MIN}" -print 2>/dev/null || true)"
  if [ -z "$recent" ] || ! tail -n 1 "$LOG_FILE" 2>/dev/null | grep -q '^OK'; then
    last_line="$(tail -n 1 "$LOG_FILE" 2>/dev/null || echo '<tom>')"
    enqueue_msg "foundry: auto-update.log mangler fersk OK-linje siste ${WINDOW_MIN} min (siste linje: ${last_line})" >/dev/null
  fi
fi

# === Check 2: audit-state.json freshness ===
# Hvis state-fila ikke eksisterer (Phase 1000 ikke fyrt enno) eller er
# tom: skip silent. Forst aktiv etter forste 04:00-audit-fyring har
# skrevet state-fila. Window 25h gir 1h grace forbi forventet daglig
# fyring.
if [ -s "$AUDIT_STATE" ]; then
  audit_recent="$(find "$AUDIT_STATE" -mmin "-${AUDIT_WINDOW_MIN}" -print 2>/dev/null || true)"
  if [ -z "$audit_recent" ]; then
    audit_last="$(grep -oE '"last_run":[[:space:]]*"[^"]+"' "$AUDIT_STATE" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/' || echo '<unknown>')"
    enqueue_msg "foundry: audit-state stale (last_run > ${AUDIT_WINDOW_MIN} min siden: ${audit_last:-<unknown>}; daglig 04:00-cron muligens skippet eller stuck)" >/dev/null
  fi
fi

drain_queue
