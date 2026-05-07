#!/usr/bin/env bash
# token-expiry-check.sh - dato-basert utlops-varsling for CLAUDE_CODE_OAUTH_TOKEN.
#
# OAuth-tokens fra `claude setup-token` har ~1 ars levetid men ingen offentlig
# expiresAt-introspeksjon. Vi varsler basert pa dato lagret i secrets.env
# (CLAUDE_TOKEN_CREATED=YYYY-MM-DD), oppdatert manuelt ved hver rotation.
#
# Triggers:
#   - dager_igjen < 30          -> Telegram-varsel (rotation-paaminnelse)
#   - dager_igjen < 0           -> Telegram-varsel (allerede utlopt - kritisk)
#   - CLAUDE_TOKEN_CREATED ikke satt eller malformert -> Telegram-varsel
#
# Aktiveres av Phase 500 deploy.sh via cron.d/token-expiry-check.cron (daglig).
# Kjor manuelt for test: env CLAUDE_TOKEN_CREATED=2025-06-15 ./token-expiry-check.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# notify-core.sh source'er secrets.env og gir oss enqueue_msg + drain_queue.
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify-core.sh"

# Antallet dager OAuth-token er gyldig (claude setup-token-tokens utloper ~365 dager etter generering).
TOKEN_LIFETIME_DAYS="${TOKEN_LIFETIME_DAYS:-365}"
WARNING_THRESHOLD_DAYS="${WARNING_THRESHOLD_DAYS:-30}"

if [ -z "${CLAUDE_TOKEN_CREATED:-}" ]; then
  enqueue_msg "foundry token-expiry-check: CLAUDE_TOKEN_CREATED mangler i secrets.env. Kjor 'claude setup-token' pa spartan og legg dagens dato (YYYY-MM-DD) i secrets.env."
  drain_queue
  exit 1
fi

created_epoch=$(date -d "$CLAUDE_TOKEN_CREATED" +%s 2>/dev/null || echo "")
if [ -z "$created_epoch" ]; then
  enqueue_msg "foundry token-expiry-check: CLAUDE_TOKEN_CREATED='${CLAUDE_TOKEN_CREATED}' er ikke en gyldig dato (forvent YYYY-MM-DD)."
  drain_queue
  exit 1
fi

expiry_epoch=$((created_epoch + TOKEN_LIFETIME_DAYS * 86400))
now_epoch=$(date +%s)
days_remaining=$(( (expiry_epoch - now_epoch) / 86400 ))

if [ "$days_remaining" -lt 0 ]; then
  overdue=${days_remaining#-}
  enqueue_msg "foundry CLAUDE OAuth-token UTLOPT for ${overdue} dager siden (CLAUDE_TOKEN_CREATED=${CLAUDE_TOKEN_CREATED}). Foundry-jobber feiler nu pa auth. Roter umiddelbart: 'claude setup-token' pa spartan og oppdater secrets.env."
  drain_queue
  exit 1
fi

if [ "$days_remaining" -le "$WARNING_THRESHOLD_DAYS" ]; then
  enqueue_msg "foundry CLAUDE OAuth-token utloper om ${days_remaining} dager (CLAUDE_TOKEN_CREATED=${CLAUDE_TOKEN_CREATED}). Forbered rotation: 'claude setup-token' pa spartan, oppdater 1Password og secrets.env."
  drain_queue
  exit 0
fi

# Silent hvis > WARNING_THRESHOLD_DAYS
exit 0
