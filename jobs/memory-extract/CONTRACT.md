# CONTRACT: foundry/jobs/memory-extract/

Leveranse-kontrakt mellom `[[SPEC-foundry]]` (runtime) og `[[SPEC-obsidian-memory]]` (payload).
Foundry leverer runtime, scheduling og pre-flight; obsidian-memory leverer Phase E payload-koden.

**Status:** Phase 600 importert 2026-05-08. extract.py + system-prompt.md + requirements.txt
levert mot denne kontrakten fra `dev-environment/scripts/memory/` (commit `8e2d89d`).

**Status (v2-reimport 2026-05-29):** extract.py reimportert fra `cortex/scripts/memory/`
(lift-and-shift-master, commit `0754017`) og oppdatert til cortex-memory-v2: `append_to_compiled_sources`
droppet (compile-pass eier sources i v2), `_aliases.py` + `_paths.py` lagt til som vendrede deps,
`_source_append.py` fjernet. Harness-tag-sanitizer (opprinnelig laget i denne foundry-kopien) er
back-portet til cortex-master og fulgte med reimporten - ingen sikkerhets-regresjon.

**Status (usage-spool-reimport 2026-06-11):** extract.py reimportert fra `cortex/scripts/memory/`
(commit `2f13813`): usage-spool for fleet/control-plane-metering (én JSONL-record per claude-kall
når `EXTRACT_USAGE_DIR` + `EXTRACT_RUN_CORRELATION` er satt; no-op ellers). run.sh fikk samtidig
CP-dispatch-kontrakten (`--cp-*` argv, se egen seksjon) og HOME-uavhengighet (USER_HOME fra passwd,
LOG_FILE fra REPO_ROOT) så den kan invokeres via `sudo runuser -u claude --` uten login-shell.

**Filformat-autoritet:** `cortex/docs/contracts/memory-knowledge-contract.md`
([GitHub](https://github.com/Spud80/cortex/blob/dev/docs/contracts/memory-knowledge-contract.md))
eier raw/-format, manifest-format, og extracted/-format inkludert `schema_version`. Foundry
CONTRACT.md (denne fila) eier kun runtime-grensesnittet.

## Leveranser obsidian-memory leverer

Følgende filer importeres til `~/foundry/jobs/memory-extract/` på CT:

| Fil | Type | Eier | Beskrivelse |
|-----|------|------|-------------|
| `extract.py` | Python 3.11+ | obsidian-memory | LLM-klassifisering av raw → typed extracted-entries; importerer `_aliases` + `_paths` (vendret ved siden av) |
| `_aliases.py` | Python 3.11+ | obsidian-memory | `load_aliases` + `AliasesError` (aliases.yaml-parsing for topic-normalisering); vendret dep for extract.py |
| `_paths.py` | Python 3.11+ | obsidian-memory | `resolve_vault_root` (CLI > env > platform-default); vendret dep for extract.py |
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
| `OBSIDIAN_VAULT_ROOT` | run.sh (default `${USER_HOME}/vault/My Vault`) | Nei (script har default, men foundry-default = filehub-path - så **må** settes på foundry) | Base-sti for raw/ + extracted/ + state-fil |
| `SYNCTHING_API_KEY` | secrets.env | Nei | Aktiverer sekundær Syncthing-pre-flight; soft-skip hvis fraværende |
| `EXTRACT_USAGE_DIR` | run.sh (spool-katalogen, når den finnes og er skrivbar) | Nei | Usage-spool: én JSONL-record per claude-kall for CP-metering; no-op når fraværende |
| `EXTRACT_RUN_CORRELATION` | run.sh (`--cp-correlation` eller cron-mintet) | Nei | Run-correlation; per-kall-nøkkel blir `<run>#<session-id>` (CP-ledger-dedup) |

extract.py må IKKE kreve andre env-vars uten å oppdatere denne kontrakten først.

### CLI-args (run.sh → extract.py)

run.sh tråder `--limit <N>` og `--max-budget-usd <X>` videre til extract.py når de er
injisert via `--cp-limit` / `--cp-max-budget-usd` (CP-dispatch). Uten CP-args kjører
extract.py som før (ingen limit, ingen per-kall-cap).

### CP-dispatch-kontrakt (fleet/control-plane delegated-headless)

CP invokerer run.sh direkte (`sudo runuser -u claude -- .../run.sh`) med argv - ikke env,
siden env ikke overlever sudo/runuser-grensen uten env_keep:

| Arg | Påkrevd? | Bruk |
|-----|----------|------|
| `--cp-correlation <id>` | Nei | Run-correlation fra CP. Fraværende -> cron-modus: run.sh minter `cron-<dato>-<hex>` så fallback-dager også metres |
| `--cp-usage-dir <dir>` | Nei | Spool-katalog (default `/var/lib/control-plane/usage-spool/memory-extract`). Finnes ikke / ikke skrivbar -> metering deaktivert denne runen (logget, aldri fatal) |
| `--cp-limit <N>` | Nei | Maks sesjoner per run (trådes til extract.py `--limit`) |
| `--cp-max-budget-usd <X>` | Nei | Per-kall-cap (trådes til extract.py `--max-budget-usd`) |

Ukjent argument -> exit 2 (FATAL, defensiv misconfig-guard). Spool-eierskap: claude-siden
(run.sh) pruner `*.jsonl` eldre enn 14 dager; CP sweeper kun (idempotent) og sletter aldri.
Exit-kodene under gjelder uendret i begge moduser; CP-lanen behandler enhver non-zero som
feil og metrer uansett fra spoolen (settle-actual).

### Exit-code-semantikk

run.sh propagerer extract.py sin exit-kode:

| Kode | Betydning | run.sh-håndtering |
|------|-----------|-------------------|
| 0 | OK - alle sesjoner prosessert, ingen pending, ELLER transient pre-flight-fail (manifest sync incomplete, Syncthing needFiles>0) | Logg, exit 0 stille; cron retrier neste dag |
| 1 | Per-session prosessering-feil (LLM-feil, parse-feil); state preservert, neste cron retrier de feilede | Notify Telegram "transient", exit 1 |
| 2 | Hard fatal (state-fil korrupt uten --force-flag, manglende system-prompt-file, mismatch state-vs-extracted) | Notify Telegram `(FATAL)`, exit 2; krever manuell intervensjon |
| 3 | (Utgått 2026-06-09) Tidligere: manifest sha256 mismatch aborterte hele runen. Nå degraderer extract.py per-dir (se nedenfor) og returnerer 0/1 | Emitteres ikke lenger; run.sh beholder en defensiv exit-3-gren som ikke nås |
| 124 | Timeout (`timeout 30m` killed extract.py) | Notify "TIMEOUT", behandles som transient |
| Andre | Behandles som fatal | Notify, exit som-er |

**Designvalg - graceful per-dir degradation (2026-06-09):**

Manifest-preflight aborterer ikke lenger hele runen ved problem i én date-dir.
`verify_manifests` klassifiserer hver date-dir som verified / mismatched / incomplete,
og kun verifiserte date-dirs prosesseres. De øvrige hoppes over:

- **Incomplete** (missing/unparseable manifest, missing file - capture/sync in flight):
  hoppes stille, retries neste cron. Unngår Telegram-spam ved Syncthing-lag.
- **Mismatched** (sha256 mismatch - persistent drift, typisk post-restore eller en
  ekstern/Syncthing-edit av en raw-fil): date-diren hoppes over, men extract.py sender
  en Telegram-notify ("preflight-degraded ... reconcile-manifest.py --apply") så operator
  rydder. Diren retries hver cron til den er reconciled.

Rene date-dirs prosesseres alltid, så én drivet historisk fil kan ikke lenger stoppe hele
pipelinen (50t-utfall 2026-06-06). Exit-koden følger prosesseringen (0 ved suksess, 1 ved
per-session-feil); **exit 3 emitteres ikke lenger**.

**Reconcile-verktøy:** `jobs/memory-extract/reconcile-manifest.py` (vendret fra
`cortex/scripts/memory/reconcile-manifest.py`; selvstendig - kun stdlib, ingen lokale
imports). Operator kjører det på foundry-CT etter en mismatched-notify:

```bash
~/foundry/jobs/memory-extract/.venv/bin/python \
  ~/foundry/jobs/memory-extract/reconcile-manifest.py \
  --vault-root "$HOME/vault/My Vault" --date <YYYY-MM-DD> --apply \
  --lock-file "$HOME/foundry/.deploy.lock"
```

`--lock-file` tar foundry deploy-locken for å unngå race mot cron-jobbene; `--date`
begrenser til den drevne date-diren (utelat for full-vault scan; dry-run er default uten
`--apply`).

Historikk: original exit-0-policy 2026-05-08; sha-mismatch exit-3-split 2026-05-16
(abort hele runen); erstattet av graceful per-dir degradation 2026-06-09.

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

## Aliases-konsumering (cross-plan-koordinering 2026-05-10)

Producer-side topic-normalisering for å løse fragmentering i `extracted/`-laget (nær-
identiske `¤topic`-tags som `¤claude-code-skills` vs `¤claude-skills`). extract.py
konsumerer `aliases.yaml` i to mekanismer:

1. **Canonical vocabulary-injection i system-prompt** - ved start leses kanoniske slugs
   fra aliases.yaml og injecter inn i `--append-system-prompt` som "preferred topic
   vocabulary" så LLM-en konvergerer på kilden, ikke i post-prosess.
2. **Alias→canonical post-prosess-mapping** - LLM-output-tags som matcher en alias-
   oppføring rewrites til canonical før heading-blokker skrives til `extracted/`. Safety-
   net når LLM ignorerer system-prompt-hint.

### Path og format

| Aspekt | Verdi |
|--------|-------|
| Path | `${OBSIDIAN_VAULT_ROOT}/8.Cortex/Memory/aliases.yaml` |
| Format | YAML (skjema-autoritet i `memory-knowledge-contract.md`) |
| Eier | obsidian-memory (write); foundry er konsument-only |
| Sync-kanal | Syncthing-folder `obsidian` (samme som vault-resten) |

### Felter foundry-extract leser

- `canonicals[<slug>].aliases[]` - liste av synonym-slugs som mappes til `<slug>`
- `canonicals[<slug>]` (slug i seg selv) - canonical vocabulary for system-prompt-injection

### Felter foundry-extract IKKE leser

- `tier` (signal/noise/archived)
- `tier_confidence` (low/medium/high)
- `tier_rationale`
- `description`

Disse konsumeres kun av obsidian-memory G3 detect og bruker-review-flow. Foundry
nøytralt-passer-gjennom alle entries uavhengig av tier.

### Fail-modes

| Tilstand | Foundry-respons |
|----------|-----------------|
| `aliases.yaml` mangler | Graceful degrade: extract kjører uten normalisering, WARNING via `_shared/notify` ("aliases.yaml ikke tilstede - kjører uten normalisering"), exit 0 |
| `aliases.yaml` finnes men er corrupt YAML | Hard fail: notify Telegram `(FATAL)`, exit 2. Korrupt config er sterkere signal enn manglende - krever manuell fix før neste cron |
| `aliases.yaml` har tom `canonicals: {}` | Behandles som "ingen aliases definert" - extract kjører uten normalisering, ingen WARNING (gyldig tilstand før bootstrap er kjørt) |
| Schema-version-mismatch (extract.py sin minimum > aliases.yaml sin schema_version) | Hard fail exit 2, samme begrunnelse som corrupt YAML |
| `aliases.yaml.sync-conflict-*` finnes i vault | Fanget av eksisterende run.sh pre-flight (`ssh filehub-cleanup --require-clean /data/sync/obsidian /data/sync/claude-memory`). Cleanup-scriptet quarantines conflict-fila som "ukjent type" til `/var/lib/syncthing-conflicts/<dato>/` og returnerer exit 1; run.sh aborter med Telegram-varsel før extract.py kalles. Ingen ekstra håndtering nødvendig i extract.py |

### Python-deps

YAML-parsing krever `pyyaml>=6.0` i `requirements.txt`. Tillegget importeres ved Phase 700-
import fra obsidian-memory; deploy.sh sin fingerprint-cache regenererer venv automatisk
ved requirements.txt-endring (Phase 500-mekanikk, ingen ny logikk nødvendig).

### Endringskontroll for aliases-konsumering

aliases.yaml sitt skjema (felter, type-enum, struktur) eies av obsidian-memory via
`memory-knowledge-contract.md`. Foundry abonnerer; bumps i skjema-version koordineres
mellom obsidian-memory og foundry før producer endrer. Foundry oppdaterer egen
`minimum-supported-schema-version` i `[[SPEC-foundry]]` ved breaking changes.

## Sources-append-konsumering (cross-plan-koordinering 2026-05-10, Phase 800)

Etter at en heading-blokk er skrevet til `extracted/<type>-YYYY-QN.md` gjør extract.py
en post-write pass mot `compiled/<canonical>.md`-filer per entry: for hver canonical-tag
i entry.topics (etter aliases-consumption alias-resolving) sjekker den om `compiled/<canonical>.md`
finnes; hvis ja, atomic-append'es `- [[<date>/<session-id>]] - <heading-slug>` under
`## Sources`-seksjonen. Auto-creates `## Sources`-seksjonen på EOF hvis den mangler.

Spec-autoritet: `cortex/docs/contracts/memory-knowledge-contract.md`
"Compiled-update protocol" → "Sources layer".

### Path og format

| Aspekt | Verdi |
|--------|-------|
| Path | `${OBSIDIAN_VAULT_ROOT}/8.Cortex/Memory/compiled/<canonical>.md` |
| Source-link-format | `- [[<YYYY-MM-DD>/<session-id>]] - <kebab-case-slug>` (`<date>/<session-id>` matcher source-raw-fila per heading-blokk-spec) |
| Eier | obsidian-memory deep-compile oppretter compiled/-filer; foundry-extract bare appender source-links til `## Sources` |
| Sync-kanal | Syncthing-folder `obsidian` (samme som vault-resten) |

### Idempotency-garanti

extract.py sjekker om source-link allerede er til stede i `compiled/<canonical>.md`
før append (substring-match). Re-runs på samme session produserer aldri duplikat-
source-links. Dette gjelder også cross-cron-grensen: hvis en session re-prosesseres
manuelt etter state-fil-rebuild, vil source-links ikke duplikere.

### Atomic-write-mekanikk

POSIX (production foundry-Linux):
- Sidecar lock-fil: `<dir>/.<filename>.lock`
- `fcntl.flock(LOCK_EX)` på lock-fila før read-modify-write
- Data-fil skrives via `atomic_write()` (temp + `os.replace`-rename) under lock-hold
- Lock slippes etter rename
- Concurrent extract.py-prosesser serialiseres trygt; loser-write's content er
  preservert via lock-await + re-read

Windows (lokale smoke-tests):
- `fcntl` ikke tilgjengelig → lock er best-effort no-op
- Production-environment er Linux, så dette gjelder ikke cron-runtime
- Smoke-test `_test/smoke_k5_k6.py` verifiserer atomicity strict på POSIX,
  best-effort på Windows

### Fail-modes (Sources-append)

| Tilstand | Foundry-respons |
|----------|-----------------|
| `compiled/<canonical>.md` finnes ikke | Status `missing`, no-op (extracted/ er ground-truth - obsidian-memory deep-compile oppretter compiled-filer separat ved threshold) |
| `compiled/<canonical>.md` finnes, source-link allerede til stede | Status `already-present`, idempotent skip |
| Transient I/O-feil ved lock/read/write | Status `error: <msg>` logges til stderr; entry-prosessering fortsetter (extracted/-skrivingen er allerede committet, extracted/ er ground-truth) |
| `## Sources`-seksjonen mangler i en eksisterende compiled-fil | Seksjonen auto-creates på EOF og source-link skrives inn |

### Endringskontroll for Sources-append

Source-link-format og atomic-append-semantikk eies av obsidian-memory via
`memory-knowledge-contract.md` "Compiled-update protocol" → "Sources layer".
Foundry abonnerer; format-bumps koordineres samme som schema_version-bumps.

## Endringskontroll

* Endringer i runtime-grensesnittet (env-vars, exit-codes, pre-flight-rekkefølge) krever
  pull-request mot DENNE fila + koordinering med obsidian-memory.
* Endringer i filformat (raw/, manifest, extracted/, schema_version) eier obsidian-memory
  via `cortex/docs/contracts/memory-knowledge-contract.md`. Foundry reagerer kun
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
* `cortex/docs/contracts/memory-knowledge-contract.md` - filformat-autoritet
  (raw/, manifest, extracted/, schema_version)
