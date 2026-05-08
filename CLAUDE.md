# Project: foundry

Lightweight Debian-12 unprivileged LXC (Proxmox VMID 102 på `thehub`) som hoster headless cron-baserte vault-automation-jobber. Felles substrat for batch-jobber som ikke hører hjemme i andre apps-VMer (knowledge-engine, argus-bridge, vault-sentinel) - typisk lette LLM-passes via Claude Code subscription, vault-lint, link-checks, snapshot-triggers.

## Identity

| Field | Value |
|-------|-------|
| **Stack** | Bash + Python 3.11 (per-jobb venvs), system-cron som scheduler |
| **Substrate** | Debian 12 unprivileged LXC, Proxmox VMID 102, Tailscale FQDN `foundry.tail8feda0.ts.net`, IP `10.0.0.52/24` |
| **Status** | Phase 100-500 ferdig (CT live, deploy-pipeline + watchdog + CI grønn). Phase 600 deferred på obsidian-memory-leveranse. |
| **Repository** | https://github.com/Spud80/foundry (PUBLIC per design) |
| **Production** | LXC `foundry` på Proxmox host `thehub` |
| **Version** | 0.5.0 (Phase 500 deploy-pipeline live) |

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
| 600 | Memory-extract aktivering (Phase E payload-import fra obsidian-memory) | 1.0.0 | deferred |

## Repo Layout

| Path | Status | Purpose |
|------|--------|---------|
| `README.md` | exists | Public-facing oversikt + repo-layout-intent |
| `.gitignore` | exists | Ekskluderer runtime state (queue/, logs/, *.venv/, .deploy.lock, .state.json) |
| `bootstrap/proxmox-ct-config.md` | exists | Proxmox CT-spec for VMID 102 |
| `bootstrap/setup-linux.sh` | exists | Idempotent CT-bringup på Debian 12 (Node, claude CLI, watchdog, logrotate) |
| `bootstrap/README.md` | exists | 15-min bringup-runbook + DR-prosedyre + OAuth-token rotation |
| `bootstrap/watchdog-notify.sh` | exists | Watchdog-entrypoint (check-mode + direct-send-mode), source'er notify-core |
| `bootstrap/foundry-watchdog.cron` | exists | Root-eid timesvis cron, deployes til /etc/cron.d/ |
| `bootstrap/logrotate.foundry` | exists | weekly × 12 rotate for ~/foundry/logs/ |
| `cron.d/` | exists | Crontab-fragmenter: auto-update, drain-queue, memory-extract, token-expiry-check |
| `_shared/` | exists | notify-core.sh + notify.sh + drain-queue.sh + token-expiry-check.sh |
| `jobs/memory-extract/` | exists (skeleton) | Phase E memory-extract: run.sh + CONTRACT.md. Payload-guard inert til extract.py importeres |
| `.github/workflows/smoke-test.yml` | exists | CI: shellcheck + cron-syntax + deploy-dry-run + notify-paritet |
| `deploy.sh` | exists | Atomisk regenerering av claude-bruker-crontab fra cron.d/, idempotent venv-setup |
| `auto-update.sh` | exists | Defensiv git-flow på CT (fetch + reset --hard origin/$FOUNDRY_BRANCH + deploy.sh) |
| `docs/ARCHITECTURE.md` | exists | Systemdesign, deploy-flyt, modul-ansvar |

## Architecture Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Substrate | Debian 12 unprivileged LXC på Proxmox | Lett, isolert, kapabel; samme mønster som filehub (CT 101) |
| Scheduler | System-cron, regenerert av deploy.sh fra `cron.d/` | Kjent semantikk, audit-bart, ingen ekstra runtime-stack |
| Auto-deploy | `auto-update.sh` cron `*/15 * * * *` med `git reset --hard origin/$FOUNDRY_BRANCH` (default `dev` til milepæl) | Defensiv: lokale endringer på CT er aldri autoritative; remote er |
| Auth headless | OAuth-token via `claude setup-token` (`CLAUDE_CODE_OAUTH_TOKEN` i secrets.env, ~1 år) | credentials.json refresh-token utløper hvis ikke aktivt brukt - upålitelig for batch-CT |
| Token-expiry-monitoring | Daglig cron leser `CLAUDE_TOKEN_CREATED` fra secrets.env, varsler < 30 dager før 1-års-mark | OAuth-tokens har ingen offentlig `expiresAt`-introspeksjon; dato-basert sanity-check |
| Repo-synlighet | Public | Pipeline-arkitekturen er ikke-sensitiv; secrets bor utenfor repo-treet |
| Watchdog | Separat root-eid cron som varsler ved auto-update-stillstand > 45 min | Defense-in-depth for auto-update-feil |
| Per-jobb venv | Hver `jobs/<x>/.venv/` isolert (gitignored), idempotent setup via fingerprint-cache i deploy.sh | Unngår dependency-konflikter, jobb-fjerning trivielt |
| Time policy | `systemd-timesyncd` masked (CAP_SYS_TIME blokkert i unprivileged LXC) | Per `~/.claude/rules/dev-infra.md` Proxmox guest time policy |
| Tailscale SSH-server | Aktivert (`tailscale set --ssh`) - tailnet-identitet for auth | Eliminerer pubkey-deploy per dev-PC; matcher filehub-mønster |
| Notify pipeline | `_shared/notify-core.sh` source'es av både `_shared/notify.sh` og `bootstrap/watchdog-notify.sh`; queue-fil til `~/foundry/queue/` med drain-cron `*/5` | At-least-once-semantikk; queue overlever Telegram-outage; CI-test verifiserer paritet mellom entrypoints |
| Payload-guard pattern | Hver jobb hvor cron-registrering aktiveres før payload-import har `[ -f <payload> ] || { notify "<job>: payload not deployed"; exit 0; }` som første linje i `run.sh` | Lar inaktive jobber coexiste med aktiv cron uten daglige feil; deploy.sh agnostisk til payload-tilstand |
| Pre-flight Syncthing-cleanliness | `ssh filehub-cleanup <PATH+>` i run.sh - wrapper auto-prepender `--require-clean` på filehub. Exit 1 hvis quarantine i scoped paths | Foundry skal ikke ekstrahere fra ukonsistent sync-state; full /data/sync-scan skjer alltid (idempotent housekeeping) |
| Sync-completion handshake | Manifest-fil i `8.Cortex/Memory/raw/<dato>/_capture-manifest.json` med sha256 per session | Syncthing garanterer ikke fil-rekkefølge; manifest med checksums er deterministisk handshake mellom capture og extract |

## Key Files

| File | Purpose |
|------|---------|
| `README.md` | Public-facing oversikt, dokumentasjons-pekere, repo-layout-intent |
| `bootstrap/proxmox-ct-config.md` | Forankrings-dokument for CT-spec - oppdater når CT-kapasitet endres på Proxmox |
| `bootstrap/README.md` | 15-min bringup-runbook + DR-prosedyre + OAuth-token rotation-prosedyre |
| `auto-update.sh` | Eier branch-strategien (`FOUNDRY_BRANCH` env-var); flock-beskyttet single-flight; loggsemantikk OK/FAIL drives av watchdog |
| `deploy.sh` | Atomisk crontab-swap, idempotent venv-setup med fingerprint-cache (sha256 av requirements.txt + python-versjon) |
| `_shared/notify-core.sh` | Felles queue+drain-helpers; source'es av både notify.sh og watchdog-notify.sh - paritet enforced av CI |
| `jobs/memory-extract/CONTRACT.md` | Leveranse-kontrakt mot obsidian-memory: env-vars, exit-codes, pre-flight, manifest-format-referanse |
| `.gitignore` | Runtime-state ekskludert; secrets MÅ leve utenfor repo-treet |
| `docs/ARCHITECTURE.md` | Deploy-flyt, komponent-diagram, modul-ansvar |
| `CLAUDE.md` | Denne filen - Claude Code project-config |

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
