# CONTRACT: foundry/jobs/memory-extract/

Leveranse-kontrakt mellom `[[SPEC-foundry]]` (runtime) og `[[SPEC-obsidian-memory]]` (payload).
Foundry leverer runtime, scheduling og pre-flight; obsidian-memory leverer Phase E payload-koden.

**Status:** Publisert 2026-05-08 som del av foundry Phase 500. obsidian-memory leverer mot
denne kontrakten i sin Phase E. Endringer her krever koordinering mellom prosjektene.

## Leveranser obsidian-memory må fylle inn

Følgende filer importeres til `~/foundry/jobs/memory-extract/` på CT:

| Fil | Type | Eier (skriving) | Beskrivelse |
|-----|------|-----------------|-------------|
| `extract.py` | Python 3.11+ | obsidian-memory | LLM-klassifisering av raw → typed extracted-entries |
| `system-prompt.md` | Markdown | obsidian-memory | `--append-system-prompt`-content for `claude -p` |
| `requirements.txt` | Python deps | obsidian-memory | Pip-deps for extract.py (deploy.sh setter opp `.venv/`) |

`run.sh` (eier: foundry) source'er secrets.env og kaller `.venv/bin/python extract.py`.
Payload-guard i `run.sh` exit'er stille hvis `extract.py` mangler (Phase 600 ikke aktivert).

## Input-format (foundry → obsidian-memory)

extract.py mottar input som:

* **Glob-pattern:** `${VAULT_ROOT}/8.Cortex/Memory/raw/<YYYY-MM-DD>/<session-id>.md`
  * `VAULT_ROOT` settes av `run.sh` fra env (default `/home/claude/vault`, justeres når Syncthing-share er etablert i Phase 400)
  * `<YYYY-MM-DD>` = capture-dato
  * `<session-id>` = Claude Code session-id (UUID)
* **Fil-format:** Markdown med vault-konformant frontmatter
  * `category: cortex`
  * `topics: ¤topic-1, ¤topic-2` (kan være tom liste)
  * `description: <kort>`
  * `parent: ` (typisk peker tilbake til opprinnelses-prosjekt)
  * Body: bruker-prompts + assistant-tekst + komprimerte tool-call-sammendrag
* **Idempotency-kontrakt:** Filnavn (session-id) er kildesannhet. Hvis raw-fil
  for samme session-id allerede har generert extracted-entries, skal extract.py
  skip stille (ingen re-run uten manuell sletting av extracted-entries).

## Output-format (obsidian-memory → vault)

extract.py skriver til:

* **Sti:** `${VAULT_ROOT}/8.Cortex/Memory/extracted/<type>-YYYY-QN.md`
  * `<type>` ∈ {`observation`, `decision`, `learning`, `error`, `pattern`, `intent`} (6-type ontologi)
  * `YYYY-QN` = kvartal-window (f.eks. `2026-Q2`)
* **Append-modus:** Hver entry appendes som `### <ISO-dato> <kort-tittel>`-heading-blokk
  med topics-tag-linje (`topics: ¤topic-1, ¤topic-2`) og kilde-referanse til raw-fila.
* **Frontmatter:** Etablert ved første skriving av en kvartal-fil; aldri overskrevet senere.

extract.py får IKKE skrive utenfor `${VAULT_ROOT}/8.Cortex/Memory/extracted/`. Eventuelle
side-effekter (cache, temp-filer) holdes i `jobs/memory-extract/.state.json` (gitignored).

## Exit-code-semantikk

run.sh propagerer extract.py sin exit-kode 1:1:

| Kode | Betydning | run.sh-håndtering |
|------|-----------|-------------------|
| 0 | OK - alle raw-filer prosessert eller ingen nye filer å prosessere | Logg success, exit 0 |
| 1 | Transient feil (LLM-rate-limit, nettverk, Syncthing-konflikt funnet) | Notify Telegram, exit 1; cron prøver igjen neste dag |
| 2 | Fatal feil (manglende dependencies, korrupt input, ugyldig vault-state) | Notify Telegram med `(FATAL)`-prefiks, exit 2; krever manuell intervensjon |
| Andre | Behandles som fatal | Notify, exit som-er |

Forskjellen mellom 1 og 2 er manuell-intervensjons-behov: 1 = retry-kandidat, 2 = krever
oppmerksomhet før neste cron-kjøring.

## Forventet kjøretid

* **Normal kjøring:** < 5 min for 1-3 nye raw-filer per dag.
* **Hard timeout i run.sh:** 30 min (`timeout 30m`).
* Hvis extract.py kjører lenger enn 30 min, drepes den hardt og run.sh exit'er 124
  (timeout-spesifikk kode); behandles som transient feil av notify-pipeline.

## Miljøvariabler satt av run.sh

run.sh source'er `~/.config/foundry/secrets.env` og videresender følgende til extract.py:

| Variabel | Kilde | Bruk i extract.py |
|----------|-------|-------------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | secrets.env | Authorization for `claude -p`-kall |
| `VAULT_ROOT` | run.sh (default `~/vault`) | Base-sti for input-glob og output-skriving |
| `EXTRACT_STATE_FILE` | run.sh | Sti til `.state.json` for cache/cursor-tracking |
| `EXTRACT_LOG_FILE` | run.sh | Sti til `~/foundry/logs/memory-extract.log` (append-mode) |

extract.py må IKKE kreve andre env-vars uten å oppdatere denne kontrakten først.

## Pre-flight (foundry-eid, før extract.py kalles)

run.sh kjører følgende sjekker FØR extract.py:

1. **`flock --nonblock ~/foundry/.deploy.lock`** - serialiserer mot auto-update.sh
2. **`ssh filehub-cleanup ${VAULT_ROOT_FILEHUB}`** - rydder Syncthing-konflikter på filehub-siden
   før ekstrakt. Exit 1 fra cleanup = konflikter funnet, run.sh exit 1 (transient).
3. **Glob-assert** - hvis `8.Cortex/Memory/raw/` mangler eller er tom, exit 0 (no-op)
4. **`timeout 30m`** rundt `.venv/bin/python extract.py` med std-args

Hvis pre-flight feiler, kalles extract.py ikke i det hele tatt.

## Endringskontroll

* Endringer i input/output-stier eller exit-codes krever pull-request mot DENNE fila
  + tilsvarende oppdatering i `[[SPEC-obsidian-memory]]` Phase E.
* Foundry kan endre runtime-detaljer (lock-mekanikk, timeout-verdi, notify-format)
  uten kontrakts-endring så lenge exit-codes propageres uendret til notify-pipeline.
* obsidian-memory eier extract.py-implementering og kan endre intern logikk fritt så
  lenge input-glob og output-format respekteres.

## Referanser

* `[[SPEC-foundry]]` - runtime-arkitektur, deploy-pipeline, watchdog
* `[[SPEC-obsidian-memory]]` Phase E - extract-payload-leveranse, 6-type ontologi
* `[[SPEC-device-sync-and-backup]]` - filehub-cleanup-bridge for pre-flight
