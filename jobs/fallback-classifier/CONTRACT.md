# CONTRACT: foundry/jobs/fallback-classifier/

Leveranse-kontrakt mellom `[[SPEC-foundry]]` (runtime) og `[[SPEC-vault-sentinel]]` /
`[[PLAN-3X-cortex-annotation-bridge]]` (input-pipeline) / `[[capture-vocabulary]]`
(filformat-autoritet).

Foundry-fallback-classifier-cron plukker `pending-foundry-*.md`-filer fra
`${OBSIDIAN_VAULT_ROOT}/1.Inbox/`, fyller manglende klassifisering, og renaver
til `ai-capture-*.md` slik at vault-sentinel batch-processor (interim: PLAN-3X
Phase 450 inbox-handler) plukker dem opp i neste runde.

**Status:** Phase 900 (PLAN-foundry) - strukturell deploy uavhengig av PLAN-3X
Phase 600 (batch-processor); reell ende-til-ende-test gated på Phase 600-leveranse.

**Filformat-autoritet:** `cortex/docs/contracts/capture-vocabulary.md`
([GitHub](https://github.com/Spud80/cortex/blob/dev/docs/contracts/capture-vocabulary.md))
eier frontmatter-skjema, `pre_classified`-enum, hard-required-felt og pipeline-
state-machine. Foundry CONTRACT.md (denne fila) eier kun runtime-grensesnittet.

Minimum-supported-schema-version: 1 (per capture-vocabulary.md "Consumer
declarations").

## Hva foundry-fallback-classifier gjør

Per cron-run (03:00 norsk lokal-tid):

1. **Recovery-scan:** identifiser `1.Inbox/pending-foundry-*.md`-filer i 3 tilstander
   (se "Recovery-scan-semantikk" under).
2. **Klassifiser per fil:**
   - `pre_classified: partial` → fyll manglende hard-required-felter
   - `pre_classified: none` → full klassifisering fra raw body
3. **Atomic write-back:** skriv normalisert frontmatter via tempfile+`os.replace`
   til samme fil-sti. Bevarer felter satt ved capture-tid (`dedup_hash`,
   `created`, `source`, `source_session`, `user_id`, `scope`).
4. **Sett `pre_classified: full`** og fjern `foundry_pending: true`-marker.
5. **Atomic rename:** `pending-foundry-<sid>-<ts>.md` → `ai-capture-<sid>-<ts>.md`
   via `os.rename`. Filen er nå synlig for Phase 450 inbox-handler / vault-sentinel
   batch-processor.

Foundry endrer **IKKE** `processing_state` (forblir `new` - batch-processor
bumper til `completed` ved flytt til `Notes/`).

## Leveranser foundry leverer

Følgende filer er foundry-eid (ingen ekstern payload-leveranse - dette er en
selvstendig jobb):

| Fil | Type | Beskrivelse |
|-----|------|-------------|
| `run.sh` | Bash | Orkestrering: secrets, flock, recovery-scan, per-fil classify-løkke |
| `classify.py` | Python 3.11+ | Per-fil-implementering: glob, frontmatter-parse, claude -p, atomic write+rename |
| `system-prompt.md` | Markdown | `--append-system-prompt`-content for `claude -p`; instruerer LLM å returnere JSON med frontmatter-felter |
| `requirements.txt` | Python deps | `pyyaml>=6.0` (frontmatter-parsing) |

`deploy.sh` setter opp `.venv/` per `requirements.txt` (samme mekanikk som
memory-extract).

## Runtime-grensesnitt

### Miljøvariabler (run.sh → classify.py)

run.sh source'er `~/.config/foundry/secrets.env` (set -a) og eksporterer i
tillegg `OBSIDIAN_VAULT_ROOT`:

| Variabel | Kilde | Påkrevd? | Bruk |
|----------|-------|----------|------|
| `CLAUDE_CODE_OAUTH_TOKEN` | secrets.env | Ja | Authorization for `claude -p`-kall |
| `OBSIDIAN_VAULT_ROOT` | run.sh (default `${HOME}/vault/My Vault`) | Ja på foundry CT | Base-sti for `1.Inbox/`-glob |
| `FOUNDRY_NOTIFY_SH` | run.sh (default `${REPO_ROOT}/_shared/notify.sh`) | Nei | Lar classify.py eskalere transient/recovery-warnings til Telegram uten exit |
| `FALLBACK_CLAUDE_TIMEOUT` | run.sh (default `20m`) | Nei | Per-fil `timeout`-verdi for `claude -p` |
| `FALLBACK_RETRY_MAX_ATTEMPTS` | classify.py (default `3`) | Nei | Maks antall forsøk på `claude -p` ved transient-feil (529/503/rate-limit). Sett 1 for å disable retry. |

classify.py må IKKE kreve andre env-vars uten å oppdatere denne kontrakten.

### Exit-code-semantikk

run.sh propagerer classify.py sin exit-kode:

| Kode | Betydning | run.sh-håndtering |
|------|-----------|-------------------|
| 0 | OK - alle pending-filer prosessert, ingen feil. Tom 1.Inbox/-glob er også OK. | Logg, exit 0 stille |
| 1 | Per-fil transient feil (LLM-feil, parse-feil, timeout på enkelt-fil); øvrige filer prosessert OK; failed-filer blir igjen som `pending-foundry-*.md` med `foundry_pending: true` for retry neste cron | Notify Telegram "transient", exit 1 |
| 2 | Hard fatal (missing system-prompt.md, korrupt vault-path, schema-mismatch) | Notify Telegram `(FATAL)`, exit 2; krever manuell intervensjon |
| 124 | Hard timeout (`timeout` på classify.py som helhet drepte prosessen) | Notify "TIMEOUT", behandles som transient |
| Andre | Behandles som fatal | Notify, exit som-er |

Per-fil-timeout (`claude -p` per fil) håndteres internt i classify.py som exit 1
for den fila; classify.py fortsetter med neste fil og samler aggregert exit-kode.

### Forventet kjøretid

- **Normal kjøring:** < 5 min per pending-fil (LLM-call dominerer). Typisk
  0-3 pending-filer per natt → 0-15 min totalt.
- **Hard timeout per fil:** 20 min (`FALLBACK_CLAUDE_TIMEOUT=20m`). Drepes
  hardt, eskaleres som transient.
- **Aggregert run.sh-timeout:** ingen (classify.py-løkke begrenses av per-fil-
  timeout × antall pending; 03:00→04:00 vinduet til audit-cron er ~60 min).

## Pre-flight (foundry-eid, før classify.py kalles)

run.sh kjører følgende sjekker FØR classify.py:

1. **Payload-guard:** `[ -f classify.py -a -f system-prompt.md ] || { notify "payload not deployed"; exit 0; }`.
   Lar inaktive jobber coexiste med aktiv cron uten daglige feil.
2. **Source secrets.env** (FATAL hvis mangler eller `CLAUDE_CODE_OAUTH_TOKEN` ikke satt).
3. **`flock --nonblock ~/foundry/.deploy.lock`** - serialiserer mot auto-update.sh,
   memory-extract og audit (Phase 1000).

Foundry-fallback-classifier kaller IKKE `ssh filehub-cleanup` per design.
Begrunnelse: cleanup er rotert til memory-extract sin 18:30-pre-flight og audit
sin 04:00-pre-flight; 03:00-fallback-cron kjører innen samme syncthing-konvergens-
vindu og dupliserer ikke pre-flight-arbeidet. Eventuelle sync-conflict-filer i
`1.Inbox/` (lite sannsynlig - capture-pipeline skriver med unique session-id i
filnavn) plukkes ikke opp av glob (`pending-foundry-*.md` matcher ikke
`*.sync-conflict-*`).

## Input-format (1.Inbox/pending-foundry-*.md)

Per `capture-vocabulary.md` "Storage layout" + "Pipeline-state machine":

- **Path:** `${OBSIDIAN_VAULT_ROOT}/1.Inbox/pending-foundry-<sid>-<ts>.md`
- **Trigger-marker:** `foundry_pending: true` i frontmatter
- **Frontmatter-tilstand ved pickup:**
  - `pre_classified: partial` - noen hard-required felt satt, andre missing
  - `pre_classified: none` - kun raw body + minimal metadata (`source`,
    `source_session`, `dedup_hash`, `created`, `updated`)

Hard-required-felter foundry må fylle (per `capture-vocabulary.md` "Required fields"):

```yaml
title:                 # string, AI-generert hvis missing
capture:               # enum: idea | quote | book | movie | tv_series | podcast | person | note
intent:                # enum: followup | reminder | someday | question | decision | null
processing_state:      # holdes `new` - batch-processor bumper til `completed`
```

Conditionally required:

```yaml
status:                # required når intent != null (default: active)
due:                   # required når intent: reminder (ISO8601)
```

Foundry kan også fylle `topics:` (¤kebab-case-tags) når content tilsier det,
men dette er ikke hard-required.

## Output-format (1.Inbox/ai-capture-*.md)

Etter vellykket classify:

- **Path:** `${OBSIDIAN_VAULT_ROOT}/1.Inbox/ai-capture-<sid>-<ts>.md`
- **Frontmatter-endringer:**
  - `pre_classified: full` (overskrives uansett tidligere verdi)
  - `foundry_pending: true` fjernet helt fra frontmatter
  - Manglende hard-required felter fylt (per "Input-format" tabellen over)
  - `tags: [📥, 💭]` satt hvis missing (default for thought-pipeline-entries)
  - `topics: [...]` satt hvis classifier identifiserer relevante ¤-tags
- **Body:** uendret (raw body bevares byte-identisk)

Felter foundry IKKE rører (capture-tid-felter):

- `dedup_hash` (beregnet av `/save`-skill eller mobile/web pipeline)
- `created` (ISO8601 fra capture-tid)
- `source` (`ai-session` | `mobile` | `web` | `manual`)
- `source_session` (`[[raw/<date>/<session-id>]]` når `source: ai-session`)
- `source_url` (når relevant)
- `user_id` (multi-tenant placeholder, default `carl`)
- `scope` (default `personal`)

## Atomic write-back-semantikk

Per PLAN-foundry Phase 900 task 900-3 (Runde 6, 2026-05-13). Identisk mønster
med PLAN-cortex-annotations Phase 450 inbox-handler.

### Operasjons-rekkefølge per fil

1. **Mutate first:** skriv ny frontmatter til tempfile `<dir>/.tmp-fallback-<sid>-<ts>.md`,
   `os.replace(tempfile, pending_path)`. Atomic POSIX-rename garanterer at
   `pending-foundry-*.md` enten har gammel eller ny content - aldri halvskrevet.
2. **Rename last:** `os.rename(pending_path, ai_capture_path)`. Filen er
   usynlig for Phase 450 inbox-handler (som globber `ai-capture-*.md`) inntil
   rename-en er ferdig.

### Hvorfor mutate-first-then-rename (ikke omvendt)

- Hvis vi rename'r først, kan Phase 450 inbox-handler plukke opp `ai-capture-*.md`
  midt i mutation - se halv-frontmatter med f.eks. `foundry_pending: true`
  fortsatt, og avvise (re-rename til `pending-foundry-*.md` → loop).
- Mutate-first holder fila som `pending-foundry-*.md` (ikke i Phase 450-glob)
  inntil ALT er klart, så rename er den eneste "publish"-handlingen.

### Syncthing-propagasjon av tempfiles

Foundry-CT-side har ikke vault-access-guard. Tempfile-pattern `.tmp-fallback-*`
må være i Syncthing `.stignore` (eksisterende på filehub-siden; verifisert under
deploy-rollout). Begrunnelse: hindre at Syncthing propagerer in-flight tempfiles
til andre noder før atomic-replace er ferdig.

## Recovery-scan-semantikk

Per PLAN-foundry Phase 900 task 900-3 (Runde 6). classify.py kjører recovery-scan
før normal classify-løkke for å håndtere mid-write-krasj-scenarioer.

For hver `1.Inbox/pending-foundry-*.md`, klassifiser tilstand:

| Tilstand | Trigger | Foundry-handling |
|----------|---------|------------------|
| **A: normal-pending** | `foundry_pending: true` | Normal classify-flyt (mutate + rename) |
| **B: rename-only** | `foundry_pending` fjernet AND alle hard-required satt AND `pre_classified: full` | Crash mellom mutate og rename. Bare rename → `ai-capture-*.md` |
| **C: re-classify** | `foundry_pending` fjernet AND minst ett hard-required missing | Mutate-tempfile-replace feilet mid-flight (skal ikke skje med atomic write, men recovery er forsvar i dybden mot Syncthing-merge-rariteter). Re-classify som om `foundry_pending: true` fortsatt var der |
| **D: log-warning** | Annen kombinasjon (f.eks. `foundry_pending: false` eksplisitt, eller uventet enum) | Logg WARNING + notify Telegram + skip fil; manuell intervensjon kreves |

Recovery er per definisjon idempotent: re-kjøring på samme tilstand gir samme
resultat (B blir A → ai-capture; C blir A → re-mutate → rename; D forblir D).

## Fail-modes

| Tilstand | Foundry-respons |
|----------|-----------------|
| `1.Inbox/`-glob returnerer 0 filer | exit 0 silent (forventet steady-state når PLAN-3X Phase 600 ikke har skrevet pending-filer) |
| Frontmatter-YAML korrupt på enkelt-fil | Logg ERROR + notify Telegram + skip fil + akkumuler exit 1 (transient) |
| `claude -p` upstream transient (529 Overloaded, 503, rate-limit) | In-process retry opp til `FALLBACK_RETRY_MAX_ATTEMPTS` (default 3) med eksponentiell backoff (60s, 180s); ved suksess: log som vanlig classify; ved uttømte forsøk: behandles som transient (logg ERROR + notify "transient exhausted" + skip fil + akkumuler exit 1) |
| `claude -p` returnerer ikke-parsbar JSON | Logg ERROR + notify Telegram + skip fil + akkumuler exit 1 (ingen retry; symptom på prompt-issue, ikke API-overload) |
| `claude -p` per-fil-timeout (20m) | Logg ERROR + notify Telegram "TIMEOUT" + skip fil + akkumuler exit 1 (ingen retry; timeout signaliserer langvarig blokk) |
| `claude -p` non-transient exit (parse-error, ugyldig arg) | Logg ERROR + notify Telegram + skip fil + akkumuler exit 1 (fail fast, ingen retry) |
| `system-prompt.md` mangler | Hard fail: notify Telegram `(FATAL)`, exit 2 |
| `OBSIDIAN_VAULT_ROOT` ikke satt eller path eksisterer ikke | Hard fail: notify Telegram `(FATAL)`, exit 2 |
| Manglende hard-required-felt ETTER claude -p (LLM ga ikke nok info) | Logg WARNING; fila beholdes som `pending-foundry-*.md` med `foundry_pending: true` for retry neste cron; akkumuler exit 1 |
| Tempfile-replace IO-feil | Logg ERROR + notify Telegram + skip fil; akkumuler exit 1; recovery-scan håndterer ved neste run |
| Rename-IO-feil (etter vellykket mutate) | Recovery-scan ved neste run identifiserer som tilstand B og fullfører rename; logg WARNING; akkumuler exit 1 |

Aggregert exit-kode-logikk: exit 2 (fatal) > exit 1 (any transient) > exit 0 (clean).

**Transient-retry-policy:** `claude -p`-exit med stderr/stdout som matcher mønster (`529`, `overloaded`, `503`, `502`, `rate_limit`, `rate limit`, `too many requests`) retries in-process opp til `FALLBACK_RETRY_MAX_ATTEMPTS` forsøk (default 3) med backoff fra `RETRY_BACKOFF_SECONDS` (default `[60, 180]`). Worst-case tilleggslatens per fil = sum av backoffs (4 min) + N retries × per-kall-tid. Per-fil-timeout (`FALLBACK_CLAUDE_TIMEOUT_SECONDS`, default 1200s/20m) gjelder per subprocess-kall, ikke aggregert over retries - 3 retries innenfor backoff-vinduet og 1200s claude-call-tid hver kan teoretisk bruke opptil ~64 min på worst-case-fil, men praktisk er hver retry-kall < 30s.

## Endringskontroll

- Endringer i runtime-grensesnittet (env-vars, exit-codes, pre-flight-rekkefølge)
  krever pull-request mot DENNE fila + koordinering mot PLAN-foundry.
- Endringer i input-format (`pending-foundry-*.md`-frontmatter, `foundry_pending`-
  marker, hard-required-felt) eier vault-sentinel via
  `cortex/docs/contracts/capture-vocabulary.md`. Foundry reagerer kun
  hvis runtime-grensesnittet endres som følge.
- Foundry kan endre run.sh + classify.py internals (lock-mekanikk, timeout-
  verdier, notify-format, JSON-schema mellom system-prompt og classify.py) uten
  kontrakts-endring så lenge env-vars og exit-code-mapping bevares.

## Referanser

- `[[SPEC-foundry]]` - runtime-arkitektur, deploy-pipeline, watchdog
- `[[PLAN-foundry]]` Phase 900 - implementering, task-list, acceptance
- `[[PLAN-3X-cortex-annotation-bridge]]` Phase 450 - inbox-handler interim-arkitektur
- `[[PLAN-3X-cortex-annotation-bridge]]` Phase 600 - pre_classified pickup-logikk (produsent av pending-filer)
- `cortex/docs/contracts/capture-vocabulary.md` - filformat-autoritet
  (frontmatter-skjema, `pre_classified`-enum, pipeline-state-machine)
- `jobs/memory-extract/CONTRACT.md` - parallell mønster (Phase 600 leveranse-kontrakt)
