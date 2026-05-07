# MEMORY: foundry

Sesjons-overlevende status. Oppdateres ved sesjonsslutt eller PC-skifte.

## Current Status (2026-05-07)

- **Phase:** 0 done (0.1.0 scaffold). Phase 100 (LXC bringup) er neste, status `planning`.
- **Branches:** `dev` og `main` begge på remote. `dev` = `b3cf920`, `main` = `60e0c4d`. Working tree clean.
- **Deployerte jobber:** ingen. CT 102 er ikke opprettet enda.

## Recent Session (2026-05-07)

- Opprettet remote `dev`-branch og pushet lokal commit `b3cf920` (`docs: add CLAUDE.md and ARCHITECTURE.md project meta-docs`).
- Verifisert at session-start-hooken ikke lenger feiler på `git pull` (remote `dev` finnes nå).

## Next Steps

- Phase 100: skriv `bootstrap/setup-linux.sh` og `bootstrap/README.md` for idempotent CT-bringup på Debian 12 (VMID 102 på `thehub`). Se `bootstrap/proxmox-ct-config.md` for CT-spec.
- Phase 200: `auto-update.sh`, `deploy.sh`, watchdog-cron, `_shared/`, `cron.d/`.

## Key Decisions (snapshot fra CLAUDE.md - autoritativ kilde der)

- `main` er auto-deploy-branch: push -> live i CT 102 innen 15 min via `auto-update.sh`.
- Daglig arbeid på `dev`. Promotering til `main` skjer via `/release`-skillen.
- Repo er PUBLIC. Secrets MÅ leve utenfor repo-treet (`~/.config/foundry/secrets.env`, `~/.claude/.credentials.json`).
