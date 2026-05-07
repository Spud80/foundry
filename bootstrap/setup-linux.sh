#!/usr/bin/env bash
# setup-linux.sh - idempotent CT-bringup for foundry.
#
# Forutsetter:
#   * claude-bruker med passwordless sudo (etablert i Phase 100)
#   * Tailscale tilkoblet og SSH fungerer
#   * Repo allerede klonet til ~/foundry/ (typisk via runbook trinn 2-3)
#   * Kjores som claude-bruker, ikke root
#
# Idempotent: alle steg er trygge a re-kjore. Andre kjoring uten endringer
# i repoet skal endre ingenting (bekreftes i acceptance-test #2 ved a kjore
# scriptet to ganger og verifisere at filer pa CT-en er identiske).
#
# Steg:
#   1. Mkdir runtime- og config-trer
#   2. Installer Node.js LTS via NodeSource
#   3. Installer Claude Code CLI (npm install -g)
#   4. Deploy notify-core.sh + watchdog-notify.sh til ~/.config/foundry/
#   5. Deploy foundry-watchdog.cron til /etc/cron.d/ (root-eid)
#   6. Deploy logrotate.foundry til /etc/logrotate.d/ (root-eid)
#
# IKKE inkludert (Phase 300+):
#   * SCP av credentials.json + secrets.env (kjor manuelt per runbook)
#   * deploy.sh for cron.d/-pipeline (Phase 500)
#   * Syncthing-installasjon (Phase 400)

set -euo pipefail

# Sanity: ikke kjor som root
if [ "$(id -un)" = "root" ]; then
  echo "setup-linux.sh: ma kjores som claude-bruker, ikke root" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${HOME:-/home/claude}"
CONFIG_DIR="${HOME_DIR}/.config/foundry"
RUNTIME_DIR="${HOME_DIR}/foundry"

log() { printf '[setup-linux] %s\n' "$*"; }

log "Repo root: ${REPO_ROOT}"
log "Home: ${HOME_DIR}"

# Verifiser at vi er i forventet repo (sanity, ikke en hard kontrakt)
if [ ! -f "${REPO_ROOT}/bootstrap/setup-linux.sh" ]; then
  echo "setup-linux.sh: forventet a finne bootstrap/setup-linux.sh under ${REPO_ROOT}" >&2
  exit 1
fi

# === Steg 1: runtime- og config-trer ===
log "Step 1/6: ensuring runtime and config dirs..."
mkdir -p "${RUNTIME_DIR}/logs" "${RUNTIME_DIR}/queue"
mkdir -p "${CONFIG_DIR}"
chmod 700 "${CONFIG_DIR}"

# === Steg 2: Node.js LTS via NodeSource ===
if command -v node >/dev/null 2>&1; then
  log "Step 2/6: Node.js already installed: $(node --version)"
else
  log "Step 2/6: installing Node.js LTS via NodeSource..."
  curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
  sudo apt-get install -y nodejs
  log "Step 2/6: installed: $(node --version)"
fi

# === Steg 3: Claude Code CLI ===
if command -v claude >/dev/null 2>&1; then
  log "Step 3/6: claude CLI already installed: $(claude --version 2>&1 | head -1)"
else
  log "Step 3/6: installing claude CLI globally via npm..."
  sudo npm install -g @anthropic-ai/claude-code
  log "Step 3/6: installed: $(claude --version 2>&1 | head -1)"
fi

# === Steg 4: notify-core.sh + watchdog-notify.sh til config-dir ===
log "Step 4/6: deploying notify-core.sh and watchdog-notify.sh to ${CONFIG_DIR}..."
install -m 0755 "${REPO_ROOT}/_shared/notify-core.sh" "${CONFIG_DIR}/notify-core.sh"
install -m 0755 "${REPO_ROOT}/bootstrap/watchdog-notify.sh" "${CONFIG_DIR}/watchdog-notify.sh"

# === Steg 5: foundry-watchdog.cron til /etc/cron.d/ ===
log "Step 5/6: installing foundry-watchdog cron to /etc/cron.d/ (root-owned)..."
sudo install -m 0644 -o root -g root \
  "${REPO_ROOT}/bootstrap/foundry-watchdog.cron" \
  /etc/cron.d/foundry-watchdog

# === Steg 6: logrotate.foundry til /etc/logrotate.d/ ===
log "Step 6/6: installing logrotate config to /etc/logrotate.d/ (root-owned)..."
sudo install -m 0644 -o root -g root \
  "${REPO_ROOT}/bootstrap/logrotate.foundry" \
  /etc/logrotate.d/foundry

log "setup-linux.sh complete."
log ""
log "Verify:"
log "  ls -la ${CONFIG_DIR}/"
log "  ls -la /etc/cron.d/foundry-watchdog /etc/logrotate.d/foundry"
log "  ${CONFIG_DIR}/watchdog-notify.sh 'foundry: setup-linux test message'"
log "    (krever at secrets.env er pa plass - Phase 300)"
log ""
log "Neste fase:"
log "  Phase 300: deploy ~/.claude/.credentials.json og ${CONFIG_DIR}/secrets.env"
log "  Phase 400: Syncthing-installasjon og folder-config"
log "  Phase 500: deploy.sh-pipeline (cron.d/, auto-update.sh, queue-drain)"
