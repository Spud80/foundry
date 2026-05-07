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
bootstrap/      Idempotent CT-bringup (setup-linux.sh, watchdog, logrotate, runbook)
cron.d/         Crontab-fragmenter (én per jobb), regenereres av deploy.sh
_shared/        Felles helpers (notify, drain-queue)
jobs/           Per-jobb subfolders med run.sh, requirements.txt, etc.
ci/             GitHub Actions (lint + smoke-tests)
deploy.sh       Regenererer system-crontab atomisk fra cron.d/
auto-update.sh  Defensiv git-flow (fetch + reset --hard origin/main + deploy.sh)
```

## Auto-deploy

Push til `main` -> auto-update.sh cron `*/15 * * * *` puller endringen og kjører deploy.sh innen 15 min. Watchdog-cron (`/etc/cron.d/foundry-watchdog`, eier root) varsler via Telegram hvis auto-update-loggen mangler OK-linje > 45 min.

## Public repo, ingen secrets

Public per design. Secrets (`~/.config/foundry/secrets.env`, `~/.claude/.credentials.json`) lever utenfor repo-treet. Pipeline-arkitekturen er ikke-sensitiv. Akseptert risiko-rad i SPEC dokumenterer GitHub-konto-kompromittering med 2FA som mitigering.
