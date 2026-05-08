#!/usr/bin/env bash
# deploy.sh - regenererer claude sin user-crontab atomisk fra cron.d/-fragmenter.
#
# Kjores av:
#   * auto-update.sh innenfor flock-window etter en vellykket git-pull
#   * Manuelt under bringup (Phase 500 task: forste deploy-aktivering)
#   * Manuelt etter en ny jobb er lagt til
#
# Idempotent: gjenta-kjoring uten endringer i cron.d/ eller jobs/ skal endre ingenting
# pa systemet (samme crontab-innhold, samme venv-tilstand).
#
# Steg:
#   1. Sanity: claude-bruker, repo-rot, cron.d/ finnes
#   2. Valider cron.d/*.cron-syntaks (regex; hard fail ved invalid linje)
#   3. Idempotent venv-setup per jobs/<jobname>/requirements.txt
#   4. Bygg ny crontab i temp-fil (header med SHELL/PATH/TZ/MAILTO + alle cron.d-fragmenter)
#   5. Backup eksisterende user-crontab til /tmp/crontab.bak.<pid>
#   6. Atomisk crontab-swap; ved feil rulles backup tilbake
#   7. Skriv kort summary til stdout (auto-update.sh fanger og fanger feilkode)
#
# IKKE rort av deploy.sh:
#   * /etc/cron.d/foundry-watchdog  (root-eid, lever utenfor pipeline per design)
#   * /etc/logrotate.d/foundry      (deployet av setup-linux.sh)
#   * ~/.config/foundry/             (deployet av setup-linux.sh + manuelt for secrets)
#
# Exit-koder:
#   0  vellykket regenerering (eller no-op hvis allerede current)
#   1  validation-feil i cron.d/ eller venv-setup
#   2  crontab-swap feilet og backup ble rullet tilbake

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_D_DIR="${REPO_ROOT}/cron.d"
JOBS_DIR="${REPO_ROOT}/jobs"
DRY_RUN="${DRY_RUN:-0}"
TIMESTAMP="$(date +%Y%m%dT%H%M%S)"
BACKUP_FILE="/tmp/crontab.bak.${TIMESTAMP}.$$"
NEW_CRONTAB="/tmp/crontab.new.${TIMESTAMP}.$$"

log() { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] FAIL: %s\n' "$*" >&2; exit "${2:-1}"; }

cleanup() {
  rm -f "$NEW_CRONTAB" 2>/dev/null || true
}
trap cleanup EXIT

# === Steg 1: sanity ===
if [ "$(id -un)" = "root" ]; then
  fail "ma kjores som claude-bruker, ikke root"
fi
if [ ! -d "$CRON_D_DIR" ]; then
  fail "cron.d/ mangler under ${REPO_ROOT}"
fi

log "Repo root: ${REPO_ROOT}"
log "Dry-run: ${DRY_RUN}"

# === Steg 2: valider cron.d/*.cron ===
log "Step 1/4: validating cron.d/*.cron syntax..."
shopt -s nullglob
cron_files=("${CRON_D_DIR}"/*.cron)
shopt -u nullglob

if [ "${#cron_files[@]}" -eq 0 ]; then
  fail "ingen cron.d/*.cron-fragmenter funnet - er repoet komplett?"
fi

# Cron-syntaks: 5 felt (min hour dom mon dow) + kommando.
# Hvert felt er enten *, et tall, eller et range/list/step (f.eks. */15, 1-5, 1,3,5).
# Dette er en pragmatisk regex - validerer struktur, ikke alle lovlige range-uttrykk.
cron_line_re='^[[:space:]]*([0-9*,/-]+)[[:space:]]+([0-9*,/-]+)[[:space:]]+([0-9*,/-]+)[[:space:]]+([0-9*,/-]+)[[:space:]]+([0-9*,/-]+)[[:space:]]+(.+)$'

invalid_lines=0
for f in "${cron_files[@]}"; do
  basename="$(basename "$f")"
  while IFS= read -r line || [ -n "$line" ]; do
    # Hopp over tomme linjer og kommentarer
    [ -z "${line// }" ] && continue
    case "$line" in
      \#*) continue ;;
    esac
    if ! [[ "$line" =~ $cron_line_re ]]; then
      printf '[deploy] FAIL: invalid cron-line in %s: %s\n' "$basename" "$line" >&2
      invalid_lines=$((invalid_lines + 1))
    fi
  done < "$f"
done

if [ "$invalid_lines" -gt 0 ]; then
  fail "${invalid_lines} ugyldige cron-linjer - aborter for crontab-mutasjon"
fi

log "Step 1/4: ${#cron_files[@]} cron-fragmenter validert"

# === Steg 3: idempotent venv-setup per jobs/<jobname>/requirements.txt ===
log "Step 2/4: ensuring per-job venvs..."
venv_count=0
if [ -d "$JOBS_DIR" ]; then
  shopt -s nullglob
  for req in "${JOBS_DIR}"/*/requirements.txt; do
    job_dir="$(dirname "$req")"
    job_name="$(basename "$job_dir")"
    venv_dir="${job_dir}/.venv"

    # Hash av requirements.txt + python-versjon for cache-invalidation
    req_hash="$(sha256sum "$req" | cut -d' ' -f1)"
    py_bin="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
    if [ -z "$py_bin" ]; then
      fail "python3 ikke funnet - kreves for venv-setup"
    fi
    py_version="$($py_bin --version 2>&1)"
    fingerprint="${req_hash}|${py_version}"

    stamp_file="${venv_dir}/.deploy-fingerprint"
    if [ -f "$stamp_file" ] && [ "$(cat "$stamp_file" 2>/dev/null)" = "$fingerprint" ]; then
      log "  ${job_name}: venv up-to-date (fingerprint match)"
      continue
    fi

    log "  ${job_name}: (re)creating venv with ${py_version}..."
    if [ "$DRY_RUN" = "1" ]; then
      log "  [dry-run] skipping venv creation for ${job_name}"
      continue
    fi
    rm -rf "$venv_dir"
    "$py_bin" -m venv "$venv_dir" || fail "venv creation failed for ${job_name}"
    "${venv_dir}/bin/pip" install --quiet --upgrade pip || fail "pip upgrade failed for ${job_name}"
    "${venv_dir}/bin/pip" install --quiet -r "$req" || fail "requirements install failed for ${job_name}"
    printf '%s' "$fingerprint" > "$stamp_file"
    venv_count=$((venv_count + 1))
  done
  shopt -u nullglob
fi
log "Step 2/4: ${venv_count} venv(s) created/updated"

# === Steg 4: bygg ny crontab ===
log "Step 3/4: building new crontab from cron.d/-fragmenter..."
{
  printf '# foundry crontab - regenerated by deploy.sh at %s\n' "$(date -Iseconds)"
  printf '# Kilde: cron.d/*.cron i ~/foundry/-repoet. ALDRI rediger denne crontaben\n'
  printf '# direkte - endringer overskrives ved neste auto-update.\n'
  printf '\n'
  printf 'SHELL=/bin/bash\n'
  printf 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
  printf 'MAILTO=""\n'
  printf '\n'
  for f in "${cron_files[@]}"; do
    printf '# === %s ===\n' "$(basename "$f")"
    cat "$f"
    printf '\n'
  done
} > "$NEW_CRONTAB"

# Sammenlikn med eksisterende; skip swap hvis identisk (idempotent no-op)
existing_crontab="$(crontab -l 2>/dev/null || true)"
new_content="$(cat "$NEW_CRONTAB")"
if [ "$existing_crontab" = "$new_content" ]; then
  log "Step 3/4: crontab uendret - skip swap"
  log "deploy.sh complete (no-op)"
  exit 0
fi

# === Steg 5: backup + atomisk swap ===
log "Step 4/4: swapping crontab atomically..."
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] new crontab would be:"
  sed 's/^/  /' "$NEW_CRONTAB"
  log "deploy.sh complete (dry-run)"
  exit 0
fi

# Backup eksisterende crontab (kan vaere tom for forste deploy)
if [ -n "$existing_crontab" ]; then
  printf '%s\n' "$existing_crontab" > "$BACKUP_FILE"
  log "  backup written to ${BACKUP_FILE}"
fi

if ! crontab "$NEW_CRONTAB"; then
  log "  crontab swap failed - rolling back..."
  if [ -f "$BACKUP_FILE" ]; then
    crontab "$BACKUP_FILE" || log "  WARNING: rollback ALSO failed - crontab may be empty"
    fail "crontab swap feilet, backup rullet tilbake" 2
  else
    fail "crontab swap feilet, ingen backup a rulle tilbake til (eksisterende var tom)" 2
  fi
fi

log "Step 4/4: crontab swapped successfully"
log "deploy.sh complete"
exit 0
