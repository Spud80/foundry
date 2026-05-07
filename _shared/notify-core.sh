#!/usr/bin/env bash
# notify-core.sh - felles enqueue/drain-helpers for foundry notify-pipeline.
#
# Source'es av:
#   _shared/notify.sh             (Phase 500 - enqueue + immediate-send-attempt)
#   _shared/drain-queue.sh        (Phase 500 - periodisk cron-drain */5 min)
#   bootstrap/watchdog-notify.sh  (Phase 200 - kopiert til ~/.config/foundry/)
#
# Kontrakt:
#   - enqueue_msg <msg>: skriver melding til ~/foundry/queue/<ts>-<pid>.msg.
#       Lager queue-dir hvis den ikke finnes. Echo'er full path til ny fil.
#   - drain_queue: itererer over alle .msg-filer, POSTer til Telegram med
#       --max-time 5, sletter ved HTTP 200. Tom queue eller manglende dir = no-op.
#       Ingen feil hvis Telegram er nede (filer blir liggende for neste pass).
#
# Krav: TELEGRAM_BOT_TOKEN og TELEGRAM_CHAT_ID i miljoet (lastes fra
#       ~/.config/foundry/secrets.env hvis den finnes).

set -uo pipefail

FOUNDRY_QUEUE_DIR="${FOUNDRY_QUEUE_DIR:-${HOME}/foundry/queue}"
FOUNDRY_SECRETS_FILE="${FOUNDRY_SECRETS_FILE:-${HOME}/.config/foundry/secrets.env}"

if [ -f "$FOUNDRY_SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  . "$FOUNDRY_SECRETS_FILE"
fi

enqueue_msg() {
  local msg="${1:-}"
  if [ -z "$msg" ]; then
    echo "enqueue_msg: empty message" >&2
    return 1
  fi
  mkdir -p "$FOUNDRY_QUEUE_DIR"
  local ts
  ts="$(date -u +%Y%m%dT%H%M%S.%N)"
  local file="${FOUNDRY_QUEUE_DIR}/${ts}-$$.msg"
  printf '%s\n' "$msg" > "$file"
  printf '%s\n' "$file"
}

drain_queue() {
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "drain_queue: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID" >&2
    return 1
  fi
  if [ ! -d "$FOUNDRY_QUEUE_DIR" ]; then
    return 0
  fi

  local file http_code body
  for file in "$FOUNDRY_QUEUE_DIR"/*.msg; do
    [ -f "$file" ] || continue
    body="$(cat "$file")"
    http_code="$(curl --silent --show-error --max-time 5 \
      --output /dev/null --write-out '%{http_code}' \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=${body}" \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      2>/dev/null || echo "000")"
    if [ "$http_code" = "200" ]; then
      rm -f "$file"
    fi
  done
}
