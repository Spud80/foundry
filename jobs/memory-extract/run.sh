#!/usr/bin/env bash
# run.sh - memory-extract daglig orkestrering.
#
# To invokasjons-moduser:
#   - cron: 30 18 * * * (regenerert til crontab av deploy.sh fra
#     cron.d/memory-extract.cron), ingen argumenter.
#   - CP-dispatch: fleet/control-plane sin delegated-headless-lane invokerer
#     run.sh direkte (sudo runuser -u claude --) med --cp-* argv. Argv, ikke env:
#     env overlever ikke sudo/runuser-grensen uten env_keep-skjorhet.
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

# HOME-uavhengighet: under CP-dispatch (sudo runuser uten -l) kan HOME peke pa
# invokerens hjem, ikke claude sin. Resolv hjemmekatalogen fra passwd i stedet.
USER_HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"

LOCK_FILE="${REPO_ROOT}/.deploy.lock"
SECRETS_FILE="${USER_HOME}/.config/foundry/secrets.env"
LOG_FILE="${REPO_ROOT}/logs/memory-extract.log"
STATE_FILE="${JOB_DIR}/.state.json"
OBSIDIAN_VAULT_ROOT="${OBSIDIAN_VAULT_ROOT:-${USER_HOME}/vault/My Vault}"
# Raw session corpus. It sits BESIDE the vault, not inside it (2026-08-09
# relocation): ~8 000 machine-generated transcripts were dominating Obsidian's
# metadata-cache cost at startup. It cannot be derived from the vault root -
# that is the point of the move - so this job has to be told where it is.
CORTEX_RAW_ROOT="${CORTEX_RAW_ROOT:-${USER_HOME}/vault/cortex/raw}"
FILEHUB_CLEAN_SCOPE="${FILEHUB_CLEAN_SCOPE:-/data/sync/obsidian /data/sync/claude-memory}"
EXTRACT_PY="${JOB_DIR}/extract.py"
VENV_PYTHON="${JOB_DIR}/.venv/bin/python"
EXTRACT_TIMEOUT="${EXTRACT_TIMEOUT:-60m}"

# CP-dispatch-kontrakt (delegated-headless-lanen). Uten --cp-correlation kjorer vi i
# cron-modus og minter egen correlation, slik at ogsa fallback-dager metres via spoolen.
CP_CORRELATION=""
CP_USAGE_DIR="${CP_USAGE_SPOOL_DIR:-/var/lib/control-plane/usage-spool/memory-extract}"
CP_LIMIT=""
CP_MAX_BUDGET_USD=""
# Backlog-depth-alarm: default 50 (extract.py fyrer NOTIFY hvis >50 okter star
# igjen etter en kjoring - inflow slar gjennomstromning). CP-dispatch kan overstyre
# via --cp-backlog-threshold (samme mekanisme som --cp-limit); env CP_BACKLOG_THRESHOLD
# eller tom verdi skrur alarmen av.
CP_BACKLOG_THRESHOLD="${CP_BACKLOG_THRESHOLD:-50}"

while [ $# -gt 0 ]; do
  case "$1" in
    --cp-correlation)    CP_CORRELATION="${2:?--cp-correlation needs a value}"; shift 2 ;;
    --cp-usage-dir)      CP_USAGE_DIR="${2:?--cp-usage-dir needs a value}"; shift 2 ;;
    --cp-limit)          CP_LIMIT="${2:?--cp-limit needs a value}"; shift 2 ;;
    --cp-max-budget-usd) CP_MAX_BUDGET_USD="${2:?--cp-max-budget-usd needs a value}"; shift 2 ;;
    --cp-backlog-threshold) CP_BACKLOG_THRESHOLD="${2:?--cp-backlog-threshold needs a value}"; shift 2 ;;
    *)
      printf 'FATAL: unknown argument %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

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

# Eksporter notify-sti slik at extract.py.notify() kan eskalere graceful-degrade
# tilstander (status=missing fra load_aliases) til Telegram via _shared/notify.sh.
# Uten denne: kun AliasesError (exit 2) eskalerer via run.sh catch-all; missing
# aliases.yaml er stille i Telegram (Runde 5 plan-review finding).
export FOUNDRY_NOTIFY_SH="${FOUNDRY_NOTIFY_SH:-${REPO_ROOT}/_shared/notify.sh}"

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

# === Steg 4b: usage-spool (CP-metering, fleet/control-plane) ===
# extract.py spooler en usage-record per claude-kall nar EXTRACT_USAGE_DIR +
# EXTRACT_RUN_CORRELATION er satt. CP sweeper spoolen idempotent (ogsa leftovers
# fra cron-dager); rotasjonen eies her - CP sletter aldri claude-eide filer.
if [ -z "$CP_CORRELATION" ]; then
  CP_CORRELATION="cron-$(date +%F)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
fi
EXTRACT_ARGS=()
if [ -d "$CP_USAGE_DIR" ] && [ -w "$CP_USAGE_DIR" ]; then
  export EXTRACT_USAGE_DIR="$CP_USAGE_DIR"
  export EXTRACT_RUN_CORRELATION="$CP_CORRELATION"
  log "usage-spool: ${CP_USAGE_DIR} correlation=${CP_CORRELATION}"
  find "$CP_USAGE_DIR" -name '*.jsonl' -mtime +14 -delete 2>/dev/null || true
else
  log "usage-spool: ${CP_USAGE_DIR} mangler/ikke skrivbar - metering deaktivert denne runen"
fi
if [ -n "$CP_LIMIT" ]; then
  EXTRACT_ARGS+=(--limit "$CP_LIMIT")
fi
if [ -n "$CP_MAX_BUDGET_USD" ]; then
  EXTRACT_ARGS+=(--max-budget-usd "$CP_MAX_BUDGET_USD")
fi
if [ -n "$CP_BACKLOG_THRESHOLD" ]; then
  EXTRACT_ARGS+=(--backlog-alert-threshold "$CP_BACKLOG_THRESHOLD")
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

export OBSIDIAN_VAULT_ROOT CORTEX_RAW_ROOT
# CLAUDE_CODE_OAUTH_TOKEN + SYNCTHING_API_KEY er allerede i env via 'set -a' source

log "running: timeout ${EXTRACT_TIMEOUT} ${VENV_PYTHON} extract.py ${EXTRACT_ARGS[*]:-}"
timeout "$EXTRACT_TIMEOUT" "$VENV_PYTHON" "$EXTRACT_PY" ${EXTRACT_ARGS[@]+"${EXTRACT_ARGS[@]}"} >> "$LOG_FILE" 2>&1
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
  3)
    # Preflight-degraded: manifest sha256 mismatch (typically post-restore).
    # Non-transient: retrying tomorrow will fail identically until reconcile.
    # Operator must run reconcile-manifest.py --apply to fix.
    notify "preflight-degraded (exit 3) - manifest sha256 mismatch. Run reconcile-manifest.py --apply on vault to fix. See logs."
    log "preflight-degraded: extract.py exit 3 (manifest sha256 mismatch)"
    ;;
  *)
    notify "(FATAL) unexpected exit ${extract_status}. See logs."
    log "FATAL: extract.py exit ${extract_status}"
    ;;
esac

exit "$extract_status"
