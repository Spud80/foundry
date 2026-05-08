#!/usr/bin/env bash
# notify.sh - tynt wrapper rundt notify-core.sh.
#
# Brukes av jobber i jobs/<jobname>/run.sh nar de vil sende et varsel:
#   ~/foundry/_shared/notify.sh "memory-extract: payload not deployed"
#
# Sekvens:
#   1. enqueue_msg "$@"   -> melding havner i ~/foundry/queue/<ts>-<pid>.msg
#   2. drain_queue        -> umiddelbart send-attempt; queue-fil slettes ved HTTP 200
#                            ellers blir den liggende for /5 cron-drain (Phase 500).
#
# Avvik fra notify.sh i andre prosjekter: vi enqueue ALLTID fer drain.
# Dette gir at-least-once-semantikk. Hvis Telegram er nede ved enqueue-tidspunktet,
# leveres meldingen av drain-queue.sh-cron senere uten at jobben mister varselet.
#
# Krav: ~/.config/foundry/secrets.env med TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
#       (notify-core.sh source'er dette automatisk).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify-core.sh"

if [ $# -eq 0 ]; then
  echo "notify.sh: usage: notify.sh <message>" >&2
  exit 1
fi

# Sla sammen alle args til en melding (tillater ucitet flerords-input)
msg="$*"

enqueue_msg "$msg" >/dev/null

# Ikke fail jobben hvis Telegram er midlertidig nede - drain-queue-cron tar over.
# drain_queue returnerer 1 hvis TELEGRAM_BOT_TOKEN/CHAT_ID mangler; det vil vi vite om.
if ! drain_queue; then
  echo "notify.sh: drain failed (likely missing Telegram secrets); message queued for retry" >&2
fi

exit 0
