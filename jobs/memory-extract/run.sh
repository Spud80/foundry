#!/usr/bin/env bash
# run.sh - memory-extract daglig orkestrering.
#
# Kjores av cron 30 18 * * * (regenerert til crontab av deploy.sh fra
# cron.d/memory-extract.cron).
#
# Sekvens (per CONTRACT.md):
#   1. Payload-guard - exit 0 stille hvis extract.py mangler (Phase 600 ikke aktiv)
#   2. Source secrets.env for CLAUDE_CODE_OAUTH_TOKEN, SYNCTHING_API_KEY
#   3. flock --nonblock pa ~/foundry/.deploy.lock (serialiserer mot auto-update.sh)
#   4. ssh filehub-cleanup <paths...> (pre-flight; wrapper prepender --require-clean,
#      exit 1 = konflikter quarantined under noen av <paths>; full /data/sync-skan
#      kjorer alltid idempotent uavhengig av path-arg)
#   5. timeout 30m .venv/bin/python extract.py (exit-code propageres til notify;
#      manifest-handshake + Syncthing-preflight skjer inne i extract.py)
#
# Logg: ~/foundry/logs/memory-extract.log (append, en linje per run + extract.py-output).
# Notify: Telegram via _shared/notify.sh ved exit != 0.

set -uo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_NAME="memory-extract"
REPO_ROOT="$(cd "${JOB_DIR}/../.." && pwd)"

LOCK_FILE="${REPO_ROOT}/.deploy.lock"
SECRETS_FILE="${HOME}/.config/foundry/secrets.env"
LOG_FILE="${HOME}/foundry/logs/memory-extract.log"
STATE_FILE="${JOB_DIR}/.state.json"
OBSIDIAN_VAULT_ROOT="${OBSIDIAN_VAULT_ROOT:-${HOME}/vault/My Vault}"
FILEHUB_CLEAN_SCOPE="${FILEHUB_CLEAN_SCOPE:-/data/sync/obsidian /data/sync/claude-memory}"
EXTRACT_PY="${JOB_DIR}/extract.py"
VENV_PYTHON="${JOB_DIR}/.venv/bin/python"
EXTRACT_TIMEOUT="${EXTRACT_TIMEOUT:-60m}"

mkdir -p "$(dirname "$LOG_FILE")"

ts() { date -Iseconds; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

# Notify-sti: hardkode for forutsigbarhet (run.sh kjor uten cwd-garantier i cron)
notify() {
  "${REPO_ROOT}/_shared/notify.sh" "${JOB_NAME}: $*" || true
}

log "=== run start (pid $$) ==="

# === Steg 1: payload-guard ===
# Phase 600 ikke aktivert hvis extract.py mangler. Send EN gang pr. dag og exit 0.
# Cron-fila er aktiv fra Phase 500, men jobben er no-op fram til obsidian-memory
# leverer mot CONTRACT.md.
if [ ! -f "$EXTRACT_PY" ]; then
  notify "payload not deployed (Phase 600 not active) - extract.py missing in ${JOB_DIR}"
  log "payload-guard: extract.py mangler - exit 0 (no-op)"
  exit 0
fi

# === Steg 2: source secrets.env for OAuth-token ===
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

# === Steg 3: flock --nonblock pa deploy-lock ===
exec 9>"$LOCK_FILE"
if ! flock --nonblock 9; then
  log "BUSY: ${LOCK_FILE} holdt - skip dette vinduet (cron prover igjen i morgen)"
  notify "skipped run: deploy-lock busy (auto-update or another job in progress)"
  exit 0
fi
log "lock acquired"

# === Steg 4: pre-flight ssh filehub-cleanup ===
# Wrapper /usr/local/bin/foundry-cleanup-login-shell prepender --require-clean
# automatisk; vi sender bare scoped paths (space-separert, word-splittes av wrapper).
# Scope = bade /data/sync/obsidian (vault) og /data/sync/claude-memory (capture-input).
log "pre-flight: ssh filehub-cleanup ${FILEHUB_CLEAN_SCOPE}"
# shellcheck disable=SC2086
cleanup_out="$(ssh -o ConnectTimeout=10 -o ServerAliveInterval=5 \
  filehub-cleanup ${FILEHUB_CLEAN_SCOPE} 2>&1)"
cleanup_status=$?

log "filehub-cleanup output: ${cleanup_out}"
log "filehub-cleanup exit: ${cleanup_status}"

if [ "$cleanup_status" -ne 0 ]; then
  notify "pre-flight blocked: filehub-cleanup returned exit ${cleanup_status} (Syncthing conflicts unresolved). See logs."
  exit 1
fi

# === Steg 5: kjor extract.py med timeout ===
# Glob-assert + manifest-pre-flight + Syncthing-preflight handteres internt
# av extract.py (manifest er primaer gate; Syncthing er sekundaer; tom raw/
# returnerer "0 date-dirs verified" + exit 0 stille).
if [ ! -x "$VENV_PYTHON" ]; then
  notify "FATAL: venv missing at ${VENV_PYTHON} (deploy.sh skulle ha satt opp - sjekk requirements.txt)"
  log "FATAL: ${VENV_PYTHON} mangler eller ikke kjorbar"
  exit 2
fi

export OBSIDIAN_VAULT_ROOT
# CLAUDE_CODE_OAUTH_TOKEN + SYNCTHING_API_KEY er allerede i env via 'set -a' source

log "running: timeout ${EXTRACT_TIMEOUT} ${VENV_PYTHON} extract.py"
timeout "$EXTRACT_TIMEOUT" "$VENV_PYTHON" "$EXTRACT_PY" >> "$LOG_FILE" 2>&1
extract_status=$?

case "$extract_status" in
  0)
    log "=== run complete (exit 0) ==="
    ;;
  124)
    notify "TIMEOUT: extract.py exceeded ${EXTRACT_TIMEOUT} - killed"
    log "TIMEOUT: extract.py drept etter ${EXTRACT_TIMEOUT}"
    ;;
  1)
    notify "transient error (exit 1) - cron will retry next day. See logs."
    log "transient error: extract.py exit 1"
    ;;
  2)
    notify "(FATAL) exit 2 - manual intervention required. See logs."
    log "FATAL: extract.py exit 2"
    ;;
  *)
    notify "(FATAL) unexpected exit ${extract_status}. See logs."
    log "FATAL: extract.py exit ${extract_status}"
    ;;
esac

exit "$extract_status"
