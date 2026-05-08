# foundry

Lightweight Linux-container (LXC) for headless cron-baserte vault-automation-jobber.

Substrate for batch-jobber som ikke hører hjemme i andre apps-VMer (knowledge-engine, argus-bridge, vault-sentinel) - typisk: lette LLM-passes via Claude Code subscription, vault-lint, link-checks, snapshot-triggers.

## Dokumentasjon

- **SPEC og PLAN:** Obsidian-vault `7.Projects/Dev/sync-infra/foundry/` (privat, ikke i dette repoet)
- **15-min bootstrap-runbook:** [`bootstrap/README.md`](bootstrap/README.md)
- **CT-spec (Proxmox):** [`bootstrap/proxmox-ct-config.md`](bootstrap/proxmox-ct-config.md)
- **Disaster recovery:** se `bootstrap/README.md` "DR-prosedyre"-seksjon

## Repo-layout

```
bootstrap/             Idempotent CT-bringup (setup-linux.sh, watchdog, logrotate, runbook)
cron.d/                Crontab-fragmenter (én per jobb), regenereres av deploy.sh
_shared/               Felles helpers (notify-core, notify, drain-queue, token-expiry-check)
jobs/<jobname>/        Per-jobb subfolders med run.sh, requirements.txt, CONTRACT.md
.github/workflows/     CI (shellcheck + cron-syntax + deploy-dry-run + notify-paritet)
deploy.sh              Regenererer claude-bruker-crontab atomisk fra cron.d/
auto-update.sh         Defensiv git-flow (fetch + reset --hard origin/$FOUNDRY_BRANCH + deploy.sh)
```

## Auto-deploy

`auto-update.sh` cron `*/15 * * * *` puller fra `origin/$FOUNDRY_BRANCH` (default `dev` til pipeline er stabil; flippes til `main` senere) og kjører `deploy.sh` ved endringer. Push til den aktive branchen lander i produksjon innen 15 min.

Watchdog-cron (`/etc/cron.d/foundry-watchdog`, eier root, kjører timesvis) varsler via Telegram hvis auto-update-loggen mangler OK-linje > 45 min. Watchdog er bevisst uavhengig av `_shared/notify.sh` slik at en feil som broker auto-update også broker varslings-pipelinen.

## Auth (headless)

Foundry kjører `claude -p` headless via `CLAUDE_CODE_OAUTH_TOKEN` env-var (~1 års levetid; generert via `claude setup-token`). Token deployes til `~/.config/foundry/secrets.env` på CT. Alternativ `~/.claude/.credentials.json` (subscription-login) er IKKE i bruk - refresh-token utløper hvis ikke aktivt brukt og er upålitelig for batch-CT.

`_shared/token-expiry-check.sh` kjører daglig 09:00 norsk tid og varsler 30 dager før 1-års-mark.

## Public repo, ingen secrets

Public per design. Secrets (OAuth-token, Telegram bot-token, Syncthing API-key) lever i `~/.config/foundry/secrets.env` (mode 600, claude-eid) - aldri i repo-treet. Pipeline-arkitekturen er ikke-sensitiv. Akseptert risiko: GitHub-konto-kompromittering mitigert med 2FA.
