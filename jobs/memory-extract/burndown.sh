#!/usr/bin/env bash
# burndown.sh - nattlig batch-burndown av memory-extract pending-ko.
#
# Kjores av cron 0 23 * * * og looper extract.py i 30m-segmenter til:
#   - sessions_pending_count == 0  (ferdig - notify success)
#   - klokka >= 08:00 norsk lokal-tid (utenfor off-peak 2x-vindu)
#
# Re-acquirer flock per segment slik at auto-update */15 far plass mellom
# iterasjoner. Sleep 15m hvis et segment ikke gjor progress (rate-limit
# antagelig truffet; sub-window throttle resetter ~10-15 min).
#
# State (.compile-state.json) oppdateres atomisk per session inne i extract.py,
# sa restart mellom segmenter er gratis - ingen sessions tapes eller dupliseres.
#
# Logg: ~/foundry/logs/memory-extract-burndown.log
# Notify: kun ved fullforing, fatal error, eller daglig stop med pending > 0.

set -uo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_NAME="memory-extract-burndown"
REPO_ROOT="$(cd "${JOB_DIR}/../.." && pwd)"

LOCK_FILE="${REPO_ROOT}/.deploy.lock"
SECRETS_FILE="${HOME}/.config/foundry/secrets.env"
LOG_FILE="${HOME}/foundry/logs/memory-extract-burndown.log"
STATE_FILE="${HOME}/vault/My Vault/8.Cortex/Memory/.compile-state.json"
EXTRACT_PY="${JOB_DIR}/extract.py"
VENV_PYTHON="${JOB_DIR}/.venv/bin/python"

SEGMENT_TIMEOUT="${SEGMENT_TIMEOUT:-30m}"
SLEEP_AFTER_NO_PROGRESS="${SLEEP_AFTER_NO_PROGRESS:-900}"  # 15 min
END_HOUR="${END_HOUR:-8}"   # exit nar klokka >= 08
START_HOUR_GUARD="${START_HOUR_GUARD:-23}"  # exit-vinduet er [END_HOUR, START_HOUR_GUARD)

mkdir -p "$(dirname "$LOG_FILE")"

ts() { date -Iseconds; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

notify() {
  "${REPO_ROOT}/_shared/notify.sh" "${JOB_NAME}: $*" || true
}

get_pending() {
  if [ ! -f "$STATE_FILE" ]; then echo "0"; return; fi
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('sessions_pending_count', 0))" "$STATE_FILE" 2>/dev/null || echo "0"
}

log "=== burndown start (pid $$) ==="

# === Pre-flight: secrets + payload ===
if [ ! -f "$SECRETS_FILE" ]; then
  log "FATAL: ${SECRETS_FILE} mangler"
  notify "FATAL: secrets.env mangler"
  exit 2
fi

set -a
# shellcheck source=/dev/null
. "$SECRETS_FILE"
set +a

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  log "FATAL: CLAUDE_CODE_OAUTH_TOKEN ikke satt"
  notify "FATAL: CLAUDE_CODE_OAUTH_TOKEN mangler i secrets.env"
  exit 2
fi

if [ ! -x "$VENV_PYTHON" ] || [ ! -f "$EXTRACT_PY" ]; then
  log "FATAL: venv (${VENV_PYTHON}) eller extract.py (${EXTRACT_PY}) mangler"
  notify "FATAL: venv eller extract.py mangler - er Phase 600 import komplett?"
  exit 2
fi

initial_pending="$(get_pending)"
log "initial pending: ${initial_pending}"
if [ "$initial_pending" -eq 0 ]; then
  log "pending=0, exit 0 (no-op)"
  exit 0
fi

export OBSIDIAN_VAULT_ROOT="${OBSIDIAN_VAULT_ROOT:-${HOME}/vault/My Vault}"

iter=0
total_processed=0

while :; do
  iter=$((iter + 1))
  current_hour="$(date +%-H)"

  # Slutt-vindu: 08:00-22:59 (off-peak gunst tapt)
  if [ "$current_hour" -ge "$END_HOUR" ] && [ "$current_hour" -lt "$START_HOUR_GUARD" ]; then
    log "klokka ${current_hour}:xx >= END_HOUR ${END_HOUR}, stopper natta (iter=${iter}, processed=${total_processed})"
    final_pending="$(get_pending)"
    notify "natta slutt: processed ${total_processed} i ${iter} iter, pending=${final_pending} igjen"
    break
  fi

  pending_before="$(get_pending)"
  if [ "$pending_before" -eq 0 ]; then
    log "pending=0, ferdig (iter=${iter}, processed=${total_processed})"
    notify "BACKLOG TOM: processed ${total_processed} i ${iter} iter"
    break
  fi

  log "iter ${iter}: pending=${pending_before}, kjorer segment (timeout ${SEGMENT_TIMEOUT})"

  # Take flock med kort retry-loop sa auto-update kan fa plass mellom segmenter
  exec 9>"$LOCK_FILE"
  flock_attempts=0
  while ! flock --nonblock 9; do
    flock_attempts=$((flock_attempts + 1))
    if [ "$flock_attempts" -ge 5 ]; then
      log "iter ${iter}: lock busy etter 5 forsok, sleep 60s og fortsett"
      sleep 60
      flock_attempts=0
    else
      sleep 10
    fi
  done

  timeout "$SEGMENT_TIMEOUT" "$VENV_PYTHON" "$EXTRACT_PY" --verbose >> "$LOG_FILE" 2>&1
  rc=$?

  # Slipp lock for at auto-update */15 kan kjore
  exec 9>&-

  pending_after="$(get_pending)"
  delta=$((pending_before - pending_after))
  if [ "$delta" -lt 0 ]; then delta=0; fi
  total_processed=$((total_processed + delta))
  log "iter ${iter}: exit=${rc}, pending ${pending_before}->${pending_after} (delta=${delta})"

  if [ "$delta" -eq 0 ]; then
    log "ingen progress - antagelig rate-limit, sleep ${SLEEP_AFTER_NO_PROGRESS}s"
    sleep "$SLEEP_AFTER_NO_PROGRESS"
  fi
done

final_pending="$(get_pending)"
log "=== burndown done (iter=${iter}, processed=${total_processed}, pending=${final_pending}) ==="
exit 0
