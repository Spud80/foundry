# Foundry - Repo Reference

Detaljert repo-map, arkitektur-beslutninger og fil-roller. Flyttet fra `CLAUDE.md` 2026-06-12 for context-footprint - innholdet er uendret. Kjerne-orientering (deploy-modell, Known Pitfalls, When Uncertain) bor fortsatt i `CLAUDE.md`; systemdesign + deploy-flyt i `docs/ARCHITECTURE.md`.

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
| `cron.d/` | exists | Crontab-fragmenter: auto-update (`10,25,40,55 * * * *`), drain-queue (`*/5`), memory-extract (`30 18`), fallback-classifier (`0 3`), audit (`0 4`), capture-heartbeat (`0 19`), token-expiry-check (`0 9`) |
| `_shared/` | exists | notify-core.sh + notify.sh + drain-queue.sh + token-expiry-check.sh + capture-heartbeat.sh |
| `jobs/memory-extract/` | exists (Phase 600+700+800) | Phase E memory-extract: run.sh + CONTRACT.md + extract.py (LLM-classify + aliases-consumption aliases-normalisering + Sources-append) + reconcile-manifest.py (operator-recovery: manifest sha256-reconcile, vendret fra cortex) |
| `jobs/fallback-classifier/` | exists (Phase 900) | Foundry-fallback classifier for `pending-foundry-*.md`: run.sh + CONTRACT.md + system-prompt.md + classify.py + smoke-test. Atomic mutate-first-then-rename + 4-state recovery-scan |
| `jobs/audit/` | exists (Phase 1000) | Nattlig audit-pass per `cortex/docs/contracts/audit-pass-spec.md`: run.sh + CONTRACT.md + system-prompt.md + audit.py + smoke-test. 6 audit-sjekker, tiered Telegram, rapport-fil til `5.Utility/Pipeline/Audit-Reports/YYYY-MM-DD.md`, heartbeat-state `~/.audit-state.json` |
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
| Sync-completion handshake | Manifest-fil i `<raw-root>/<dato>/_capture-manifest.json` (`$CORTEX_RAW_ROOT`, ellers `<vault>/8.Cortex/Memory/raw/`) med sha256 per session | Syncthing garanterer ikke fil-rekkefølge; manifest med checksums er deterministisk handshake mellom capture og extract |

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
| `jobs/fallback-classifier/CONTRACT.md` | Phase 900 runtime-kontrakt: input `pending-foundry-*.md`, atomic mutate-first-then-rename, 4-state recovery-scan, exit-codes |
| `jobs/audit/CONTRACT.md` | Phase 1000 runtime-kontrakt: 6 audit-sjekker, tier-policy (silent/lav/hoy/kritisk), atomic rapport-write, heartbeat-state semantikk. Spec-autoritet: `cortex/docs/contracts/audit-pass-spec.md` |
| `.gitignore` | Runtime-state ekskludert; secrets MÅ leve utenfor repo-treet |
| `docs/ARCHITECTURE.md` | Deploy-flyt, komponent-diagram, modul-ansvar |
| `CLAUDE.md` | Denne filen - Claude Code project-config |
