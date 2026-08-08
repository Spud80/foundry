# Architecture

## Overview

foundry er et Debian-12 unprivileged LXC (Proxmox VMID 102 på `thehub`) som fungerer som felles substrat for headless cron-baserte vault-automation-jobber. Designet er bevisst minimalistisk: cron + bash + Python (per-jobb venvs), ingen langvarige tjenester eller daemoner utenom auto-update og watchdog.

Primært arkitektur-pattern: **defensiv pull-based deploy**. CT-en er ikke kilde-sannhet for noe - alt avgjørende state lever enten i git-repoet (kode, cron-fragmenter) eller utenfor CT-treet (secrets, eventuelle persistente queues). Auto-update gjør `git reset --hard origin/$FOUNDRY_BRANCH` hvert 15. min (default `dev` mens pipeline-en stabiliseres; flippes til `main` senere via env-var i auto-update.sh), så lokale modifikasjoner på CT er per design transient.

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
|  |  fetch + reset       |    |  claude-user crontab |          |
|  |  (flock-protected)   |    |  + idempotent venvs  |          |
|  +----------+-----------+    +----------+-----------+          |
|             |                           |                      |
|             v                           v                      |
|  +----------------------+    +----------------------+          |
|  |  ~/foundry/ (repo)   |    |  user crontab        |          |
|  |  bootstrap/          |    |  + /etc/cron.d/      |          |
|  |  cron.d/             |    |    foundry-watchdog  |          |
|  |  jobs/<job>/         |    |    (root-owned)      |          |
|  |  _shared/            |    +----------+-----------+          |
|  |  .deploy.lock        |               |                      |
|  +----------------------+               | triggers             |
|                                         v                      |
|                              +----------------------+          |
|                              |  Per-jobb run.sh     |          |
|                              |  payload-guard ->    |          |
|                              |  source secrets.env  |          |
|                              |  flock .deploy.lock  |          |
|                              |  pre-flight cleanup  |          |
|                              |  exec .venv/python   |          |
|                              +----------+-----------+          |
|                                         |                      |
|                                         v                      |
|                              +-----------------------+         |
|                              | Outbound side-effects:          |
|                              |  - Claude API (CLAUDE_CODE_     |
|                              |    OAUTH_TOKEN env-var)         |
|                              |  - Syncthing folder writes      |
|                              |    (vault, claude-memory)       |
|                              |  - Telegram (notify queue       |
|                              |    -> drain-queue cron)         |
|                              |  - SSH to filehub-cleanup       |
|                              +-----------------------+         |
+----------------------------------------------------------------+

Secrets (out-of-tree):  ~/.config/foundry/secrets.env
                        - CLAUDE_CODE_OAUTH_TOKEN (~1 yr)
                        - CLAUDE_TOKEN_CREATED (date for expiry-check)
                        - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                        - foundry-syncthing-api-key, device-id
```

## Data Flow

### Deploy-flyt (kontinuerlig)

1. Utvikler committer på `dev` (eller via `/release` til `main` post-stabilisering) og pusher til origin.
2. `auto-update.sh` kjører via cron `*/15 * * * *` på CT 102, flock-beskyttet på `.deploy.lock`:
   - `git fetch origin $FOUNDRY_BRANCH`
   - Hvis `HEAD != origin/$FOUNDRY_BRANCH`: `git reset --hard origin/$FOUNDRY_BRANCH`, deretter `bash deploy.sh`
   - Hvis `.deploy.lock` busy (jobb pågår): `touch_mtime_only` på loggen så watchdog ikke alarmerer; ingen append.
3. `deploy.sh` regenererer claude-bruker-crontab atomisk fra `cron.d/`-fragmenter (validerer cron-syntaks, skriver via temp-fil + crontab-replace, ruller tilbake ved feil) og setter opp idempotente per-jobb venvs (sha256-fingerprint av requirements.txt + python-versjon i `.venv-fingerprint` så reinstall kun skjer ved endring).
4. Cron leser nye fragmenter ved neste minutt-tikk; jobber starter på sin schedule.

### Job-eksekvering (memory-extract som referanse-implementasjon)

1. Cron-fragment trigger `jobs/memory-extract/run.sh` 18:30 daglig.
2. **Payload-guard:** hvis `extract.py` mangler -> notify "payload not deployed" + exit 0. Lar inaktive jobber coexiste med aktiv cron uten daglige feil.
3. **Source secrets.env** for `CLAUDE_CODE_OAUTH_TOKEN`. FATAL hvis mangler.
4. **`flock --nonblock .deploy.lock`** - serialiserer mot auto-update.sh og andre jobber.
5. **Pre-flight Syncthing-cleanliness:** `ssh filehub-cleanup <PATH+>` - wrapper auto-prepender `--require-clean`, returnerer exit 1 hvis quarantines lander under scoped paths. Cleanup-script kjører alltid full `/data/sync`-scan (idempotent housekeeping). Default scope: `/data/sync/obsidian /data/sync/claude-memory`.
6. **Glob-assert** på `<raw-root>/<dato>/` (`$CORTEX_RAW_ROOT`, ellers `<vault>/8.Cortex/Memory/raw/`). Manifest-handshake (Phase 600): verifisér `_capture-manifest.json` tilstede + alle listed paths lokalt + sha256 matcher. Hvis manifest sier N filer men M ankommet: exit 0 stille (capture sync incomplete; cron retry neste dag).
7. **`timeout 30m .venv/bin/python extract.py`** - exit-code propageres til notify (0=OK, 1=transient, 2=fatal, 124=timeout).
8. Output i `~/foundry/logs/<job>.log` (logrotate ukentlig × 12 uker).
9. Ved feil/timeout/fatal: `_shared/notify.sh` enqueue + drain til Telegram.

### Notify-pipeline (queue + drain)

1. `_shared/notify.sh` enqueue til `~/foundry/queue/<timestamp>-<pid>.msg` og forsøker immediate drain.
2. `_shared/drain-queue.sh` kjøres via cron `*/5 * * * *` for retry på Telegram-outage. POSTer queued meldinger; sletter ved 200-respons.
3. Begge wrappers source'er `_shared/notify-core.sh` for enqueue/drain-helpers. CI verifiserer paritet mellom `notify.sh` og `bootstrap/watchdog-notify.sh`.

### Watchdog (defense-in-depth)

1. `foundry-watchdog`-cron (root-eid, deployes til `/etc/cron.d/foundry-watchdog` av setup-linux.sh, IKKE regenerert av deploy.sh) kjører timesvis (`0 * * * *`).
2. Kaller `~/.config/foundry/watchdog-notify.sh` (kopiert ut av setup-linux.sh så det er robust mot deploy-feil) som sjekker `~/foundry/logs/auto-update.log` for OK-linje siste 45 min.
3. Hvis manglende: sender Telegram-varsel direkte via `notify-core.sh` (også kopiert til `~/.config/foundry/`) - ikke via repo-treet, fordi en feil som broke auto-update kan også broke repo-tilgang.

### Token-expiry-monitoring

1. `_shared/token-expiry-check.sh` kjøres via cron daglig 09:00 norsk lokal-tid.
2. Leser `CLAUDE_TOKEN_CREATED` (ISO-dato) fra secrets.env, beregner dager til 1-års-mark.
3. Varsler via notify-pipeline når < 30 dager. Eksplisitt sjekk for unset eller malformert dato (alarm med (FATAL)-prefiks).

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Deploy-strategi | Pull-based via auto-update.sh + `git reset --hard origin/$FOUNDRY_BRANCH` | Lokal CT-state er aldri autoritativ; konflikter umulig per design |
| Branch-strategi | `FOUNDRY_BRANCH=dev` under utvikling, flippes til `main` post-stabilisering | Unngå main-merge før pipeline er bevist stabil i drift |
| Scheduler | System-cron (claude-user crontab regenerert av deploy.sh; root-cron for watchdog) | Kjent semantikk, audit-bart, ingen ekstra runtime |
| Auth headless | OAuth-token via `CLAUDE_CODE_OAUTH_TOKEN` env-var fra secrets.env | credentials.json refresh-token utløper for ikke-aktivt-brukte hosts |
| Job-isolasjon | Per-jobb venvs (`jobs/<x>/.venv/`), idempotent setup via fingerprint-cache | Unngår dependency-konflikter; reinstall kun ved endring av requirements |
| Watchdog-eier | Root-eid cron i `/etc/cron.d/`, separat fra auto-update | Skal varsle hvis auto-update selv feiler; deployes av setup-linux.sh, ikke regenerert av deploy.sh |
| Notify-pipeline | Enqueue til `~/foundry/queue/`, immediate-drain + `*/5`-cron drain | At-least-once-semantikk; queue overlever Telegram-outage |
| Notify-paritet | `_shared/notify-core.sh` source'es av både notify.sh og watchdog-notify.sh | CI-test verifiserer at de to entrypoints produserer identisk Telegram-format |
| Watchdog-robusthet | `~/.config/foundry/watchdog-notify.sh` + `notify-core.sh` kopiert ut av repo-tre av setup-linux.sh | Hvis repo-tre er broken må watchdog fortsatt nå ut |
| Payload-guard | `[ -f extract.py ] || { notify "payload not deployed"; exit 0; }` som første linje i run.sh | Lar cron-registrering aktiveres før payload-import; inaktive jobber gir ikke daglige feil |
| Pre-flight cleanliness | `ssh filehub-cleanup <PATH+>` med wrapper-auto-prepended `--require-clean` | Memory-extract aborterer hvis filehub-Syncthing-state har quarantines under memory-relevante paths |
| Sync-handshake | Manifest-fil med sha256 per session i `_capture-manifest.json` | Syncthing garanterer ikke fil-rekkefølge; manifest gir deterministisk capture→extract handshake |
| LXC-type | Unprivileged | Default sikkerhets-posture; CAP_SYS_TIME-tap akseptert |
| Time-sync | `systemd-timesyncd` masked, host-klokka arves | CAP_SYS_TIME blokkert i unprivileged LXC uansett |
| Repo-synlighet | Public | Pipeline ikke-sensitiv; secrets lever out-of-tree |
| Single-flight | `.deploy.lock` flock-fil; ved BUSY: `touch_mtime_only` (ingen logg-append) | Forhindrer overlappende deploy + watchdog false-positive ved lange jobber |

## Module Structure

- `bootstrap/` - Idempotent CT-bringup (`setup-linux.sh`, watchdog-entrypoint, logrotate-config, runbook). Kjøres én gang per CT-instans, må kunne re-kjøres uten å ødelegge eksisterende state. Watchdog-helpers (`watchdog-notify.sh`, `notify-core.sh`) kopieres til `~/.config/foundry/` slik at de er robuste mot repo-tre-feil.
- `bootstrap/proxmox-ct-config.md` - CT-spec (VMID, ressurser, network, `pct create`-kommando). Forankrings-dokument; oppdateres når CT-kapasitet endres på Proxmox.
- `cron.d/` - Crontab-fragmenter (én fil per jobb): `auto-update.cron` (`10,25,40,55 * * * *`), `drain-queue.cron` (`*/5`), `memory-extract.cron` (`30 18`), `fallback-classifier.cron` (`0 3`), `audit.cron` (`0 4`), `capture-heartbeat.cron` (`0 19`), `token-expiry-check.cron` (`0 9`). Konsumeres av `deploy.sh` for atomisk regenerering av claude-bruker-crontab. Alle jobber serialiseres mot `~/foundry/.deploy.lock`.
- `_shared/` - Felles helpers source'et av flere wrappers/jobber: `notify-core.sh` (queue/drain-implementasjon), `notify.sh` (enqueue + immediate drain), `drain-queue.sh` (cron-drain), `token-expiry-check.sh` (OAuth-token expiry-warning), `capture-heartbeat.sh` (memory-pipeline-heartbeat). Underscore-prefiks for at `jobs/`-iterasjon ikke skal traversere.
- `jobs/<jobname>/` - Per-jobb mappe: `run.sh`, `requirements.txt` (om Python), `CONTRACT.md` (om jobben krysser prosjekt-grenser), per-jobb `.venv/` (gitignored). Ingen kryss-jobb-imports.
- `jobs/memory-extract/` - Phase 600 LLM-extract over raw-korpuset (`<raw-root>`, se over). `run.sh` med payload-guard + pre-flight + flock + timeout; `extract.py` orkestrerer aliases-consumption (aliases.yaml-normalisering, Phase 700) + sources-append (Sources-append til `compiled/`, Phase 800). `CONTRACT.md` definerer leveranse-grensesnitt mot obsidian-memory-prosjektet.
- `jobs/fallback-classifier/` - Phase 900 nattlig classifier for `pending-foundry-*.md` med `pre_classified: partial|none`. `classify.py` kaller `claude -p`, atomic mutate-first-then-rename til `ai-capture-*.md`; 4-state recovery-scan håndterer mid-write-krasj. `CONTRACT.md` mot PLAN-3X Phase 600 batch-processor.
- `jobs/audit/` - Phase 1000 nattlig audit-pass per `cortex/docs/contracts/audit-pass-spec.md`. `audit.py` kjører 6 sjekker (dedup / schema / wikilinks / sampling-classification / tags / missing-required-field), aggregerer tier (silent/lav/hoy/kritisk), skriver atomic rapport til `5.Utility/Pipeline/Audit-Reports/YYYY-MM-DD.md` + heartbeat til `~/.audit-state.json`. Watchdog leser heartbeat for 25h-staleness-varsel.
- `.github/workflows/smoke-test.yml` - CI: shellcheck (severity=warning, ekskluderer SC1090/1091/2034), cron-syntaks-validering, `deploy.sh DRY_RUN=1`, notify-paritet (mock-curl + diff av formatert Telegram-API-call).
- `deploy.sh` - Top-level: regenererer claude-bruker-crontab atomisk fra `cron.d/`, idempotent venv-setup med fingerprint-cache.
- `auto-update.sh` - Top-level: defensiv git-flow på CT-en (fetch + reset --hard + deploy.sh), flock-beskyttet, branch styres av `FOUNDRY_BRANCH`.

## External Dependencies

- **GitHub (Spud80/foundry)** - kilde-sannhet for kode. `auto-update.sh` puller `origin/$FOUNDRY_BRANCH` hvert 15. min.
- **Claude API** via `claude -p` headless med `CLAUDE_CODE_OAUTH_TOKEN` env-var (~1 år; generert via `claude setup-token` på desktop, deployet til `~/.config/foundry/secrets.env` på CT). Token-rotation 30-dagers-varsel via `_shared/token-expiry-check.sh`.
- **Filehub (LXC 101 på thehub)** - Syncthing-peer for `obsidian` og `claude-memory` folder-shares; `filehub-cleanup`-bruker via login-shell-wrapper (`/usr/local/bin/foundry-cleanup-login-shell`) som auto-prepender `--require-clean` på `sync-conflict-cleanup.py`. Cleanup-skriptet eier device-sync-and-backup-prosjektet.
- **Syncthing (filehub <-> foundry)** - bidirektional sync. v1.30.0 på foundry, v2.0.16 på filehub (protokoll-kompatibelt). Folder `obsidian` -> `/home/claude/vault`, `claude-memory` -> `/home/claude/claude-memory`. `~/.claude/projects/` på foundry er IKKE i sync-scope (selvrekursjons-isolasjon).
- **Telegram Bot API** - for varsling (failure-notifications, watchdog-alerts, token-expiry-warnings). Bot-token i `~/.config/foundry/secrets.env`.
- **Tailscale tailnet (`tail8feda0.ts.net`)** - for inn/ut-tilgang. CT autentiseres med tailnet-identitet (`tailscale set --ssh` aktivert ved bringup; auth=none over tailnet).
- **Proxmox host `thehub`** - substrate-host. CT-spec forankret i `bootstrap/proxmox-ct-config.md`. Snapshot-policy ukentlig × 4 styres på Proxmox-host (ikke i dette repoet, avhenger av device-sync-and-backup Phase 300).

## Test Infrastructure

Foundry har ingen runtime-database. Tester kjører i CI:

- **Shell-lint:** `shellcheck` på alle `*.sh`-filer i repo.
- **Python-lint:** `ruff check` per jobb (når jobs/-mapper finnes).
- **Smoke-test:** `deploy.sh --dry-run` for å verifisere at `cron.d/`-fragmenter parser korrekt og at output-crontab er valid.
- **Job-spesifikke tester:** kjøres i jobb-mappa via `jobs/<job>/test.sh` om filen finnes.

CI går via GitHub Actions (`.github/workflows/smoke-test.yml`, ferdig Phase 500). 4 jobs: shellcheck, cron-syntax, deploy-dry-run, notify-paritet. Workflow trigges på push til alle branches.

## Disaster Recovery

CT-en er stateless utenom secrets, logs, queue og Syncthing-state. Tap av CT-en innebærer:

- Kode: gjenopprettes fra GitHub (`git clone`).
- Secrets: må re-deployes fra 1Password til `~/.config/foundry/secrets.env` (OAuth-token, Telegram-tokens, Syncthing-keys). Notepad+SCP-mønster med CRLF-strip; full prosedyre i `bootstrap/README.md`.
- Syncthing-state: foundry må re-pares mot filehub via API (4-stegs flow dokumentert i memory `reference_syncthing_api_peer_pairing.md`).
- Cron-state: regenereres av `deploy.sh` ved første post-bringup-kjøring.
- Logs: borte (akseptabelt - logger er ikke kilde-sannhet).
- Queue: hvis ikke-tomme `~/foundry/queue/`-meldinger ved CT-tap er disse tapt; akseptert siden hver jobb sender ny notify ved neste run hvis problemet vedvarer.

DR-prosedyre detaljert i `bootstrap/README.md` (Phase 200).

Snapshot-policy: Proxmox-snapshot ukentlig × 4 (rolling) per `bootstrap/proxmox-ct-config.md`. Konfigureres på Proxmox-host, ikke i dette repoet (avhenger av device-sync-and-backup Phase 300 vzdump-aktivering).

## Related Documents

| Document | Content |
|----------|---------|
| [`../README.md`](../README.md) | Public-facing oversikt, repo-layout-intent |
| [`../CLAUDE.md`](../CLAUDE.md) | Claude Code project-config (commands, decisions, pitfalls) |
| [`../bootstrap/proxmox-ct-config.md`](../bootstrap/proxmox-ct-config.md) | Proxmox CT-spec for VMID 102 (ressurser, network, pct create) |
