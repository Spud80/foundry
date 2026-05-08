#!/usr/bin/env bash
# drain-queue.sh - periodisk Telegram-queue-drain.
#
# Kalles av cron.d/drain-queue.cron (*/5 min) for a levere meldinger som ble
# enqueued mens Telegram var nede eller mens en notify.sh-kjoring fikk transient
# nettverksfeil. Idempotent: tom queue eller manglende katalog er no-op.
#
# Krav: ~/.config/foundry/secrets.env med TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
#       (notify-core.sh source'er dette automatisk).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify-core.sh"

drain_queue
exit $?
