# Foundry bootstrap-runbook

15-min manuell SSH-runbook for å reise foundry-CT fra null til kjørende runtime, samt disaster-recovery-prosedyrer.

> **Autoritativ doc:** SPEC og PLAN ligger i Obsidian-vaulten under `7.Projects/Dev/sync-infra/foundry/`. Denne fila er repo-lokal operasjons-doc.

## Forutsetninger

| Krav | Hvor sjekkes |
|------|--------------|
| Proxmox-host `thehub` tilgjengelig via SSH | `ssh thehub "uptime"` |
| Debian 12-template lastet ned på thehub | `ls /var/lib/vz/template/cache/debian-12-standard*.tar.zst` på thehub |
| Tailscale tailnet (`tail8feda0.ts.net`) medlemskap fra dev-PC | `tailscale status` |
| GitHub-konto med tilgang til `Spud80/foundry` | `gh repo view Spud80/foundry` |
| 1Password-vault med Telegram-bot-token og Claude credentials | manuell |

## 15-min bringup-runbook (ny CT)

Tidsestimat: 12-15 min ved kjørbare nett-forhold. Forutsetter at CT-spec ([`proxmox-ct-config.md`](proxmox-ct-config.md)) ikke har endret seg siden forrige bringup.

### Trinn 1: Provision LXC på thehub (~3 min)

```bash
ssh thehub
sudo pct create 102 \
  /var/lib/vz/template/cache/debian-12-standard_12.12-1_amd64.tar.zst \
  --hostname foundry \
  --ostype debian \
  --unprivileged 1 \
  --features nesting=1 \
  --onboot 1 \
  --cores 2 \
  --memory 2048 \
  --swap 512 \
  --rootfs local-lvm:20 \
  --net0 name=eth0,bridge=vmbr0,ip=10.0.0.52/24,gw=10.0.0.1,type=veth \
  --nameserver "1.1.1.1 8.8.8.8"
```

Append TUN-device-eksponering til `/etc/pve/lxc/102.conf`:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file 0 0
```

Start CT-en: `sudo pct start 102`.

### Trinn 2: CT-internt grunnoppsett (~5 min)

Kjør fra thehub via `pct exec`:

```bash
sudo pct exec 102 -- bash -c '
set -e
apt-get update
apt-get install -y curl sudo ca-certificates git jq
useradd -m -s /bin/bash claude
echo "claude ALL=(root) NOPASSWD: ALL" > /etc/sudoers.d/claude
chmod 440 /etc/sudoers.d/claude
visudo -c -f /etc/sudoers.d/claude
passwd -l claude
timedatectl set-timezone Europe/Oslo
systemctl mask systemd-timesyncd
'
```

Per [Proxmox guest time policy](../docs/) (`~/.claude/rules/dev-infra.md`): timesyncd masket på unprivileged LXC fordi CAP_SYS_TIME er blokkert uansett; host-klokka arves direkte.

### Trinn 3: Tailscale på CT (~2 min)

```bash
sudo pct exec 102 -- bash -c '
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh
'
```

Browser-flow trigges på dev-PC for device-auth - godkjenn `foundry`-noden i Tailscale-admin-konsollen. Etter godkjenning: `tailscale set --ssh` allerede aktivert via `up --ssh`.

Verifiser fra dev-PC: `ssh foundry "uname -a"` (Tailscale SSH-modell, auth=none over tailnet).

### Trinn 4: Klon repo og kjør setup-linux.sh (~3 min)

Fra dev-PC, alt heretter via `ssh foundry`:

```bash
ssh foundry "git clone https://github.com/Spud80/foundry.git ~/foundry && \
  cd ~/foundry && ./bootstrap/setup-linux.sh"
```

> **Repo og runtime deler rot:** Repoet klones til `~/foundry/`. Runtime-state (`~/foundry/logs/`, `~/foundry/queue/`, `~/foundry/.deploy.lock`, `jobs/*/.venv/`) lever inne i repoet men er gitignored, slik at `git reset --hard origin/main` (auto-update */15) ikke blåser dem bort. Se `.gitignore` på repo-rot (kommer i Phase 500) for full liste.

setup-linux.sh installerer Node.js, claude CLI, og deployer:
- `~/.config/foundry/notify-core.sh` + `watchdog-notify.sh`
- `/etc/cron.d/foundry-watchdog` (root-eid)
- `/etc/logrotate.d/foundry` (root-eid)

### Trinn 5: Secrets-deploy (Phase 300, ~2 min)

> Kun applicable etter Phase 300 er ferdig. Dokumenteres her for fullstendig DR-runbook.

Foundry bruker OAuth-token via `CLAUDE_CODE_OAUTH_TOKEN` env-var (ikke `~/.claude/.credentials.json`). Begrunnelse: credentials.json holder kun timer/dager før refresh-token utloper hvis ikke aktivt brukt; OAuth-token har ~1 ars levetid og er eksplisitt designet for automation.

```bash
# 1. Pa spartan: generer OAuth-token (vises kun en gang - lagre umiddelbart i 1Password)
claude setup-token

# 2. Pa dev-PC: skriv secrets.env til foundry via heredoc (verdier passerer kun via SSH-tunnel)
ssh foundry 'cat > ~/.config/foundry/secrets.env << "EOF"
TELEGRAM_BOT_TOKEN=<bot-token fra 1Password>
TELEGRAM_CHAT_ID=<chat-id fra 1Password>
CLAUDE_CODE_OAUTH_TOKEN=<oauth-token fra 1Password>
CLAUDE_TOKEN_CREATED=YYYY-MM-DD
EOF
chmod 600 ~/.config/foundry/secrets.env'

# 3. Verifiser headless-auth (source secrets, deretter claude -p)
ssh foundry "set -a; source ~/.config/foundry/secrets.env; set +a; claude -p 'reply with the word OK'"
# forvent: OK
```

`CLAUDE_TOKEN_CREATED` er datoen tokenen ble generert (YYYY-MM-DD). Brukes av `_shared/token-expiry-check.sh` for daglig dato-basert utlops-varsling 30 dager for 1-ars-mark.

### Trinn 6: Deploy-pipeline (Phase 500)

> Phase 500-leveranser. `auto-update.sh */15` puller endringer fra `main` og kjører `deploy.sh` for å regenerere system-crontab fra `cron.d/`-fragmenter. Dokumenteres her når Phase 500 er implementert.

## Disaster Recovery

### CT-rollback (Proxmox snapshot)

Ukentlig × 4 rolling Proxmox-snapshots per [SPEC-foundry] retention-policy. Liste tilgjengelige snapshots:

```bash
ssh thehub "sudo pct listsnapshot 102"
```

Rollback til siste good snapshot:

```bash
ssh thehub "sudo pct rollback 102 <snapshot-name>"
```

CT-en stoppes, rolles tilbake, og kan startes igjen med `sudo pct start 102`. **Effekt:** alle endringer etter snapshot-tidspunkt er borte (inkludert auto-update'ede commits, queued Telegram-meldinger, ferske logs). Akseptabelt fordi:
- Foundry har ingen unik tilstand (synket vault er master, jobber kan re-kjøres)
- Memory-extract kjører daglig 18:30 - tap av <24t batch-output er gjenopprettelig
- Secrets (`secrets.env` med OAuth-token og Telegram-token) bevares (lever i hjemmemappa, snapshottet sammen)

### Host-død (thehub utilgjengelig)

Foundry-CT lever på thehub. Hvis thehub dør, må CT-en gjenopprettes fra ekstern backup eller bygges fra grunnen.

**Variant A: thehub kommer tilbake (transient feil).**
1. Vent på host-recovery
2. CT auto-starter via `--onboot 1`
3. auto-update-cron puller siste main innen 15 min
4. Watchdog-cron varsler hvis ikke

**Variant B: permanent host-tap, gjenoppbygg fra null.**
1. Provision ny Proxmox-host (utenfor scope for denne runbook)
2. Kjør Trinn 1-4 over på den nye hosten (~12-15 min)
3. Kjør Trinn 5 (secrets-deploy fra 1Password og spartan) (~2 min)
4. Total RTO: ~20 min fra ny host er klar

**Datatap-vurdering:** Foundry holder ingen unik tilstand. Vault leveres via Syncthing fra filehub (hub-modell, separat DR). Memory-extract output skrives tilbake til synket vault, ikke i foundry-CT. Altså: full CT-tap = 0 datatap, kun midlertidig avbrudd i daglig 18:30-cron.

### Vault-restore

Foundry er en downstream consumer av vault via Syncthing (Phase 400). Vault-DR håndteres av filehub som hub og dev-PC-er som peers. Foundry-spesifikk DR-handling: ingen. Når Syncthing er konfigurert (Phase 400), re-syncer foundry vault automatisk når den kommer online.

## Token-rotation

CLAUDE_CODE_OAUTH_TOKEN har ~1 ars levetid. Daglig `_shared/token-expiry-check.sh` (cron 09:00 norsk tid, aktivert av Phase 500 deploy.sh) sender Telegram-varsel nar 30 dager gjenstar til utlop.

### Rotation-prosedyre (~3 min)

```bash
# 1. Pa spartan: generer ny OAuth-token (vises kun en gang!)
claude setup-token
# Kopier output-tokenen umiddelbart til 1Password "Infrastructure"-vault entry "claude-foundry-oauth-token"

# 2. Pa dev-PC: oppdater 2 linjer i secrets.env pa foundry
#    (CLAUDE_CODE_OAUTH_TOKEN + CLAUDE_TOKEN_CREATED)
ssh foundry 'cat > ~/.config/foundry/secrets.env << "EOF"
TELEGRAM_BOT_TOKEN=<eksisterende verdi fra 1Password>
TELEGRAM_CHAT_ID=<eksisterende verdi fra 1Password>
CLAUDE_CODE_OAUTH_TOKEN=<ny token fra 1Password>
CLAUDE_TOKEN_CREATED=YYYY-MM-DD
EOF
chmod 600 ~/.config/foundry/secrets.env'

# 3. Verifiser
ssh foundry "set -a; source ~/.config/foundry/secrets.env; set +a; claude -p 'reply OK'"
# forvent: OK
```

### Hvorfor OAuth-token og ikke credentials.json

`~/.claude/.credentials.json` brukes av interaktiv `claude`-login og inneholder en kort-levetid access-token + refresh-token. Refresh-tokenet utloper hvis ikke aktivt brukt i noen dager. For en CT som kjorer batch-cron daglig (eller sjeldnere), er det ikke palitelig.

`claude setup-token` produserer en separat long-lived OAuth-token (~1 ar) som ikke trenger refresh. Eksplisitt designet for automation. Levert via `CLAUDE_CODE_OAUTH_TOKEN` env-var.

### Hvis token utloper for rotation

`token-expiry-check.sh` sender alarm i tre stadier:
- 30 dager igjen: "Forbered rotation"
- 0 dager: "Utloper i dag"
- < 0 dager: "UTLOPT for X dager siden - foundry-jobber feiler na pa auth"

Hvis siste tilfelle skjer: `auto-update.sh` vil fortsette a virke (krever ikke claude-auth), men jobber som kaller `claude -p` (memory-extract Phase 600) feiler til ny token er deployet. Watchdog-cron varsler ogsa fordi `auto-update.log` fortsetter a vise OK uavhengig av jobb-feil - sjekk Telegram for jobb-spesifikke alarm i tillegg.

## Acceptance-test for setup-linux.sh

Etter trinn 4, bekreft Phase 200 acceptance:

```bash
# 1. Idempotens: andre kjoring uten endringer skal vaere no-op
ssh foundry "cd ~/foundry && git status"  # forvent: clean
ssh foundry "cd ~/foundry && ./bootstrap/setup-linux.sh"  # forvent: 'already installed' for node + claude

# 2. Filer pa plass
ssh foundry "ls -la ~/.config/foundry/"
# forvent: notify-core.sh + watchdog-notify.sh, mode 0755, eier claude

ssh foundry "sudo ls -la /etc/cron.d/foundry-watchdog /etc/logrotate.d/foundry"
# forvent: begge eier root, mode 0644

# 3. watchdog-notify fungerer uten ~/foundry/-tre
ssh foundry "mv ~/foundry ~/foundry.bak; \
             ~/.config/foundry/watchdog-notify.sh 'foundry: smoke test fra DR-runbook'; \
             mv ~/foundry.bak ~/foundry"
# forvent (etter Phase 300 secrets): Telegram-melding mottatt
# forvent (uten secrets): script kjorer, melding havner i queue uten leveranse
```
