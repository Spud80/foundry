# Proxmox CT-spec: foundry

Forankrings-dokument for foundry-LXC. Beskriver baseline-spec, network-konvensjon, ressurs-allokering og oppfølgings-policy. Endringer i CT-kapasitet skal speiles her etter at de er gjort i Proxmox.

## Identitet

| Felt | Verdi |
|------|-------|
| VMID | `102` |
| Hostname | `foundry` |
| Tailscale FQDN | `foundry.tail8feda0.ts.net` |
| Host (Proxmox) | `thehub` |
| OS | Debian 12 (`debian-12-standard_12.12-1_amd64.tar.zst`) |
| Type | Unprivileged LXC |

## Ressurser (baseline, justeres etter måling)

| Ressurs | Verdi | Begrunnelse |
|---------|-------|-------------|
| Cores | `2` | Per [SPEC-foundry] Open Q1 startverdier; juster oppover hvis Claude CLI + Node.js + Syncthing presser ved bringup |
| Memory | `2048` MB | Samme som over; node + claude + syncthing er hovedforbrukere |
| Swap | `512` MB | Beskjeden (<25% av RAM); LXC-default-mønster |
| Rootfs | `local-lvm:20G` | Inkluderer system + per-jobb-venvs + log-historikk (12 ukers logrotate); juster etter empiri |

Mål forbruk under Phase 100 acceptance og oppdater dette dokumentet hvis grensene endres.

## Nettverk

| Felt | Verdi |
|------|-------|
| Bridge | `vmbr0` |
| IP | `10.0.0.52/24` |
| Gateway | `10.0.0.1` |
| Nameserver | `1.1.1.1 8.8.8.8` |
| Tailscale | `tailscale up && tailscale set --ssh` IKKE - kun `tailscale up` (Tailscale SSH server deferred per `~/.claude/rules/dev-infra.md`) |

IP `.52` valgt fordi `.51` er filehub (CT 101) og resten av subnet er enten gateway/router-utstyr eller eksisterende LAN-klienter (verifisert via ARP 2026-05-07).

## Tailscale TUN-device-eksponering

Unprivileged LXC krever eksplisitt eksponering av `/dev/net/tun` for at Tailscale-klienten skal fungere. Speilet fra filehub (CT 101) sin konfigurasjon:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file 0 0
```

Disse linjene må legges til i `/etc/pve/lxc/102.conf` etter `pct create` (kan ikke settes via `pct create`-flagg direkte).

## Andre flagg

| Felt | Verdi | Begrunnelse |
|------|-------|-------------|
| `unprivileged` | `1` | Default sikkerhets-posture; CAP_SYS_TIME blokkert (akseptert per Proxmox guest time policy) |
| `features` | `nesting=1` | Tillater systemd og container-internals; samme som filehub |
| `onboot` | `1` | Auto-start ved Proxmox-host reboot (cron-jobber må kjøre uten manuell intervensjon) |
| `ostype` | `debian` | Eksplisitt for at pct skal generere Debian-spesifikk init-config |

## pct create-kommando

```bash
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

Etter `pct create`, append TUN-device-eksponeringen til `/etc/pve/lxc/102.conf` (se seksjon over), deretter `sudo pct start 102`.

## Snapshot-policy

Foundry-CT Proxmox-snapshot ukentlig × 4 (rolling) per `[[SPEC-foundry]]` Key Decisions. Konfigurert separat på Proxmox-host (ikke en del av denne CT-spec-en).
