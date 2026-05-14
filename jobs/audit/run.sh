#!/usr/bin/env bash
# run.sh - audit nightly orchestration.
#
# Kjores av cron 0 4 * * * (regenerert til crontab av deploy.sh fra
# cron.d/audit.cron).
#
# Sekvens (per CONTRACT.md):
#   1. Payload-guard - exit 0 stille hvis audit.py/system-prompt.md mangler
#   2. Source secrets.env for CLAUDE_CODE_OAUTH_TOKEN (kreves for sjekk 4)
#   3. flock --nonblock pa ~/foundry/.deploy.lock (serialiser mot auto-update,
#      memory-extract, fallback-classifier)
#   4. .venv/bin/python audit.py (rapport-fil + heartbeat + Telegram)
#
# Logg: ~/foundry/logs/audit.log (append).
# Notify: Telegram via _shared/notify.sh ved exit != 0 + tiered varsel
# fra audit.py for funn hoy/kritisk.

set -uo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_NAME="audit"
REPO_ROOT="$(cd "${JOB_DIR}/../.." && pwd)"

LOCK_FILE="${REPO_ROOT}/.deploy.lock"
SECRETS_FILE="${HOME}/.config/foundry/secrets.env"
LOG_FILE="${HOME}/foundry/logs/audit.log"
OBSIDIAN_VAULT_ROOT="${OBSIDIAN_VAULT_ROOT:-${HOME}/vault/My Vault}"
AUDIT_PY="${JOB_DIR}/audit.py"
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
# Phase 1000 deployes uten payload forst tilfelle delvis-deploy/rollback;
# payload-guard beskytter mot daglige feil ved manglende fil.
if [ ! -f "$AUDIT_PY" ] || [ ! -f "$SYSTEM_PROMPT" ]; then
  notify "payload not deployed - audit.py or system-prompt.md missing in ${JOB_DIR}"
  log "payload-guard: audit.py eller system-prompt.md mangler - exit 0 (no-op)"
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

# Eksporter notify-sti slik at audit.py kan eskalere tiered Telegram-varsel.
export FOUNDRY_NOTIFY_SH="${FOUNDRY_NOTIFY_SH:-${REPO_ROOT}/_shared/notify.sh}"

# === Steg 3: flock --nonblock pa deploy-lock ===
exec 9>"$LOCK_FILE"
if ! flock --nonblock 9; then
  log "BUSY: ${LOCK_FILE} holdt - skip dette vinduet (cron prover igjen i morgen)"
  notify "skipped run: deploy-lock busy (auto-update, memory-extract, or fallback-classifier in progress)"
  exit 0
fi
log "lock acquired"

# === Steg 4: kjor audit.py ===
if [ ! -x "$VENV_PYTHON" ]; then
  notify "FATAL: venv missing at ${VENV_PYTHON} (deploy.sh skulle ha satt opp - sjekk requirements.txt)"
  log "FATAL: ${VENV_PYTHON} mangler eller ikke kjorbar"
  exit 2
fi

export OBSIDIAN_VAULT_ROOT
# CLAUDE_CODE_OAUTH_TOKEN er allerede i env via 'set -a' source

log "running: ${VENV_PYTHON} audit.py"
"$VENV_PYTHON" "$AUDIT_PY" >> "$LOG_FILE" 2>&1
audit_status=$?

case "$audit_status" in
  0)
    log "=== run complete (exit 0) ==="
    ;;
  1)
    notify "(FATAL) exit 1 unexpected runtime error. See logs."
    log "FATAL: audit.py exit 1"
    ;;
  2)
    notify "(FATAL) exit 2 schema-mismatch or config error. See logs."
    log "FATAL: audit.py exit 2"
    ;;
  124)
    notify "vault unavailable: audit skipped this night"
    log "TRANSIENT: audit.py exit 124"
    ;;
  *)
    notify "(FATAL) unexpected exit ${audit_status}. See logs."
    log "FATAL: audit.py exit ${audit_status}"
    ;;
esac

exit "$audit_status"
