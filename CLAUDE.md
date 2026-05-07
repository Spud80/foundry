# Project: foundry

Lightweight Debian-12 unprivileged LXC (Proxmox VMID 102 på `thehub`) som hoster headless cron-baserte vault-automation-jobber. Felles substrat for batch-jobber som ikke hører hjemme i andre apps-VMer (knowledge-engine, argus-bridge, vault-sentinel) - typisk lette LLM-passes via Claude Code subscription, vault-lint, link-checks, snapshot-triggers.

## Identity

| Field | Value |
|-------|-------|
| **Stack** | Bash + Python 3.12+ (per-jobb venvs), system-cron som scheduler |
| **Substrate** | Debian 12 unprivileged LXC, Proxmox VMID 102, Tailscale FQDN `foundry.tail8feda0.ts.net`, IP `10.0.0.52/24` |
| **Status** | Bootstrap (scaffold-fase, ingen deployerte jobber enda) |
| **Repository** | https://github.com/Spud80/foundry (PUBLIC per design) |
| **Production** | LXC `foundry` på Proxmox host `thehub` |
| **Version** | 0.1.0 (initial scaffold) |

## Deployment Model (kritisk å forstå før commit)

Foundry avviker fra standard dev/main-konvensjonen i `~/.claude/rules/workflow.md`. `main` er auto-deploy-branch:

- `auto-update.sh` cron-kjører `git fetch + reset --hard origin/main + deploy.sh` hvert 15. min på CT 102.
- Push til `main` -> live i produksjon innen 15 min.

Konsekvenser:

- Daglig arbeid skjer på `dev`-branch lokalt.
- Endringer flyttes til main via `/release`-skill (commit-melding `release: vX.Y.Z` + tag).
- Aldri direkte commit eller push til main utenom `/release`.
- `block-main-modify.sh`-hooken enforcer dette.

## Commands

```bash
# Sync arbeidskopi (etter pull eller før release-merge)
git fetch --all --prune

# Lokal validering før release (når deploy.sh finnes)
./deploy.sh --dry-run

# CT-bringup (idempotent, fra Proxmox-host) - planlagt for Phase 100
ssh thehub "sudo bash /tmp/setup-linux.sh"

# CT-status
ssh thehub "sudo pct status 102"

# Inn i CT (via Tailscale SSH når Phase 100 er ferdig)
ssh foundry

# Sjekk auto-update-logg på CT
ssh foundry "tail -50 ~/foundry/logs/auto-update.log"
```

## Goals & Roadmap

| Phase | Description | Version | Status |
|-------|-------------|---------|--------|
| 0 | Repo-scaffold + Proxmox CT-spec forankret | 0.1.0 | done |
| 100 | LXC bringup på thehub (pct create + setup-linux.sh + Tailscale up) | 0.2.0 | planning |
| 200 | Auto-update + watchdog skeleton (auto-update.sh, deploy.sh, watchdog cron) | 0.3.0 | planning |
| 300 | Første jobb integrert (Phase E memory-extract) | 1.0.0 | future |

## Repo Layout

| Path | Status | Purpose |
|------|--------|---------|
| `README.md` | exists | Oversikt + dokumentasjons-pekere + repo-layout-intent |
| `.gitignore` | exists | Ekskluderer runtime state (queue/, logs/, *.venv/, .deploy.lock, .state.json) |
| `bootstrap/proxmox-ct-config.md` | exists | Proxmox CT-spec for VMID 102 |
| `bootstrap/setup-linux.sh` | planlagt (Phase 100) | Idempotent CT-bringup på Debian 12 |
| `bootstrap/README.md` | planlagt (Phase 100) | 15-min bringup-runbook + DR-prosedyre |
| `cron.d/` | planlagt (Phase 200) | Crontab-fragmenter (én per jobb), regenereres av deploy.sh |
| `_shared/` | planlagt (Phase 200) | Felles helpers (notify, drain-queue) |
| `jobs/<jobname>/` | planlagt (Phase 300+) | Per-jobb mappe med run.sh, requirements.txt |
| `ci/` | planlagt | GitHub Actions (shellcheck + ruff + deploy.sh --dry-run) |
| `deploy.sh` | planlagt (Phase 200) | Atomisk regenerering av system-crontab fra cron.d/ |
| `auto-update.sh` | planlagt (Phase 200) | Defensiv git-flow på CT (fetch + reset --hard + deploy.sh) |
| `docs/ARCHITECTURE.md` | exists | Systemdesign, deploy-flyt, modul-ansvar |

## Architecture Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Substrate | Debian 12 unprivileged LXC på Proxmox | Lett, isolert, kapabel; samme mønster som filehub (CT 101) |
| Scheduler | System-cron, regenerert av deploy.sh fra `cron.d/` | Kjent semantikk, audit-bart, ingen ekstra runtime-stack |
| Auto-deploy | `auto-update.sh` cron `*/15 * * * *` med `git reset --hard origin/main` | Defensiv: lokale endringer på CT er aldri autoritative; main er |
| Repo-synlighet | Public | Pipeline-arkitekturen er ikke-sensitiv; secrets bor utenfor repo-treet |
| Watchdog | Separat root-eid cron som varsler ved auto-update-stillstand > 45 min | Defense-in-depth for auto-update-feil |
| Per-jobb venv | Hver `jobs/<x>/.venv/` isolert (gitignored) | Unngår dependency-konflikter mellom jobber, gjør jobb-fjerning trivielt |
| Time policy | `systemd-timesyncd` masked (CAP_SYS_TIME blokkert i unprivileged LXC) | Per `~/.claude/rules/dev-infra.md` Proxmox guest time policy |
| Tailscale SSH-server | Deferred (kun `tailscale up` ved bringup) | Vurderes senere når use-case er klart |

## Key Files

| File | Purpose |
|------|---------|
| `README.md` | Public-facing oversikt, dokumentasjons-pekere, repo-layout-intent |
| `bootstrap/proxmox-ct-config.md` | Forankrings-dokument for CT-spec - oppdater når CT-kapasitet endres på Proxmox |
| `.gitignore` | Runtime-state ekskludert; secrets MÅ leve utenfor repo-treet |
| `docs/ARCHITECTURE.md` | Deploy-flyt, komponent-diagram, modul-ansvar |
| `CLAUDE.md` | Denne filen - Claude Code project-config |

## Known Pitfalls

- `main` er auto-deploy-branch - direkte push lander i produksjon innen 15 min.
- `auto-update.sh` bruker `git reset --hard origin/main` - alle lokale endringer på CT mistes ved hver kjøring (by design).
- Secrets MÅ leve utenfor repo-treet (`~/.config/foundry/secrets.env`, `~/.claude/.credentials.json`). Aldri commit secrets - repoet er PUBLIC.
- TUN-device-eksponering må appendes manuelt til `/etc/pve/lxc/102.conf` etter `pct create` (ikke et `pct create`-flagg).
- Tailscale SSH-server er deferred - bringup setter kun `tailscale up`, ikke `tailscale set --ssh`.
- Auto-update kan ikke regenerere seg selv mens egen kjøring pågår - `.deploy.lock` enforcer single-flight.

## When Uncertain

- Aldri push direkte til main utenom `/release`-skillen.
- Hvis det er tvil om en endring kan vente på neste release-syklus, lat den vente - auto-deploy gjør hver merge til main til en produksjons-deploy.
- Spør før arkitektur-endringer i deploy-flyten (auto-update, deploy.sh, cron-regenerering).
- Aldri legg secrets eller credentials i repo-treet.

## Definition of Done

En endring er ferdig når:

- [ ] CI lint og smoke-tester passerer (når `ci/`-mappa finnes)
- [ ] `bootstrap/proxmox-ct-config.md` speiler eventuelle CT-ressurs-justeringer
- [ ] `docs/ARCHITECTURE.md` oppdatert hvis deploy-flyt eller modul-struktur endres
- [ ] Ingen TODO/FIXME igjen i endrede filer
- [ ] Ingen nye fil-paths under `~/.config/foundry/` eller `~/.claude/` referert uten å være dokumentert i README

## Architecture

Les `docs/ARCHITECTURE.md` ved strukturelle endringer (deploy-flyt, jobb-struktur, runtime-isolasjon).

## Documentation

Etter signifikante endringer (nye features, deploy-flyt-justering, ny jobb-type): kjør `/update-docs` for å sync `README.md` + `docs/ARCHITECTURE.md`.
