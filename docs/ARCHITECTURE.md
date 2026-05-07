# Architecture

## Overview

foundry er et Debian-12 unprivileged LXC (Proxmox VMID 102 på `thehub`) som fungerer som felles substrat for headless cron-baserte vault-automation-jobber. Designet er bevisst minimalistisk: cron + bash + Python (per-jobb venvs), ingen langvarige tjenester eller daemoner utenom auto-update og watchdog.

Primært arkitektur-pattern: **defensiv pull-based deploy**. CT-en er ikke kilde-sannhet for noe - alt avgjørende state lever enten i git-repoet (kode, cron-fragmenter) eller utenfor CT-treet (secrets, eventuelle persistente queues). Auto-update gjør `git reset --hard origin/main` hvert 15. min, så lokale modifikasjoner på CT er per design transient.

Nøkkel-constraints:

- Public repo: ingen secrets, ingen sensitiv config i repo-treet.
- Unprivileged LXC: CAP_SYS_TIME blokkert (`systemd-timesyncd` masked, host-klokka arves).
- Cron som eneste scheduler: ingen eventbus, ingen brokere, ingen langvarige prosesser utenom auto-update og watchdog.
- Auto-deploy fra main: ingen staging-CT, ingen blue-green; sikkerhetsnett er git-historikk + watchdog-varsling.

## Component Diagram

```
                    +-----------------------------+
                    |   GitHub: Spud80/foundry    |
                    |   main = deploy-branch      |
                    +-------------+---------------+
                                  |
                                  |  fetch + reset --hard
                                  |  (every 15 min)
                                  v
+----------------------------------------------------------------+
|  LXC 102 (foundry) on Proxmox host thehub - 10.0.0.52          |
|                                                                |
|  +----------------------+    +----------------------+          |
|  |  auto-update.sh      |--->|  deploy.sh           |          |
|  |  (cron */15)         |    |  regenerates         |          |
|  |  fetch + reset       |    |  /etc/cron.d/foundry |          |
|  +----------+-----------+    +----------+-----------+          |
|             |                           |                      |
|             v                           v                      |
|  +----------------------+    +----------------------+          |
|  |  ~/foundry/ (repo)   |    |  /etc/cron.d/        |          |
|  |  bootstrap/          |    |  foundry-<job>       |          |
|  |  cron.d/             |    |  foundry-watchdog    |          |
|  |  jobs/<job>/         |    +----------+-----------+          |
|  |  _shared/            |               |                      |
|  +----------------------+               | triggers             |
|                                         v                      |
|                              +----------------------+          |
|                              |  Per-jobb run.sh     |          |
|                              |  (jobs/<job>/.venv/) |          |
|                              +----------+-----------+          |
|                                         |                      |
|                                         v                      |
|                              +-----------------------+         |
|                              | Outbound side-effects:          |
|                              |  - Claude API (LLM)             |
|                              |  - Obsidian REST                |
|                              |  - Telegram (notify)            |
|                              +-----------------------+         |
+----------------------------------------------------------------+

Secrets (out-of-tree):  ~/.config/foundry/secrets.env
                        ~/.claude/.credentials.json
```

## Data Flow

### Deploy-flyt (kontinuerlig)

1. Utvikler committer på `dev` lokalt -> kjører `/release` -> merge til `main` + push til origin.
2. `auto-update.sh` kjører via cron `*/15 * * * *` på CT 102:
   - `git fetch origin main`
   - Hvis `HEAD != origin/main`: `git reset --hard origin/main`, deretter `bash deploy.sh`
3. `deploy.sh` regenererer `/etc/cron.d/foundry-*` atomisk fra `cron.d/`-fragmenter i repo (skriver til temp-fil, validerer, mv-replaces).
4. Cron leser nye fragmenter ved neste minutt-tikk; jobber starter på sin schedule.

### Job-eksekvering

1. Cron-fragment (f.eks. `cron.d/foundry-memory-extract`) trigger `jobs/memory-extract/run.sh` på sin schedule.
2. `run.sh` aktiverer `jobs/<job>/.venv/`, leser `~/.config/foundry/secrets.env`, kjører jobb-logikken.
3. Output skrives til `~/foundry/logs/<job>.log` (logrotate ukentlig × 12 uker per CT-spec).
4. Ved feil: `_shared/notify.sh` sender Telegram-varsel.

### Watchdog (defense-in-depth)

1. `foundry-watchdog`-cron (root-eid, separat fra auto-update) kjører hvert 30. min.
2. Sjekker `~/foundry/logs/auto-update.log` for OK-linje siste 45 min.
3. Hvis manglende: sender Telegram-varsel via uavhengig kanal - ikke via `_shared/notify.sh`, fordi en feil som broke auto-update kan også broke `_shared/`.

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Deploy-strategi | Pull-based via auto-update.sh + `git reset --hard` | Lokal CT-state er aldri autoritativ; konflikter umulig per design |
| Scheduler | System-cron, regenerert av deploy.sh | Kjent semantikk, audit-bart, ingen ekstra runtime |
| Job-isolasjon | Per-jobb venvs (`jobs/<x>/.venv/`) | Unngår dependency-konflikter, jobb-fjerning er trivielt |
| Watchdog-eier | Root-eid cron, separat fra auto-update | Skal varsle hvis auto-update selv feiler |
| Notify-channel | Telegram via `_shared/notify.sh` | Lavterskel push, ingen ekstra infra |
| Watchdog-channel | Uavhengig av `_shared/notify.sh` | Hvis `_shared/` er broken må watchdog fortsatt nå ut |
| LXC-type | Unprivileged | Default sikkerhets-posture; CAP_SYS_TIME-tap akseptert |
| Time-sync | `systemd-timesyncd` masked, host-klokka arves | CAP_SYS_TIME blokkert i unprivileged LXC uansett |
| Repo-synlighet | Public | Pipeline ikke-sensitiv; secrets lever out-of-tree |
| Single-flight | `.deploy.lock` flock-fil | Forhindrer overlappende deploy.sh-kjøringer |

## Module Structure

- `bootstrap/` - Idempotent CT-bringup (`setup-linux.sh`, watchdog-stub, logrotate-config, runbook). Kjøres én gang per CT-instans, må kunne re-kjøres uten å ødelegge eksisterende state.
- `bootstrap/proxmox-ct-config.md` - CT-spec (VMID, ressurser, network, `pct create`-kommando). Forankrings-dokument; oppdateres når CT-kapasitet endres på Proxmox.
- `cron.d/` - Crontab-fragmenter (én fil per jobb). Konsumeres av `deploy.sh` for atomisk regenerering av `/etc/cron.d/foundry-*`.
- `_shared/` - Felles helpers brukt av flere jobber: `notify.sh` (Telegram), `drain-queue.sh`, lockfile-helpers. Underscore-prefiks for at `jobs/`-iterasjon ikke skal traversere.
- `jobs/<jobname>/` - Per-jobb mappe: `run.sh`, `requirements.txt` (om Python), evt config. Ingen kryss-jobb-imports.
- `ci/` - GitHub Actions for lint (shellcheck, ruff) og smoke-tester (`deploy.sh --dry-run`).
- `deploy.sh` - Top-level: regenererer system-crontab atomisk fra `cron.d/`. Idempotent.
- `auto-update.sh` - Top-level: defensiv git-flow på CT-en (fetch + reset --hard + deploy.sh).

## External Dependencies

- **GitHub (Spud80/foundry)** - kilde-sannhet for kode. `auto-update.sh` puller hvert 15. min.
- **Claude API** (via `~/.claude/.credentials.json` på CT-en) - for LLM-baserte jobber. Headless via `claude -p`. Token kopieres fra utvikler-PC etter `claude setup-token`-flow.
- **Obsidian REST API** - for vault-mutasjoner som krever struktur-håndtering (wikilink-cascade-safe rename/move/delete). Brukes via `obsidian`-wrapper på utvikler-PC; for CT-jobber via `obsidian_routing` Python-modul.
- **Telegram Bot API** - for varsling (failure-notifications, watchdog-alerts). Bot-token i `~/.config/foundry/secrets.env`.
- **Tailscale tailnet (`tail8feda0.ts.net`)** - for inn/ut-tilgang. CT autentiseres med tailnet-identitet (ikke device-keys).
- **Proxmox host `thehub`** - substrate-host. CT-spec forankret i `bootstrap/proxmox-ct-config.md`.

## Test Infrastructure

Foundry har ingen runtime-database. Tester kjører i CI:

- **Shell-lint:** `shellcheck` på alle `*.sh`-filer i repo.
- **Python-lint:** `ruff check` per jobb (når jobs/-mapper finnes).
- **Smoke-test:** `deploy.sh --dry-run` for å verifisere at `cron.d/`-fragmenter parser korrekt og at output-crontab er valid.
- **Job-spesifikke tester:** kjøres i jobb-mappa via `jobs/<job>/test.sh` om filen finnes.

CI går via GitHub Actions (`ci/`-mappa, scaffold pågår - Phase 200).

## Disaster Recovery

CT-en er stateless utenom secrets og logs. Tap av CT-en innebærer:

- Kode: gjenopprettes fra GitHub (`git clone`).
- Secrets: må kopieres fra backup (`~/.config/foundry/secrets.env`, `~/.claude/.credentials.json`).
- Cron-state: regenereres av `deploy.sh` ved første post-bringup-kjøring.
- Logs: borte (akseptabelt - logger er ikke kilde-sannhet).

DR-prosedyre detaljeres i `bootstrap/README.md` (planlagt Phase 100).

Snapshot-policy: Proxmox-snapshot ukentlig × 4 (rolling) per `bootstrap/proxmox-ct-config.md`. Konfigureres på Proxmox-host, ikke i dette repoet.

## Related Documents

| Document | Content |
|----------|---------|
| [`../README.md`](../README.md) | Public-facing oversikt, repo-layout-intent |
| [`../CLAUDE.md`](../CLAUDE.md) | Claude Code project-config (commands, decisions, pitfalls) |
| [`../bootstrap/proxmox-ct-config.md`](../bootstrap/proxmox-ct-config.md) | Proxmox CT-spec for VMID 102 (ressurser, network, pct create) |
