# Project: foundry

Lightweight Debian-12 unprivileged LXC (Proxmox VMID 102 på `thehub`) som hoster headless cron-baserte vault-automation-jobber. Felles substrat for batch-jobber som ikke hører hjemme i andre apps-VMer (knowledge-engine, argus-bridge, vault-sentinel) - typisk lette LLM-passes via Claude Code subscription, vault-lint, link-checks, snapshot-triggers.

## Identity

| Field | Value |
|-------|-------|
| **Stack** | Bash + Python 3.11 (per-jobb venvs), system-cron som scheduler |
| **Substrate** | Debian 12 unprivileged LXC, Proxmox VMID 102, Tailscale FQDN `foundry.tail8feda0.ts.net`, IP `10.0.0.52/24` |
| **Status** | Phase 100-800 ferdig. Phase 900 (fallback-classifier 03:00) + Phase 1000 (audit 04:00) strukturelt deployet + cron-aktivt; reelle ende-til-ende-fyringer mot syntetiske input organisk pågående. |
| **Repository** | https://github.com/Spud80/foundry (PUBLIC per design) |
| **Production** | LXC `foundry` på Proxmox host `thehub` |
| **Version** | 0.5.0 (Phase 500 deploy-pipeline live; Phase 600-1000-iterasjoner deployes via auto-update mot `origin/dev` uten formell version-bump - venter på post-stabilisering main-flipp) |

## Deployment Model (kritisk å forstå før commit)

Foundry avviker fra standard dev/main-konvensjonen i `~/.claude/rules/workflow.md`. Auto-update-branch styres av `FOUNDRY_BRANCH` env-var i `auto-update.sh`:

- **Nåværende (Phase 500 utviklings-fase):** `auto-update.sh` puller `origin/dev` hvert 15. min. Push til `dev` -> live i produksjon innen 15 min. Brukes mens vi stabiliserer pipeline-en.
- **Senere (post-stabilisering):** `FOUNDRY_BRANCH` flippes til `main`; daglig arbeid på `dev`, releases via `/release`-skill (merge til main + tag).

Konsekvenser i nåværende fase:

- Hver push til `dev` lander i produksjon innen 15 min - ingen skille mellom WIP-commits og prod-deploy.
- Branch-flippen til main skjer når foundry har vært stabil noen dager (vurderes etter Phase 600 aktivering).
- `block-main-modify.sh`-hooken blokkerer fortsatt main-modifikasjoner utenom `/release`.

## Commands

```bash
# Sync arbeidskopi
git fetch --all --prune

# Lokal validering av cron-fragmenter + deploy-pipeline
DRY_RUN=1 ./deploy.sh

# CT-status (fra Proxmox-host)
ssh thehub "sudo pct status 102"

# Inn i CT (via Tailscale SSH)
ssh foundry

# Sjekk auto-update-logg på CT
ssh foundry "tail -50 ~/foundry/logs/auto-update.log"

# Sjekk aktiv crontab på CT
ssh foundry "crontab -l"

# Manuell test av notify-pipeline
ssh foundry "~/foundry/_shared/notify.sh 'manual test from CLAUDE.md'"
```

## Goals & Roadmap

| Phase | Description | Version | Status |
|-------|-------------|---------|--------|
| 0 | Repo-scaffold + Proxmox CT-spec forankret | 0.1.0 | done |
| 100 | LXC bringup på thehub (pct create + setup-linux.sh + Tailscale SSH up) | 0.2.0 | done |
| 200 | Bootstrap-runbook + setup-linux.sh + watchdog skeleton | 0.3.0 | done |
| 300 | OAuth-token-auth + secrets-deploy + token-expiry-check | 0.4.0 | done |
| 400 | Syncthing-peer mot filehub (vault + claude-memory bidirektional) | 0.4.5 | done |
| 500 | Deploy-pipeline (deploy.sh, auto-update.sh) + notify-wrappers + CI smoke-test + memory-extract job-skeleton | 0.5.0 | done |
| 600 | Memory-extract aktivering (Phase E payload-import fra obsidian-memory; 18:30 norsk lokal-tid) | - | done |
| 700 | Aliases.yaml producer-side normalisering i extract.py | - | done (strukturelt; steady-state-verifisering organisk) |
| 800 | Sources-append-pass i extract.py + dedikert `memory-sources-append`-CLI | - | done |
| 900 | Foundry-fallback classifier-cron (03:00 norsk lokal-tid) for `pending-foundry-*.md` | - | strukturelt deployet 2026-05-13 commit `65357ed`; ende-til-ende-test pending PLAN-3X Phase 600 |
| 1000 | Audit-pass nightly cron (04:00 norsk lokal-tid) - 6 audit-sjekker per `audit-pass-spec.md` | - | strukturelt deployet 2026-05-14 commit `fb8945e`; første reelle 04:00-fyring 2026-05-15 |

## Repo Reference

Repo-layout, Architecture Decisions (ADR-tabell) og Key Files (fil-roller) - detaljert reference flyttet til [docs/repo-reference.md](docs/repo-reference.md), lastes ved behov. Systemdesign + deploy-flyt: `docs/ARCHITECTURE.md`.

## Known Pitfalls

- `dev` er nåværende auto-deploy-branch (`FOUNDRY_BRANCH=dev` i auto-update.sh) - hver push til dev lander i produksjon innen 15 min. Branch-flippen til main er én env-var-endring når foundry har vært stabil noen dager.
- `auto-update.sh` bruker `git reset --hard origin/$FOUNDRY_BRANCH` - alle lokale endringer på CT mistes ved hver kjøring (by design).
- Secrets MÅ leve utenfor repo-treet (`~/.config/foundry/secrets.env` med `CLAUDE_CODE_OAUTH_TOKEN` + Telegram-tokens). Aldri commit secrets - repoet er PUBLIC.
- OAuth-token-rotation: `claude setup-token` på desktop genererer ny token (vises kun én gang). Oppdater 1Password + endre `CLAUDE_CODE_OAUTH_TOKEN` + `CLAUDE_TOKEN_CREATED` i secrets.env. Token-expiry-check varsler 30 dager før 1-års-mark.
- secrets.env-deploy via Notepad+SCP introduserer CRLF-line-endings; kjør `sed -i 's/\r$//' ~/.config/foundry/secrets.env` på CT etter SCP. Bakt inn i bootstrap/README.md deploy-mønster.
- TUN-device-eksponering må appendes manuelt til `/etc/pve/lxc/102.conf` etter `pct create` (ikke et `pct create`-flagg).
- Auto-update kan ikke regenerere seg selv mens egen kjøring pågår - `.deploy.lock` enforcer single-flight. Ved BUSY: logg-fil får `touch_mtime_only` (ingen append) sa watchdog ikke alarmerer.
- `ssh filehub-cleanup <PATH+>` har sideeffekt: full `/data/sync`-scan kjøres alltid (idempotent housekeeping) - det er ikke et read-only check. Wrapper auto-prepender `--require-clean`; caller sender bare path-args, ingen flagg kommer gjennom.
- Path-arg til filehub-cleanup må være uten mellomrom (wrapper word-splitter); bruk folder-roots `/data/sync/obsidian /data/sync/claude-memory`, ikke subpath som inkluderer "My Vault".
- `bootstrap/`-filer (`watchdog-notify.sh`, `notify-core.sh`, `foundry-watchdog.cron`) er BEVISST utenfor `deploy.sh` / auto-update-pipelinen. Self-monitoring må ikke bygge på det den overvåker. Endringer her krever manuell scp til eksisterende CT-er - prosedyre er dokumentert som header-banner i `bootstrap/watchdog-notify.sh`. Nye CT-er får siste versjon automatisk via `setup-linux.sh` ved provisioning.

## When Uncertain

- Aldri push direkte til main utenom `/release`-skillen.
- Hvis det er tvil om en endring kan vente på neste release-syklus, lat den vente - auto-deploy gjør hver merge til main til en produksjons-deploy.
- Spør før arkitektur-endringer i deploy-flyten (auto-update, deploy.sh, cron-regenerering).
- Aldri legg secrets eller credentials i repo-treet.

## Definition of Done

En endring er ferdig når:

- [ ] CI lint og smoke-tester grønne (`.github/workflows/smoke-test.yml`)
- [ ] `bootstrap/proxmox-ct-config.md` speiler eventuelle CT-ressurs-justeringer
- [ ] `docs/ARCHITECTURE.md` oppdatert hvis deploy-flyt eller modul-struktur endres
- [ ] Ingen TODO/FIXME igjen i endrede filer
- [ ] Ingen nye fil-paths under `~/.config/foundry/` eller `~/.claude/` referert uten å være dokumentert i README

## Architecture

Les `docs/ARCHITECTURE.md` ved strukturelle endringer (deploy-flyt, jobb-struktur, runtime-isolasjon).

## Documentation

Etter signifikante endringer (nye features, deploy-flyt-justering, ny jobb-type): kjør `/update-docs` for å sync `README.md` + `docs/ARCHITECTURE.md`.
