#!/usr/bin/env bash
# run.sh - fallback-classifier nightly orchestration.
#
# Kjores av cron 0 3 * * * (regenerert til crontab av deploy.sh fra
# cron.d/fallback-classifier.cron).
#
# Sekvens (per CONTRACT.md):
#   1. Payload-guard - exit 0 stille hvis classify.py/system-prompt.md mangler
#   2. Source secrets.env for CLAUDE_CODE_OAUTH_TOKEN
#   3. flock --nonblock pa ~/foundry/.deploy.lock (serialiser mot auto-update,
#      memory-extract, audit)
#   4. .venv/bin/python classify.py (per-fil timeout handteres internt av
#      classify.py; ingen ekstra outer timeout)
#
# Logg: ~/foundry/logs/fallback-classifier.log (append).
# Notify: Telegram via _shared/notify.sh ved exit != 0 + per-fil-feil fra
# classify.py.

set -uo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_NAME="fallback-classifier"
REPO_ROOT="$(cd "${JOB_DIR}/../.." && pwd)"

LOCK_FILE="${REPO_ROOT}/.deploy.lock"
SECRETS_FILE="${HOME}/.config/foundry/secrets.env"
LOG_FILE="${HOME}/foundry/logs/fallback-classifier.log"
OBSIDIAN_VAULT_ROOT="${OBSIDIAN_VAULT_ROOT:-${HOME}/vault/My Vault}"
CLASSIFY_PY="${JOB_DIR}/classify.py"
SYSTEM_PROMPT="${JOB_DIR}/system-prompt.md"
VENV_PYTHON="${JOB_DIR}/.venv/bin/python"

mkdir -p "$(dirname "$LOG_FILE")"

ts() { date -Iseconds; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

notify() {
  "${REPO_ROOT}/_shared/notify.sh" "${JOB_NAME}: $*" || true
}

log "=== run start (pid $$) ==="

# === Steg 1: payload-guard ===
# Phase 900 deployes uten payload forst; classify.py+system-prompt.md kommer i
# samme PR men payload-guard beskytter mot delvis-deploy/rollback-scenarier.
if [ ! -f "$CLASSIFY_PY" ] || [ ! -f "$SYSTEM_PROMPT" ]; then
  notify "payload not deployed - classify.py or system-prompt.md missing in ${JOB_DIR}"
  log "payload-guard: classify.py eller system-prompt.md mangler - exit 0 (no-op)"
  exit 0
fi

# === Steg 2: source secrets.env ===
if [ ! -f "$SECRETS_FILE" ]; then
  notify "FATAL: secrets.env not found at ${SECRETS_FILE}"
  log "FATAL: ${SECRETS_FILE} mangler"
  exit 2
fi

set -a
# shellcheck source=/dev/null
. "$SECRETS_FILE"
set +a

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  notify "FATAL: CLAUDE_CODE_OAUTH_TOKEN missing from secrets.env"
  log "FATAL: CLAUDE_CODE_OAUTH_TOKEN ikke satt etter source"
  exit 2
fi

# Eksporter notify-sti slik at classify.py kan eskalere per-fil-feil til Telegram.
export FOUNDRY_NOTIFY_SH="${FOUNDRY_NOTIFY_SH:-${REPO_ROOT}/_shared/notify.sh}"

# === Steg 3: flock --nonblock pa deploy-lock ===
exec 9>"$LOCK_FILE"
if ! flock --nonblock 9; then
  log "BUSY: ${LOCK_FILE} holdt - skip dette vinduet (cron prover igjen i morgen)"
  notify "skipped run: deploy-lock busy (auto-update, memory-extract, or audit in progress)"
  exit 0
fi
log "lock acquired"

# === Steg 4: kjor classify.py ===
# Ingen ssh filehub-cleanup pre-flight per design (CONTRACT.md). Per-fil
# claude -p-timeout handteres internt av classify.py (FALLBACK_CLAUDE_TIMEOUT_SECONDS).
if [ ! -x "$VENV_PYTHON" ]; then
  notify "FATAL: venv missing at ${VENV_PYTHON} (deploy.sh skulle ha satt opp - sjekk requirements.txt)"
  log "FATAL: ${VENV_PYTHON} mangler eller ikke kjorbar"
  exit 2
fi

export OBSIDIAN_VAULT_ROOT
# CLAUDE_CODE_OAUTH_TOKEN er allerede i env via 'set -a' source

log "running: ${VENV_PYTHON} classify.py"
"$VENV_PYTHON" "$CLASSIFY_PY" >> "$LOG_FILE" 2>&1
classify_status=$?

case "$classify_status" in
  0)
    log "=== run complete (exit 0) ==="
    ;;
  1)
    notify "transient errors during classify - some files still pending. See logs."
    log "transient: classify.py exit 1"
    ;;
  2)
    notify "(FATAL) exit 2 - manual intervention required. See logs."
    log "FATAL: classify.py exit 2"
    ;;
  124)
    notify "TIMEOUT: classify.py killed"
    log "TIMEOUT: classify.py exit 124"
    ;;
  *)
    notify "(FATAL) unexpected exit ${classify_status}. See logs."
    log "FATAL: classify.py exit ${classify_status}"
    ;;
esac

exit "$classify_status"
