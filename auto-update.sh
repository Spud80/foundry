#!/usr/bin/env bash
# auto-update.sh - defensiv git-flow for foundry-CT.
#
# Kjores av cron.d/auto-update.cron hvert 15. min. HELE sekvensen
# (fetch + reset --hard + deploy) er flock-beskyttet for a hindre at
# auto-update muterer working-tree mens en jobb kjorer.
#
# Branch-strategi (vedtatt 2026-05-08):
#   * Inntil foundry er stable og prodsatt: pull origin/dev
#   * Etter milepael: bytt FOUNDRY_BRANCH=main her, og bruk /release
#     for hver fremtidig endring til main
#
# Loggsemantikk (~/foundry/logs/auto-update.log):
#   * OK <timestamp> <details>   - vellykket no-op (allerede current) eller fullfort run
#   * FAIL <timestamp>: <reason> - git fetch eller deploy.sh feilet
#   * (ingen append ved BUSY)    - lock holdt; mtime touch'es slik at watchdog
#                                   ser fila som fersk men siste OK-linje forblir
#                                   oversst. Lange jobber genererer dermed ikke
#                                   false-positive watchdog-alarm.
#
# Watchdog (cron 0 * * * * via /etc/cron.d/foundry-watchdog) sjekker etter
# fersk mtime + "OK"-prefiks pa siste linje innenfor 45 min. Vedvarende FAIL
# uten paafolgende OK trigger derfor alarm korrekt - en reell brokenness-tilstand
# som krever manuell oppmerksomhet.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${HOME}/foundry/logs"
LOG_FILE="${LOG_DIR}/auto-update.log"
LOCK_FILE="${REPO_ROOT}/.deploy.lock"
FOUNDRY_BRANCH="${FOUNDRY_BRANCH:-dev}"

mkdir -p "$LOG_DIR"

ts() { date -Iseconds; }

log_ok() {
  printf 'OK %s %s\n' "$(ts)" "$*" >> "$LOG_FILE"
}

log_fail() {
  printf 'FAIL %s: %s\n' "$(ts)" "$*" >> "$LOG_FILE"
}

touch_mtime_only() {
  # Brukes ved BUSY (lock holdt) - oppdaterer mtime sa watchdog ser fila som
  # fersk uten a override siste OK-linje. Hvis loggfila ikke finnes enno,
  # opprett en tom (forste run).
  : >> "$LOG_FILE"
  touch "$LOG_FILE"
}

run_with_lock() {
  cd "$REPO_ROOT" || { log_fail "cd ${REPO_ROOT} feilet"; return 1; }

  # Fetch
  if ! git fetch origin "$FOUNDRY_BRANCH" 2>/tmp/auto-update-fetch.err; then
    err="$(tr '\n' ' ' < /tmp/auto-update-fetch.err | head -c 200)"
    rm -f /tmp/auto-update-fetch.err
    log_fail "git fetch origin ${FOUNDRY_BRANCH}: ${err}"
    return 1
  fi
  rm -f /tmp/auto-update-fetch.err

  # Sjekk om vi er bak origin
  local local_sha remote_sha
  local_sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  remote_sha="$(git rev-parse "origin/${FOUNDRY_BRANCH}" 2>/dev/null || echo unknown)"

  if [ "$local_sha" = "$remote_sha" ]; then
    # Allerede current - logg OK uten a kjore deploy.sh
    log_ok "no-op (HEAD=${local_sha:0:8})"
    return 0
  fi

  # Reset hard til origin (mister enhver lokal endring; runtime-state er gitignored)
  if ! git reset --hard "origin/${FOUNDRY_BRANCH}" >/dev/null 2>/tmp/auto-update-reset.err; then
    err="$(tr '\n' ' ' < /tmp/auto-update-reset.err | head -c 200)"
    rm -f /tmp/auto-update-reset.err
    log_fail "git reset --hard origin/${FOUNDRY_BRANCH}: ${err}"
    return 1
  fi
  rm -f /tmp/auto-update-reset.err

  # Kjor deploy.sh (regenerer crontab + venvs)
  if ! ./deploy.sh >/tmp/auto-update-deploy.out 2>&1; then
    err="$(tr '\n' ' ' < /tmp/auto-update-deploy.out | head -c 300)"
    rm -f /tmp/auto-update-deploy.out
    log_fail "deploy.sh: ${err}"
    return 1
  fi
  rm -f /tmp/auto-update-deploy.out

  log_ok "updated ${local_sha:0:8} -> ${remote_sha:0:8}, deploy.sh complete"
  return 0
}

# flock --nonblock: hopp over hvis lock er holdt (typisk en jobb som kjorer).
# Eksitkode 1 = lock kunne ikke hentes; vi tolker det som BUSY og loggrer ikke
# en FAIL-linje (jobben slipper lock'en snart).
exec 9>"$LOCK_FILE"
if ! flock --nonblock 9; then
  touch_mtime_only
  exit 0
fi

# Lock acquired - kjor sekvensen og frigi naturlig ved exit
run_with_lock
status=$?

# flock fd 9 frigis automatisk ved exit; ingen eksplisitt unlock.
exit "$status"
