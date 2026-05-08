# CONTRACT: foundry/jobs/memory-extract/

Leveranse-kontrakt mellom `[[SPEC-foundry]]` (runtime) og `[[SPEC-obsidian-memory]]` (payload).
Foundry leverer runtime, scheduling og pre-flight; obsidian-memory leverer Phase E payload-koden.

**Status:** Phase 600 importert 2026-05-08. extract.py + system-prompt.md + requirements.txt
levert mot denne kontrakten fra `dev-environment/scripts/memory/` (commit `8e2d89d`).

**Filformat-autoritet:** `dev-environment/docs/reference/memory-knowledge-contract.md`
([GitHub](https://github.com/Spud80/dev-environment/blob/main/docs/reference/memory-knowledge-contract.md))
eier raw/-format, manifest-format, og extracted/-format inkludert `schema_version`. Foundry
CONTRACT.md (denne fila) eier kun runtime-grensesnittet.

## Leveranser obsidian-memory leverer

Følgende filer importeres til `~/foundry/jobs/memory-extract/` på CT:

| Fil | Type | Eier | Beskrivelse |
|-----|------|------|-------------|
| `extract.py` | Python 3.11+ | obsidian-memory | LLM-klassifisering av raw → typed extracted-entries; standalone, ingen kryss-import |
| `system-prompt.md` | Markdown | obsidian-memory | `--system-prompt`-content for `claude -p` (override av default Claude Code system prompt) |
| `requirements.txt` | Python deps | obsidian-memory | `requests>=2.31.0` (kun for optional Syncthing REST-pre-flight); deploy.sh setter opp `.venv/` |

`run.sh` (eier: foundry) source'er secrets.env, kjører pre-flight, og kaller
`.venv/bin/python extract.py`. Payload-guard exit'er stille hvis `extract.py` mangler.

## Runtime-grensesnitt

### Miljøvariabler (run.sh → extract.py)

run.sh source'er `~/.config/foundry/secrets.env` (set -a) og eksporterer i tillegg
`OBSIDIAN_VAULT_ROOT`. Følgende leses av extract.py:

| Variabel | Kilde | Påkrevd? | Bruk |
|----------|-------|----------|------|
| `CLAUDE_CODE_OAUTH_TOKEN` | secrets.env | Ja | Authorization for `claude -p`-kall |
| `OBSIDIAN_VAULT_ROOT` | run.sh (default `${HOME}/vault/My Vault`) | Nei (script har default, men foundry-default = filehub-path - så **må** settes på foundry) | Base-sti for raw/ + extracted/ + state-fil |
| `SYNCTHING_API_KEY` | secrets.env | Nei | Aktiverer sekundær Syncthing-pre-flight; soft-skip hvis fraværende |

extract.py må IKKE kreve andre env-vars uten å oppdatere denne kontrakten først.

### Exit-code-semantikk

run.sh propagerer extract.py sin exit-kode:

| Kode | Betydning | run.sh-håndtering |
|------|-----------|-------------------|
| 0 | OK - alle sesjoner prosessert, ingen pending, ELLER transient pre-flight-fail (manifest sync incomplete, Syncthing needFiles>0) | Logg, exit 0 stille; cron retrier neste dag |
| 1 | Per-session prosessering-feil (LLM-feil, parse-feil); state preservert, neste cron retrier de feilede | Notify Telegram "transient", exit 1 |
| 2 | Hard fatal (state-fil korrupt uten --force-flag, manglende system-prompt-file, mismatch state-vs-extracted) | Notify Telegram `(FATAL)`, exit 2; krever manuell intervensjon |
| 124 | Timeout (`timeout 30m` killed extract.py) | Notify "TIMEOUT", behandles som transient |
| Andre | Behandles som fatal | Notify, exit som-er |

**Designvalg som avviker fra tidligere CONTRACT.md-versjon:** preflight-fail (manifest
mismatch eller Syncthing incomplete) returnerer **exit 0 silent**, ikke exit 1. Begrunnelse:
unngår Telegram-spam ved Syncthing-lag (vanlig, transient situasjon). Eksplisitt avvik
akseptert av foundry-sesjonen 2026-05-08.

### Forventet kjøretid

* **Normal kjøring:** < 5 min for 1-3 nye raw-filer per dag.
* **Hard timeout i run.sh:** 30 min (`timeout 30m`).
* Lengre enn 30 min: drepes hardt, exit 124, behandles som transient.

## Pre-flight (foundry-eid, før extract.py kalles)

run.sh kjører følgende sjekker FØR extract.py:

1. **Payload-guard:** `[ -f extract.py ] || { notify "payload not deployed"; exit 0; }`.
   Lar inaktive jobber coexiste med aktiv cron uten daglige feil.
2. **Source secrets.env** (FATAL hvis mangler eller `CLAUDE_CODE_OAUTH_TOKEN` ikke satt).
3. **`flock --nonblock ~/foundry/.deploy.lock`** - serialiserer mot auto-update.sh og
   andre jobber.
4. **`ssh filehub-cleanup <PATH+>`** - kjører `sync-conflict-cleanup.py --require-clean <PATH+>`
   på filehub-siden via login-shell-wrapper (`/usr/local/bin/foundry-cleanup-login-shell`)
   som auto-prepender `--require-clean`. Semantikk:
   - Cleanup-scriptet skanner **alltid hele `/data/sync`** uavhengig av path-arg (idempotent
     full-scan: identical-to-canonical-konflikter slettes, divergent quarantineres til
     `/var/lib/syncthing-conflicts/<dato>/...` og rapporteres til vault-rapport).
   - `--require-clean <PATH+>` aktiverer **exit-code-gate**: returnerer exit 1 hvis
     én eller flere quarantines lander under noen av `<PATH+>`; ellers exit 0.
   - Foundry sender `${FILEHUB_CLEAN_SCOPE}` (default
     `/data/sync/obsidian /data/sync/claude-memory`) - paths uten mellomrom, word-splittes
     av wrapper.
   - run.sh exit 1 ved cleanup-exit 1 (transient; cron prøver igjen neste dag).

extract.py kjører deretter sin egen interne pre-flight (manifest-handshake + Syncthing
REST-API). Glob-assert og raw-katalog-sjekk er IKKE i run.sh - extract.py håndterer
dette internt og returnerer exit 0 stille hvis raw/ er tom.

**Wrapper-kontrakt:** filehub-cleanup-wrapper aksepterer kun path-args (ingen flagg som
`--dry-run` eller `--root` kan komme gjennom). Endringer i wrapper-grensesnittet eier
device-sync-and-backup.

## Endringskontroll

* Endringer i runtime-grensesnittet (env-vars, exit-codes, pre-flight-rekkefølge) krever
  pull-request mot DENNE fila + koordinering med obsidian-memory.
* Endringer i filformat (raw/, manifest, extracted/, schema_version) eier obsidian-memory
  via `dev-environment/docs/reference/memory-knowledge-contract.md`. Foundry reagerer kun
  hvis runtime-grensesnittet endres som følge.
* Foundry kan endre run.sh internals (lock-mekanikk, timeout-verdi, notify-format,
  pre-flight-detaljer) uten kontrakts-endring så lenge env-vars og exit-code-mapping
  bevares.
* obsidian-memory kan endre extract.py intern logikk fritt så lenge env-var-kontrakten,
  exit-code-semantikken og runtime-pre-flight-antagelsene respekteres.

## Referanser

* `[[SPEC-foundry]]` - runtime-arkitektur, deploy-pipeline, watchdog
* `[[SPEC-obsidian-memory]]` Phase E - extract-payload-leveranse, 6-type ontologi
* `[[SPEC-device-sync-and-backup]]` - filehub-cleanup-bridge for pre-flight
* `dev-environment/docs/reference/memory-knowledge-contract.md` - filformat-autoritet
  (raw/, manifest, extracted/, schema_version)
