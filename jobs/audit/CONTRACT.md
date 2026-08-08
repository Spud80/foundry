# CONTRACT: foundry/jobs/audit/

Leveranse-kontrakt mellom `[[SPEC-foundry]]` (runtime) og
`cortex/docs/contracts/audit-pass-spec.md` (audit-spec, eid av
vault-sentinel).

Foundry-audit-cron kjorer nattlig 04:00 norsk lokal-tid, scanner vault for
schema-drift, dedup-kollisjoner, broken wikilinks, klassifiserings-divergens,
tag-inkonsistens og manglende required-felter, skriver rapport-fil til
`5.Utility/Pipeline/Audit-Reports/YYYY-MM-DD.md`, og emittert tiered
Telegram-varsel.

**Status:** Phase 1000 (PLAN-foundry) - strukturell deploy uavhengig av
inntakt vault-tilstand; reell ende-til-ende-test mot syntetisk audit-tilstand
verifisert via smoke-test, organisk steady-state-verifisering gates pa
forste 04:00-cron-fyring i produksjon.

**Spec-autoritet:** `cortex/docs/contracts/audit-pass-spec.md`
([GitHub](https://github.com/Spud80/cortex/blob/dev/docs/contracts/audit-pass-spec.md))
eier audit-sjekkene, output-format, tier-policy og runner-grensesnittet.
Foundry CONTRACT.md (denne fila) eier kun foundry-side runtime-detaljer
(paths, secrets, venv).

Minimum-supported-schema-version: 1 (per `audit-pass-spec.md` "Consumer declarations").

## Hva foundry-audit gjor

Per cron-run (04:00 norsk lokal-tid):

1. **Scan vault** for entries i `1.Inbox/`, `2.Resources/Notes/**/`, og
   raw-korpuset `<raw-root>/**/` (kun lese-tilgang). `<raw-root>` er
   `$CORTEX_RAW_ROOT` nar satt, ellers `<vault>/8.Cortex/Memory/raw/`.
2. **Kjor 6 audit-sjekker** sekvensielt per `audit-pass-spec.md`:
   - Sjekk 1: dedup-verification (full-vault)
   - Sjekk 2: schema-compliance
   - Sjekk 3: wikilink-validation
   - Sjekk 4: sampling-classification (LLM-required, kun denne sjekken)
   - Sjekk 5: tag-consistency
   - Sjekk 6: missing-required-field
3. **Aggreger funn** til tier-klassifisering (silent / lav / hoy / kritisk).
4. **Skriv rapport-fil** atomisk til
   `${OBSIDIAN_VAULT_ROOT}/5.Utility/Pipeline/Audit-Reports/YYYY-MM-DD.md`
   via tempfile + `os.replace`.
5. **Skriv heartbeat-state** atomisk til `~/.audit-state.json` (utenfor vault).
6. **Notify Telegram** ved tier >= hoy (per spec tier-policy).

Audit er strikt **read-only** over vault-content. Eneste skriving:
rapport-fil (i vault) + heartbeat-state (lokalt). Audit endrer ikke
audited entries.

## Leveranser foundry leverer

Folgende filer er foundry-eid (ingen ekstern payload-leveranse - dette er
en selvstendig jobb):

| Fil | Type | Beskrivelse |
|-----|------|-------------|
| `run.sh` | Bash | Orkestrering: secrets, flock, payload-guard, kaller audit.py |
| `audit.py` | Python 3.11+ | 6 audit-sjekker, rapport-generering, tier-aggregering, atomic write |
| `system-prompt.md` | Markdown | `--system-prompt`-content for `claude -p` (kun sjekk 4 sampling-classification) |
| `requirements.txt` | Python deps | `pyyaml>=6.0` (frontmatter-parsing) |

`deploy.sh` setter opp `.venv/` per `requirements.txt` (samme mekanikk som
memory-extract og fallback-classifier).

## Runtime-grensesnitt

### Miljovariabler (run.sh -> audit.py)

run.sh source'r `~/.config/foundry/secrets.env` (set -a) og eksporterer i
tillegg `OBSIDIAN_VAULT_ROOT`:

| Variabel | Kilde | Pakrevd? | Bruk |
|----------|-------|----------|------|
| `CLAUDE_CODE_OAUTH_TOKEN` | secrets.env | Ja | Authorization for `claude -p`-kall (kun sjekk 4) |
| `OBSIDIAN_VAULT_ROOT` | run.sh (default `${HOME}/vault/My Vault`) | Ja pa foundry CT | Base-sti for vault-scan |
| `FOUNDRY_NOTIFY_SH` | run.sh (default `${REPO_ROOT}/_shared/notify.sh`) | Nei | Lar audit.py eskalere tiered Telegram-varsel |
| `AUDIT_CLAUDE_TIMEOUT_SECONDS` | run.sh (default `300`) | Nei | Per-fil `claude -p`-timeout for sjekk 4 |
| `AUDIT_STATE_FILE` | run.sh (default `${HOME}/.audit-state.json`) | Nei | Override heartbeat-state path (for smoke-test) |

audit.py ma IKKE kreve andre env-vars uten a oppdatere denne kontrakten.

### Exit-code-semantikk

Per `audit-pass-spec.md` "Exit codes":

| Kode | Betydning | run.sh-handtering |
|------|-----------|-------------------|
| 0 | Suksess (med eller uten findings) | Logg, exit 0 stille |
| 1 | Uventet runtime-feil (caught exception, fallthrough) | Notify Telegram `(FATAL)`, exit 1 |
| 2 | Schema-mismatch pa audit-runner config (spec drifted) | Notify Telegram `(FATAL)`, exit 2 |
| 124 | Vault- eller raw-korpus utilgjengelig (Syncthing-mount mangler) | Notify "TIMEOUT", "vault unavailable" eller "raw corpus unavailable", exit 124 |
| Andre | Behandles som fatal | Notify, exit som-er |

Tier-policy er separat fra exit-code:

- Exit 0 + funn med tier hoy/kritisk -> Telegram-melding (audit funnet noe)
- Exit 1/2/124 -> Telegram-melding (audit kunne ikke fullfore)

Exit 124 er ikke-fatal pa forste forekomst (transient Syncthing-issue);
eskaler til Telegram kritisk-tier kun etter `consecutive_failed_runs >= 2`
via heartbeat-state.

### Forventet kjoretid

- **Normal kjoring:** typisk 1-5 min totalt:
  - Sjekk 1-3, 5, 6: deterministisk, ~30-60 sek for full-vault scan
  - Sjekk 4 (LLM): 5-15 `claude -p`-call (10% av ~50-150 fresh entries),
    ~30 sek per call -> 2-7 min worst-case
- **Hard timeout:** 20 min via `flock` policy (run.sh kjor under
  `.deploy.lock`). audit.py setter ikke egen total-timeout - reliances
  pa flock cap.
- **LLM-call hard cap:** 50 `claude -p`-call per audit-run (audit.py).

## Pre-flight (foundry-eid, for audit.py kalles)

run.sh kjorer folgende sjekker FOR audit.py:

1. **Payload-guard:** `[ -f audit.py -a -f system-prompt.md ] || { notify "payload not deployed"; exit 0; }`.
   Lar inaktive jobber coexiste med aktiv cron uten daglige feil.
2. **Source secrets.env** (FATAL hvis mangler eller `CLAUDE_CODE_OAUTH_TOKEN`
   ikke satt - kreves for sjekk 4 LLM-call).
3. **`flock --nonblock ~/foundry/.deploy.lock`** - serialiserer mot
   auto-update.sh, memory-extract, og fallback-classifier. Hvis lock ikke
   kan akkvireres innen 5 min, exit 0 stille (rapport produseres ikke
   denne natta; neste cron tar over).

Foundry-audit kaller IKKE `ssh filehub-cleanup` per design. Begrunnelse:
audit er strikt read-only over vault, sa eventuelle sync-conflict-filer i
`1.Inbox/` plukkes ikke opp (audit-glob ekskluderer `*.sync-conflict-*`
ved navn). Cleanup-ansvaret ligger pa memory-extract sin 18:30-pre-flight.

## Input-format

### Scope (per audit-pass-spec.md "Scope")

Audited:

```
${OBSIDIAN_VAULT_ROOT}/
1.Inbox/
  ai-capture-*.md
  pending-foundry-*.md
  ai-capture-FAILED-*.md
2.Resources/Notes/
  Books/, Media/, Quotes/, Ideas/, People/, Podcasts/, Annotations/, Misc/
<raw-root>/<YYYY-MM-DD>/<session-id>.md            (kun for wikilink-validation)
```

`<raw-root>` trenger ikke ligge under `<vault-root>`: korpuset flyttes ut av
vault-treet slik at Obsidian slipper aa parse ~8 000 maskin-genererte
transkripsjoner. Formen under rota er uendret, før som etter. Autoritativt hjem for
env-navnet og default-stien er `cortex/scripts/memory/_paths.py`; parity
mot den sjekkes i `_test/smoke_phase_1000.py`.

Eksklusjoner:

- `_test/`, `archive/`, `templates/`
- `2.Resources/Notes/**/_*.md` (underscore-prefix opt-out)
- Entries med `processing_state: review` (Carl's KE-research-flag)
- Sync-conflict-filer (`*.sync-conflict-*.md`)

### Frontmatter-format

Per `cortex/docs/contracts/capture-vocabulary.md` schema_version 1.
Audit forventer:

- Hard-required-felter per `pre_classified`-niva (full / partial / none)
- `tags: [📥, 💭]` for entries i `2.Resources/Notes/`
- `topics:`-array med `¤`-prefixed kebab-case
- `source_session: [[raw/<date>/<sid>]]` for `source: ai-session`-entries

## Output-format

### Rapport-fil

Per `audit-pass-spec.md` "Report-file":

- **Path:** `${OBSIDIAN_VAULT_ROOT}/5.Utility/Pipeline/Audit-Reports/YYYY-MM-DD.md`
  (UTC-dato matcher cron-fire-tid; norsk dato pa 04:00-fyring er entydig UTC+2)
- **Frontmatter:** strukturert per spec ("Frontmatter shape")
- **Body:** 6 seksjoner (en per sjekk), idempotent struktur (samme vault-tilstand -> samme rapport modulo timestamp)
- **Atomic write:** tempfile `.tmp-audit-<uuid>.md` + `os.replace` (samme monster som fallback-classifier)
- **Overwrite-policy:** re-run innen samme kalenderdato overskriver rapport-fil
  (ikke append). audit_date i frontmatter reflekterer siste run.

### Heartbeat-state

Per `audit-pass-spec.md` "Heartbeat-state":

- **Path:** `~/.audit-state.json` pa foundry-CT (utenfor vault, ikke synket)
- **Atomic write:** tempfile + `os.replace`
- **Felter:** `last_run`, `last_exit_code`, `last_tier`,
  `last_findings_total`, `consecutive_silent_runs`, `consecutive_failed_runs`

### Telegram-varsel

Per `audit-pass-spec.md` "Telegram tiered alerting":

| Tier | Trigger | Foundry-handling |
|------|---------|------------------|
| silent | Null findings | Ingen melding; heartbeat oppdateres |
| lav | Broken `links:`/`related:`, unknown fields, divergens-rate <= 0.20 | Ingen melding; kun rapport-fil |
| hoy | Dedup-kollisjoner, schema-violations, legacy-emoji-regresjon, divergens > 0.20, missing conditional-required | Ett Telegram-melding med per-sjekk-tellinger |
| kritisk | schema_version-mismatch, broken source_session pa ai-session, runtime-feil i runner | Umiddelbar melding med fil-paths, prefiks `🚨` |

Format per spec eksempler. audit.py konstruerer meldingen og kaller
`FOUNDRY_NOTIFY_SH` med ferdig tekst.

## Atomic write-back-semantikk

Identisk monster med fallback-classifier (Phase 900):

1. **Rapport-fil:** skriv til `.tmp-audit-<uuid>.md` i
   `5.Utility/Pipeline/Audit-Reports/`, deretter `os.replace` til
   `YYYY-MM-DD.md`. POSIX-atomic.
2. **Heartbeat-state:** skriv til `~/.audit-state.json.tmp-<uuid>`,
   deretter `os.replace` til `~/.audit-state.json`.

Syncthing `.stignore`-pattern `.tmp-audit-*` anbefales pa Syncthing-siden
for a hindre in-flight propagasjon (eksisterende `.tmp-fallback-*`-
mitigeringen kan utvides analogt).

## Fail-modes

| Tilstand | Foundry-respons |
|----------|-----------------|
| `1.Inbox/` eller `2.Resources/Notes/` mangler | exit 124 (vault unavailable), tier kritisk etter 2 paafolgende |
| `OBSIDIAN_VAULT_ROOT` ikke satt eller path eksisterer ikke | Hard fail: notify Telegram `(FATAL)`, exit 2 |
| Raw-rota (`$CORTEX_RAW_ROOT`, ellers default) eksisterer ikke | exit 124 (raw corpus unavailable), ingen rapport skrives. Raw-korpuset ligger utenfor vaulten, så en montert vault sier ingenting om det. Tom raw-indeks ville rapportert HVER note med `source: ai-session` som `kritisk` - hele `2.Resources/Notes/` - så passet nekter i stedet |
| `system-prompt.md` mangler | Hard fail: notify Telegram `(FATAL)`, exit 2 |
| Frontmatter-YAML korrupt pa enkelt-fil | Logg WARNING, registrer som schema-violation (sjekk 2), fortsett |
| `claude -p` returnerer ikke-parsbar JSON (sjekk 4) | Logg WARNING, hopp over den entry'en fra sample, fortsett |
| `claude -p` per-fil-timeout (sjekk 4) | Logg WARNING, hopp over entry, fortsett |
| Rapport-fil-write IO-feil | Logg ERROR + notify Telegram `(FATAL)` + exit 1 |
| LLM-call hard cap (50 calls) overskridet | Stopp sjekk 4 etter cap, rapporter delvis sample-result, fortsett ovrige sjekker |
| Sjekk 4 har < 5 fresh entries | Skip sjekk 4 silent, rapport-frontmatter setter `classification_check_skipped: true` |

## Idempotens

Per `audit-pass-spec.md` "Idempotency":

- Re-run innen samme kalenderdato overskriver rapport-fil (ikke append)
- Per-sjekk-funn sorteres deterministisk (file-path-stable sort)
- Sample-classification entry-utvalg er deterministisk (sort by file-path,
  ta forste 10%)
- Heartbeat-state oppdateringer er timestamp-baserte (latest wins)
- Ingen state-mutasjon i audited vault entries

## Endringskontroll

- Endringer i audit-sjekker, output-format, tier-policy eier vault-sentinel
  via `cortex/docs/contracts/audit-pass-spec.md`. Foundry reagerer
  via runner-update + `minimum-supported-schema-version`-bump per spec
  "Bump procedure".
- Endringer i runtime-grensesnittet (env-vars, exit-codes, pre-flight)
  krever pull-request mot DENNE fila + koordinering mot PLAN-foundry.
- Foundry kan endre run.sh + audit.py internals (lock-mekanikk,
  timeout-verdier, notify-format) uten kontrakts-endring sa lenge
  env-vars og exit-code-mapping bevares.

## Referanser

- `[[SPEC-foundry]]` - runtime-arkitektur, deploy-pipeline, watchdog
- `[[PLAN-foundry]]` Phase 1000 - implementering, task-list, acceptance
- `cortex/docs/contracts/audit-pass-spec.md` - audit-spec-autoritet
- `cortex/docs/contracts/capture-vocabulary.md` - filformat for entries
  som audites (schema_version 1)
- `cortex/docs/contracts/memory-knowledge-contract.md` - raw/-layout
  som sjekk 3 wikilink-valideringen leser
- `jobs/fallback-classifier/CONTRACT.md` - parallell monster (Phase 900)
- `jobs/memory-extract/CONTRACT.md` - parallell monster (Phase 600)
