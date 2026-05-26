#!/usr/bin/env python3
"""Extract-pass orchestrator for obsidian-memory pipeline.

Per PLAN-obsidian-memory Phase E (re-spec 2026-05-07: foundry takes over
extract-ownership; Runde 6 manifest-handshake):

- Reads raw/<date>/<session-id>.md files written by `memory-capture.py` on
  filehub. Subprocess-invokes `claude -p` with `--append-system-prompt` from
  `system-prompt.md` and `--json-schema` for structured output validation.
  Subscription auth via `CLAUDE_CODE_OAUTH_TOKEN` (~1 year, foundry-runtime
  source's secrets.env before invocation).
- Pre-flight (in order):
  1. `_capture-manifest.json` per date-dir verified: present + listed paths
     present locally + sha256 matches. Adresses Syncthing leveranse-ordering.
  2. Syncthing REST API: `needFiles == 0` for `claude-memory` and `obsidian`
     folders (sekundary gate; soft-fail with transient exit if env-var
     `SYNCTHING_API_KEY` not set).
  3. State-fil `.compile-state.json` validation + state-vs-extracted mismatch
     detection. Mismatch -> abort with explicit warning unless
     `--force-rebuild` or `--force-fresh`.
- Per session: subprocess to `claude -p`, parse JSON output, append heading-
  blocks to `extracted/<type>-YYYY-QN.md`. Quarter-file auto-created from
  template if missing. State-fil updated atomically per processed session
  (mid-batch-crash safe).

Authoritative file format spec:
`dev-environment/docs/reference/memory-knowledge-contract.md`.

Foundry-side wrapping (`foundry/jobs/memory-extract/run.sh`) eats the cron
trigger, source's `secrets.env` for `CLAUDE_CODE_OAUTH_TOKEN`, and invokes
this script. See `README-foundry.md` for delivery contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# K6 Sources-append + aliases-loading live in a shared library so the CLI
# (`memory-sources-append`) and this extract-cron use the exact same
# atomic-rename / flock mechanics AND aliases-loading semantics (G3a-4
# Runde 10 acceptance: "ekte ekstraksjon, ikke kopi"; aliases-relocation
# 2026-05-14 to unblock /usr/local/bin/-only deploy on foundry-CT where
# extract.py is NOT in sys.path). The names below are re-exported for
# backward-compat with smoke_k5_k6.py and any downstream importer of
# `extract` (e.g. `extract.load_aliases`, `extract.AliasesError`).
from _k6_source_append import (  # noqa: F401  (re-export)
    _HAS_FCNTL,
    _SOURCES_HEADER_RE,
    _insert_under_sources,
    ALIASES_FILENAME,
    AliasesError,
    MIN_ALIASES_SCHEMA_VERSION,
    append_to_compiled_sources,
    atomic_write,
    load_aliases,
)

SCHEMA_VERSION = 1
TYPES = ("observation", "decision", "learning", "error", "pattern", "intent")
MANIFEST_FILENAME = "_capture-manifest.json"
STATE_FILENAME = ".compile-state.json"
COMPILED_DIRNAME = "compiled"
# Raw-side schema floor (H5, 2026-05-14). Per memory-knowledge-contract.md
# raw-frontmatter contains `raw_schema_version: N`. Pre-H5 files carry only
# legacy `contract-version: 1`; we accept those as version-1-equivalent for
# backward-compat reads of the 4749+ historical raw-files. A future
# raw_schema_version > MIN aborts FATAL (exit 2) to force coordinated bump
# rather than silently feeding incompatible raw-format to the LLM.
MIN_RAW_SCHEMA_VERSION = 1
SCRIPT_DIR = Path(__file__).resolve().parent

# H5: raw-frontmatter parsing for schema-version validation. Lightweight
# regex (avoids PyYAML dependency for this single check). The frontmatter
# fence is exactly `---\n...\n---` per _jsonl_format.format_jsonl().
_RAW_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_RAW_INT_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(\d+)\s*$", re.MULTILINE)

# Bare-bones schema: types and required fields only. Strict constraints
# (regex patterns, additionalProperties:false, minItems/maxItems, enum on
# nested fields, minLength) trigger Claude's --json-schema strict-mode to
# silently return an empty result with stop_reason=end_turn instead of
# rejecting with an error. Empirically reproduced on foundry 2026-05-08:
# 10/10 sessions returned `result: ""` despite output_tokens=1245. The
# non-ASCII ¤ character in the topics-pattern is the most likely trigger,
# but additionalProperties:false on nested objects and the combination of
# multiple constraints also fail. We rely on Claude following the
# constraints documented in system-prompt.md and validate post-hoc in
# Python (see validate_entry below).
OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "slug", "topics", "body", "date"],
                "properties": {
                    "type": {"type": "string", "enum": list(TYPES)},
                    "slug": {"type": "string"},
                    "topics": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "modal": {"type": "string"},
                    "body": {"type": "string"},
                    "date": {"type": "string"},
                },
            },
        },
    },
    "required": ["entries"],
}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TOPIC_RE = re.compile(r"^¤[a-z0-9-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_MODALS = ("actionable", "speculative", "question")


def validate_entry(entry: dict) -> list[str]:
    """Return a list of constraint violations for a single entry. Empty list
    means the entry passes Python-side validation. Caller skips the entry and
    logs the violations when the list is non-empty.

    Mirrors the constraints previously enforced by the JSON schema before they
    were moved to Python (see OUTPUT_JSON_SCHEMA note above).
    """
    errs: list[str] = []
    t = entry.get("type")
    if t not in TYPES:
        errs.append(f"type={t!r} not in {TYPES}")
    slug = entry.get("slug", "")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        errs.append(f"slug={slug!r} not kebab-case")
    topics = entry.get("topics")
    if not isinstance(topics, list) or not (1 <= len(topics) <= 5):
        errs.append(f"topics must be a list of 1-5 items, got {topics!r}")
    else:
        for i, tag in enumerate(topics):
            if not isinstance(tag, str) or not TOPIC_RE.match(tag):
                errs.append(f"topics[{i}]={tag!r} does not match ^¤[a-z0-9-]+$")
    modal = entry.get("modal")
    if modal is not None and modal not in VALID_MODALS:
        errs.append(f"modal={modal!r} not in {VALID_MODALS}")
    body = entry.get("body", "")
    if not isinstance(body, str) or not body.strip():
        errs.append("body is empty or not a string")
    date = entry.get("date", "")
    if not isinstance(date, str) or not DATE_RE.match(date):
        errs.append(f"date={date!r} does not match YYYY-MM-DD")
    return errs

# Mirror of templates/extracted-quarter-skeleton.md (frozen format per PLAN Phase H1).
QUARTER_TEMPLATE = """---
type: {type}
category: cortex
quarter: {quarter}
topics:
  - ¤memory
  - ¤{type}
schema_version: 1
created: {today}
updated: {today}
---

[[INDEX-Memory|🧠 Memory]] · {type_title} entries - {quarter}

# {type_title} - {quarter}

> [!info]+ Append-only file
> Entries appended by `memory-extract.py` as heading-blocks:
>
> ```
> ## YYYY-MM-DD - slug
> topics: ¤topic-1, ¤topic-2
> source: [[<date>/<session-id>]]
>
> body
> ```
>
> Manuell editering tillatt for korrigering, men nye entries kommer fra extract-pass. Se `[[INDEX-Memory]]` for pipeline-oversikt.

"""


# ----- Path helpers -----

def vault_root_from_args(args: argparse.Namespace) -> Path:
    if args.vault_root:
        return Path(args.vault_root)
    env = os.environ.get("OBSIDIAN_VAULT_ROOT")
    if env:
        return Path(env)
    if sys.platform.startswith("linux"):
        return Path("/data/sync/obsidian/My Vault")
    return Path("C:/sync/obsidian/My Vault")


def quarter_for(date_iso: str) -> str:
    """ '2026-05-08' -> '2026-Q2'. """
    y, m, _ = date_iso.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"{y}-Q{q}"


# ----- Notify helper (K5/K6 fail-mode signaling) -----

def notify(message: str) -> None:
    """Emit WARN/NOTIFY to stderr. If FOUNDRY_NOTIFY_SH env-var points at an
    executable, also enqueue via foundry's _shared/notify.sh. Best-effort;
    extract.py never fails because notify failed.
    """
    notify_sh = os.environ.get("FOUNDRY_NOTIFY_SH", "")
    if notify_sh:
        notify_path = Path(notify_sh)
        if notify_path.is_file():
            try:
                subprocess.run([str(notify_path), message], timeout=10, check=False)
            except Exception as e:  # noqa: BLE001
                print(f"NOTIFY-subprocess-failed: {e}", file=sys.stderr)
    print(f"NOTIFY: {message}", file=sys.stderr)


# ----- Raw-schema validation (H5: producer-side version contract) -----

class RawSchemaError(Exception):
    """Raised for hard-fail raw-schema conditions (missing frontmatter,
    missing both raw_schema_version and legacy contract-version, or
    raw_schema_version > MIN_RAW_SCHEMA_VERSION). Caller exits 2 + FATAL notify.
    """


def validate_raw_schema(raw_text: str, raw_path: Path) -> None:
    """Validate raw-file schema version. Returns None on success.

    Accepts (per memory-knowledge-contract.md Raw-files section):
      - New format: `raw_schema_version: N` (preferred). Aborts if N > MIN.
      - Legacy format: `contract-version: N` only (pre-H5 raw-files, ~4749
        historical). Accepted version-agnostically since legacy bump-policy
        was not version-validated by extract.py; the field exists purely
        for forwards-deprecation. Coordinated migration drops it after
        90-day organic turnover.
      - New + legacy both present (transition window): preferred field wins.

    Aborts FATAL (raises RawSchemaError) when:
      - Frontmatter fence missing
      - Neither raw_schema_version nor contract-version present
      - raw_schema_version > MIN_RAW_SCHEMA_VERSION (forces coordinated bump)

    contract-version > MIN is NOT a FATAL by design - legacy field has no
    enforced floor and historical files may carry inflated values from
    earlier experiments. The H5 contract is enforced via the new field.
    """
    m = _RAW_FRONTMATTER_RE.match(raw_text)
    if not m:
        raise RawSchemaError(f"{raw_path.name}: frontmatter fence missing")
    fm_text = m.group(1)
    raw_v: int | None = None
    legacy_v: int | None = None
    for match in _RAW_INT_FIELD_RE.finditer(fm_text):
        field, value = match.group(1), int(match.group(2))
        if field == "raw_schema_version":
            raw_v = value
        elif field == "contract-version":
            legacy_v = value
    if raw_v is None and legacy_v is None:
        raise RawSchemaError(
            f"{raw_path.name}: missing both raw_schema_version and "
            f"contract-version - cannot determine schema compatibility"
        )
    if raw_v is not None and raw_v > MIN_RAW_SCHEMA_VERSION:
        raise RawSchemaError(
            f"{raw_path.name}: raw_schema_version={raw_v} exceeds extract.py "
            f"minimum-supported={MIN_RAW_SCHEMA_VERSION} - coordinated bump required"
        )


# ----- Aliases (K5: producer-side topic normalisation) -----

def build_vocab_section(canonicals: list[str]) -> str:
    """Render the canonical vocabulary block appended to system-prompt.

    Empty list -> empty string (caller skips augmentation).
    """
    if not canonicals:
        return ""
    vocab_line = ", ".join(f"¤{c}" for c in canonicals)
    return (
        "\n\n# Preferred topic vocabulary\n\n"
        "Use these canonical topic-tags when applicable; only invent new tags "
        "for genuinely new subjects:\n\n"
        f"{vocab_line}\n"
    )


def resolve_topic(topic: str, alias_map: dict[str, str]) -> str:
    """Map a single ``¤alias`` tag to ``¤canonical`` via alias_map.

    Tags absent from alias_map pass through unchanged (genuinely new subjects).
    Tags without the ``¤`` prefix pass through unchanged (modal tags or other).
    """
    if not isinstance(topic, str) or not topic.startswith("¤"):
        return topic
    slug = topic[1:]
    canonical = alias_map.get(slug)
    if canonical is None:
        return topic  # unknown - pass through
    return "¤" + canonical


# ``atomic_write`` is now imported from _k6_source_append (single source of
# truth shared with the CLI). Re-exported above so existing call-sites in
# this module need no change.


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ----- Manifest pre-flight -----

class PreflightError(Exception):
    """Raised for transient pre-flight failures. Caller exits 0 (retry next cron)."""


class PreflightShaMismatchError(PreflightError):
    """Raised when manifest sha256 != on-disk sha256.

    Distinct from PreflightError because it is NOT transient - the manifest is
    stale (typically after backup-restore or external file modification) and
    will not self-heal by retrying. Caller exits 3 to escalate via run.sh
    Telegram notify, prompting manual `reconcile-manifest.py --apply`.
    """


def verify_manifests(raw_root: Path) -> list[Path]:
    """Verify every non-empty date-dir has a valid manifest. Return verified date-dirs."""
    if not raw_root.is_dir():
        return []
    verified: list[Path] = []
    for date_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        md_files = sorted(date_dir.glob("*.md"))
        if not md_files:
            continue
        manifest_path = date_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise PreflightError(
                f"capture sync incomplete: missing manifest for {date_dir.name} "
                f"({len(md_files)} raw-files present without manifest)"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise PreflightError(f"manifest unparseable for {date_dir.name}: {e}")

        listed = {entry["path"]: entry["sha256"] for entry in manifest.get("sessions", [])}
        # All listed paths must exist locally and match sha256
        for rel_path, expected_sha in listed.items():
            local = raw_root.parent.parent / "Memory" / rel_path  # vault/8.Cortex/Memory/raw/...
            # Actually raw_root = .../8.Cortex/Memory/raw, rel_path = raw/<date>/<file>.md
            # So local = raw_root.parent / rel_path
            local = raw_root.parent / rel_path
            if not local.exists():
                raise PreflightError(
                    f"capture sync incomplete: manifest for {date_dir.name} lists "
                    f"{rel_path} but file is not present locally"
                )
            actual = sha256_of(local)
            if actual != expected_sha:
                raise PreflightShaMismatchError(
                    f"sha256 mismatch on {rel_path} "
                    f"(manifest={expected_sha[:8]}.., actual={actual[:8]}..). "
                    f"Run reconcile-manifest.py --apply to fix."
                )
        verified.append(date_dir)
    return verified


# ----- Syncthing pre-flight (secondary gate) -----

def syncthing_preflight(folders: list[str], api_url: str, api_key: str | None) -> None:
    """Optional REST-API check. Soft-skip if no API key (manifest is primary gate)."""
    if not api_key:
        print("preflight: SYNCTHING_API_KEY not set, skipping REST-API gate", file=sys.stderr)
        return
    import requests  # local import - keeps script importable without dep
    for folder in folders:
        r = requests.get(
            f"{api_url}/rest/db/status",
            params={"folder": folder},
            headers={"X-API-Key": api_key},
            timeout=10,
            verify=False,
        )
        r.raise_for_status()
        data = r.json()
        need = data.get("needFiles", 0)
        if need > 0:
            raise PreflightError(
                f"syncthing folder '{folder}' has needFiles={need}, sync incomplete"
            )


# ----- State management -----

def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return _fresh_state()
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}  # signal corruption


def _fresh_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_success_at": None,
        "last_attempt_at": None,
        "last_error": None,
        "sessions_pending_count": 0,
        "last_run_produced_output": False,
        "processed_session_ids_by_date": {},
    }


def save_state(state_path: Path, state: dict) -> None:
    atomic_write(state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def state_processed_set(state: dict) -> set[tuple[str, str]]:
    """ {(date, session_id), ...} from state. """
    out: set[tuple[str, str]] = set()
    for date, ids in state.get("processed_session_ids_by_date", {}).items():
        for sid in ids:
            out.add((date, sid))
    return out


def extracted_processed_set(extracted_dir: Path) -> set[tuple[str, str]]:
    """Scan extracted/*.md for source: [[<date>/<session-id>]] refs."""
    out: set[tuple[str, str]] = set()
    if not extracted_dir.is_dir():
        return out
    pattern = re.compile(r"^source:\s*\[\[(\d{4}-\d{2}-\d{2})/([0-9a-f-]+)\]\]", re.M)
    for f in extracted_dir.glob("*.md"):
        text = f.read_text(encoding="utf-8")
        for date, sid in pattern.findall(text):
            out.add((date, sid))
    return out


# ----- Discovery -----

def discover_raw_sessions(raw_root: Path) -> list[tuple[str, str, Path]]:
    """Return [(date, session-id, path), ...] for every raw markdown file."""
    out: list[tuple[str, str, Path]] = []
    if not raw_root.is_dir():
        return out
    for date_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        for md in sorted(date_dir.glob("*.md")):
            out.append((date_dir.name, md.stem, md))
    return out


# ----- Claude subprocess -----

# Oversize-fallback: Sonnet's 200K context rejects raw transcripts > ~700KB
# (~230K tokens) with `is_error: true, result: "Prompt is too long"` in stdout JSON
# and exit=1 with empty stderr. Opus 4.7 with 1M context handles these on
# Claude Max subscription without the paid 1M add-on (verified 2026-05-12);
# Sonnet's 1M variant `claude-sonnet-4-6[1m]` requires the add-on, Opus's
# `claude-opus-4-7[1m]` does not.
OVERSIZED_CONTEXT_MODEL = "claude-opus-4-7[1m]"
OVERSIZED_ERROR_MARKERS = ("Prompt is too long", "prompt is too long")


# Harness-internal tags from the captured session that act as prompt-injection
# vectors when fed back to Claude as user content. The extractor model parses
# `<system-reminder>` etc. as REAL harness instructions (not transcript content)
# and may try to use tools (TaskCreate etc.) that aren't available in extract
# context, triggering structured-output-retry exhaustion
# (subtype=error_max_structured_output_retries, stop_reason=tool_use).
# Reproduced 2026-05-21 on session db0cdd12-3fe8-4456-ba88-0f54cb09f3d9: a
# session containing 2 `<system-reminder>` tags burned ~$0.30 / 196s / 6 turns
# before exhausting retries. A sibling session in the same date-dir with 0
# such tags processed cleanly. Sanitising at extract-input boundary is the
# correct layer: raw files stay faithful to the captured session, but the LLM
# never sees the dangerous syntax.
#
# Extended 2026-05-26 after session 19102926-0b6f-4b45-ab34-39f7fcbac4ac burned
# ~$0.55 / 169s / 7 turns: original list missed `<tool_use_error>` (6 occurrences
# in that file) and `<persisted-output>` (1). Survey of full raw-archive at fix-time:
# 1037 `<tool_use_error>`, 283 `<persisted-output>`, 40 `<bash-stdout>`, 40
# `<bash-stderr>` spread across 546 raw files - all added preemptively. The failure
# rate is probabilistic (LLM may or may not act on a given harness-marker), so most
# files happened to process cleanly historically.
_HARNESS_TAG_RE = re.compile(
    r'<(system-reminder|function_calls|function_results|antml:function_calls|antml:invoke|antml:parameter|tool_use_error|persisted-output|bash-stdout|bash-stderr)\b[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE,
)
_HARNESS_OPEN_TAG_RE = re.compile(
    r'</?(?:system-reminder|function_calls|function_results|antml:function_calls|antml:invoke|antml:parameter|tool_use_error|persisted-output|bash-stdout|bash-stderr)\b[^>]*>',
    re.IGNORECASE,
)


def sanitize_harness_tags(text: str) -> str:
    """Neutralise harness-internal tags in the captured transcript before
    sending to the extractor LLM. Replaces balanced tag-blocks with a marker;
    strips orphan open/close tags from truncated captures. Preserves the
    transcript's information density while removing the prompt-injection
    surface.
    """
    text = _HARNESS_TAG_RE.sub('[harness-tag redacted]', text)
    text = _HARNESS_OPEN_TAG_RE.sub('', text)
    return text


def _is_oversize_error(stdout: str) -> bool:
    try:
        d = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(d, dict) or not d.get("is_error"):
        return False
    result = d.get("result", "")
    return isinstance(result, str) and any(m in result for m in OVERSIZED_ERROR_MARKERS)


def _strip_md_json_fence(text: str) -> str:
    """Strip Markdown ```json ... ``` wrapper that LLMs sometimes add to JSON
    output despite the prompt asking for raw JSON. No-op when no fence detected.

    Why: ``claude -p --output-format json`` without ``--json-schema`` returns the
    LLM's literal output in ``wrapper["result"]``. Some prompts elicit a
    Markdown code-fence wrap (``\\`\\`\\`json\\n{...}\\n\\`\\`\\``); ``json.loads``
    then fails with "Expecting value: line 1 column 1". Stripping the fence
    before parsing keeps the call resilient to that LLM behaviour.
    """
    s = text.strip()
    if s.startswith("```json"):
        s = s[len("```json"):].lstrip()
    elif s.startswith("```"):
        s = s[len("```"):].lstrip()
    else:
        return text
    if s.endswith("```"):
        s = s[:-len("```")].rstrip()
    return s


def call_claude(
    raw_text: str,
    *,
    model: str,
    system_prompt: str,
    max_budget_usd: float | None,
    fallback_model: str | None,
) -> dict:
    """Invoke `claude -p` headless with structured output. Return parsed JSON."""
    # No --bare: would disable OAuth/keychain auth (incl. CLAUDE_CODE_OAUTH_TOKEN
    # env-var read), which is exactly the auth surface we depend on. Use
    # --system-prompt to fully override default Claude Code system prompt
    # (avoids tool-mention bloat and auto-memory loop risk).
    def _build_cmd(m: str) -> list[str]:
        c = [
            "claude", "-p",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(OUTPUT_JSON_SCHEMA),
            "--system-prompt", system_prompt,
            "--model", m,
        ]
        if fallback_model:
            c += ["--fallback-model", fallback_model]
        if max_budget_usd is not None:
            c += ["--max-budget-usd", str(max_budget_usd)]
        # Pass raw transcript via stdin to avoid argv-length limits and to keep
        # claude from misparsing leading `---` as an option flag.
        c += ["--input-format", "text"]
        return c

    proc = subprocess.run(
        _build_cmd(model),
        input=raw_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    if proc.returncode != 0 and _is_oversize_error(proc.stdout) and model != OVERSIZED_CONTEXT_MODEL:
        print(
            f"  oversize: '{model}' returned 'Prompt is too long' "
            f"({len(raw_text)} chars), retrying with {OVERSIZED_CONTEXT_MODEL}",
            file=sys.stderr,
        )
        proc = subprocess.run(
            _build_cmd(OVERSIZED_CONTEXT_MODEL),
            input=raw_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    if proc.returncode != 0:
        # Include stdout snippet: claude often surfaces the actual error there
        # (e.g. "Prompt is too long") with empty stderr, especially on api-errors.
        raise RuntimeError(
            f"claude exit {proc.returncode}: "
            f"stderr={proc.stderr[:300]!r} stdout={proc.stdout[:300]!r}"
        )
    # claude --output-format json wraps the response. Field placement depends
    # on whether --json-schema is in use (foundry-verified 2026-05-08):
    #   - With --json-schema: validated output goes to wrapper["structured_output"]
    #     as an already-parsed dict; wrapper["result"] is empty string.
    #   - Without --json-schema: wrapper["result"] is the raw text (often
    #     markdown-fenced JSON) and structured_output is absent.
    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude stdout not JSON: {e}; raw[:200]={proc.stdout[:200]}")
    if isinstance(wrapper, dict) and isinstance(wrapper.get("structured_output"), dict):
        return wrapper["structured_output"]
    if isinstance(wrapper, dict) and "result" in wrapper:
        result_text = wrapper["result"]
    else:
        result_text = proc.stdout  # fall back
    result_text = _strip_md_json_fence(result_text)
    try:
        return json.loads(result_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"extract response not JSON: {e}; first-200-chars={result_text[:200]}"
        )


# ----- Heading-block formatting -----

def format_heading_block(entry: dict, *, date: str, session_id: str) -> str:
    topics = ", ".join(entry["topics"])
    if entry.get("modal"):
        topics = f"{topics}, ¤{entry['modal']}"
    body = entry["body"].strip()
    return (
        f"## {entry['date']} - {entry['slug']}\n"
        f"topics: {topics}\n"
        f"source: [[{date}/{session_id}]]\n\n"
        f"{body}\n\n"
    )


def ensure_quarter_file(extracted_dir: Path, type_: str, quarter: str) -> Path:
    target = extracted_dir / f"{type_}-{quarter}.md"
    if target.exists():
        return target
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    atomic_write(target, QUARTER_TEMPLATE.format(
        type=type_, type_title=type_.capitalize(), quarter=quarter, today=today,
    ))
    return target


def append_block(target: Path, block: str) -> None:
    """Append a heading-block to a quarter-file (read-modify-write, atomic)."""
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if not existing.endswith("\n"):
        existing += "\n"
    atomic_write(target, existing + block)


def append_blocks(target: Path, blocks: list[str]) -> None:
    """Append multiple heading-blocks in a single atomic write.

    Per-session batching: equivalent to N append_block calls but one rename().
    Shrinks the Syncthing race-window proportionally to entries-per-target.
    """
    if not blocks:
        return
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if not existing.endswith("\n"):
        existing += "\n"
    atomic_write(target, existing + "".join(blocks))


# ``append_to_compiled_sources`` + ``_insert_under_sources`` + ``_HAS_FCNTL``
# are re-exported from _k6_source_append at the top of this module. The K6
# logic is owned by that lib (single source of truth shared with the
# ``memory-sources-append`` CLI).


# ----- Per-session processing -----

def process_session(
    raw_path: Path,
    *,
    date: str,
    session_id: str,
    extracted_dir: Path,
    compiled_dir: Path,
    alias_map: dict[str, str],
    system_prompt: str,
    args: argparse.Namespace,
) -> int:
    """Returns number of entries written for this session.

    K5 (alias-resolution): every tag in entry['topics'] is mapped through
    alias_map BEFORE the heading-block is formatted. Tags absent from the map
    pass through (genuinely new subjects). Modal tags are appended later by
    format_heading_block and are never alias-resolved.

    K6 (Sources-append): for each successfully-written entry, for each
    canonical tag in the (already alias-resolved) topics list, append a
    source-link to compiled/<canonical>.md ## Sources section if that file
    exists. Idempotent + best-effort: errors logged, never abort the session.
    """
    raw_text = raw_path.read_text(encoding="utf-8")
    validate_raw_schema(raw_text, raw_path)  # H5: raises RawSchemaError on FATAL
    raw_text = sanitize_harness_tags(raw_text)  # strip prompt-injection vectors
    if args.dry_run:
        print(f"  [dry-run] would call claude for {date}/{session_id}", file=sys.stderr)
        return 0
    response = call_claude(
        raw_text,
        model=args.model,
        system_prompt=system_prompt,
        max_budget_usd=args.max_budget_usd,
        fallback_model=args.fallback_model,
    )
    entries = response.get("entries", [])
    written = 0
    # Per-session batching: collect blocks per target, flush once at end of session.
    # Cuts atomic_write count from N entries to M unique target files (typically 1-3),
    # shrinking the Syncthing race-window between writes against the same file.
    blocks_by_target: dict[Path, list[str]] = {}
    for idx, entry in enumerate(entries):
        # Validate intent has modal
        if entry.get("type") == "intent" and not entry.get("modal"):
            print(f"  WARN: intent entry without modal in {session_id}, defaulting to speculative", file=sys.stderr)
            entry["modal"] = "speculative"
        violations = validate_entry(entry)
        if violations:
            print(f"  WARN: skipping entry {idx} in {session_id}: {'; '.join(violations)}", file=sys.stderr)
            continue
        # K5: alias-resolve topics in-place (safety-net for LLM not following vocab hint)
        if alias_map:
            entry["topics"] = [resolve_topic(t, alias_map) for t in entry["topics"]]
        q = quarter_for(entry["date"])
        target = ensure_quarter_file(extracted_dir, entry["type"], q)
        block = format_heading_block(entry, date=date, session_id=session_id)
        blocks_by_target.setdefault(target, []).append(block)
        written += 1
        # K6: Sources-append for each canonical tag with an existing compiled file
        source_link = f"- [[{date}/{session_id}]] - {entry['slug']}"
        seen_canonicals: set[str] = set()
        for tag in entry["topics"]:
            if not isinstance(tag, str) or not tag.startswith("¤"):
                continue
            canonical = tag[1:]
            if canonical in seen_canonicals:
                continue  # dedup if entry has same canonical twice (post alias-resolve)
            seen_canonicals.add(canonical)
            compiled_path = compiled_dir / f"{canonical}.md"
            status = append_to_compiled_sources(compiled_path, source_link)
            if status == "missing":
                continue  # no compiled file yet for this canonical - normal
            if status == "appended":
                if args.verbose:
                    print(f"    sources: appended to {compiled_path.name}", file=sys.stderr)
            elif status == "already-present":
                if args.verbose:
                    print(f"    sources: already present in {compiled_path.name}", file=sys.stderr)
            elif status.startswith("error:"):
                # Best-effort: log but don't fail the session - extracted/ is ground truth
                print(f"  WARN: sources-append {compiled_path.name}: {status}", file=sys.stderr)
    # Flush batched blocks - one atomic_write per target instead of one per entry
    for target, blocks in blocks_by_target.items():
        append_blocks(target, blocks)
    return written


# ----- Main orchestration -----

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault-root", type=Path, default=None,
                        help="Override vault root (default: env OBSIDIAN_VAULT_ROOT or platform default)")
    parser.add_argument("--model", default="sonnet",
                        help="Claude model alias or full id (default: sonnet)")
    parser.add_argument("--fallback-model", default=None)
    parser.add_argument("--max-budget-usd", type=float, default=None,
                        help="Per-invocation cost cap (passed to claude --max-budget-usd)")
    parser.add_argument("--system-prompt-file", type=Path,
                        default=SCRIPT_DIR / "system-prompt.md",
                        help="Path to system-prompt.md")
    parser.add_argument("--syncthing-url", default="https://localhost:8384",
                        help="Syncthing REST API base URL (default: https://localhost:8384)")
    parser.add_argument("--syncthing-folders", nargs="*", default=["obsidian"],
                        help="Folder ids to verify (default: obsidian)")
    parser.add_argument("--skip-syncthing-preflight", action="store_true",
                        help="Skip Syncthing REST-API pre-flight (manifest is primary)")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="Rebuild state from extracted/ source-refs (deterministic)")
    parser.add_argument("--force-fresh", action="store_true",
                        help="Ignore missing/corrupt state, process all raw-files (escape hatch)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N sessions (smoke-test / cost-limited runs)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover + pre-flight, no claude calls, no state writes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    vault = vault_root_from_args(args)
    memory_dir = vault / "8.Cortex" / "Memory"
    raw_root = memory_dir / "raw"
    extracted_dir = memory_dir / "extracted"
    compiled_dir = memory_dir / COMPILED_DIRNAME
    state_path = memory_dir / STATE_FILENAME

    if not args.system_prompt_file.exists():
        print(f"ERROR: system-prompt-file not found: {args.system_prompt_file}", file=sys.stderr)
        return 2
    system_prompt = args.system_prompt_file.read_text(encoding="utf-8")

    # ----- Aliases (K5: producer-side topic normalisation) -----
    # Failure semantics per foundry CONTRACT.md "Aliases-konsumering > Fail-modes":
    #   missing  -> graceful degrade + WARNING notify + exit 0
    #   empty    -> silent run without normalisation (valid pre-bootstrap state)
    #   corrupt / schema-mismatch -> hard fail exit 2 + (FATAL) notify
    alias_map: dict[str, str] = {}
    canonical_list: list[str] = []
    try:
        alias_map, canonical_list, alias_status = load_aliases(memory_dir)
    except AliasesError as e:
        notify(f"(FATAL) aliases.yaml unusable: {e}")
        return 2
    if alias_status == "missing":
        notify("aliases.yaml not present - extract running without canonical normalisation")
    elif alias_status == "empty":
        print("aliases: canonicals: {} - skipping vocab injection and alias mapping", file=sys.stderr)
    else:
        print(
            f"aliases: loaded {len(canonical_list)} canonical(s), "
            f"{len(alias_map) - len(canonical_list)} alias(es)",
            file=sys.stderr,
        )

    vocab_section = build_vocab_section(canonical_list)
    if vocab_section:
        system_prompt = system_prompt + vocab_section

    # ----- Pre-flight 1: manifest -----
    try:
        verified = verify_manifests(raw_root)
    except PreflightShaMismatchError as e:
        # Non-transient: manifest is stale (typically post-restore). Escalate to
        # run.sh via exit 3 so Telegram notifies and operator runs reconcile.
        print(f"preflight (manifest): {e}", file=sys.stderr)
        return 3
    except PreflightError as e:
        print(f"preflight (manifest): {e}", file=sys.stderr)
        return 0  # transient, retry next cron
    print(f"preflight (manifest): {len(verified)} date-dirs verified", file=sys.stderr)

    # ----- Pre-flight 2: Syncthing (optional) -----
    if not args.skip_syncthing_preflight:
        try:
            syncthing_preflight(
                args.syncthing_folders,
                args.syncthing_url,
                os.environ.get("SYNCTHING_API_KEY"),
            )
        except PreflightError as e:
            print(f"preflight (syncthing): {e}", file=sys.stderr)
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"preflight (syncthing): soft-fail on {e}, continuing", file=sys.stderr)

    # ----- Pre-flight 3: state-fil + mismatch -----
    state = load_state(state_path)
    if state == {}:  # corrupt
        if args.force_fresh:
            print("state corrupt - --force-fresh: starting fresh", file=sys.stderr)
            state = _fresh_state()
        elif args.force_rebuild:
            print("state corrupt - --force-rebuild: reconstructing from extracted/", file=sys.stderr)
            state = _fresh_state()
            for date, sid in extracted_processed_set(extracted_dir):
                state["processed_session_ids_by_date"].setdefault(date, []).append(sid)
        else:
            print("ERROR: state-fil corrupt or missing while raw/ has content. "
                  "Run with --force-rebuild to reconstruct from extracted/, or "
                  "--force-fresh to start over (will produce duplicates).",
                  file=sys.stderr)
            return 2

    state_set = state_processed_set(state)
    extracted_set = extracted_processed_set(extracted_dir)
    mismatch = extracted_set - state_set
    if mismatch:
        if args.force_rebuild:
            print(f"--force-rebuild: absorbing {len(mismatch)} mismatch entries from extracted/", file=sys.stderr)
            for date, sid in mismatch:
                state["processed_session_ids_by_date"].setdefault(date, []).append(sid)
            state_set = state_processed_set(state)
        elif args.force_fresh:
            print(f"--force-fresh: ignoring {len(mismatch)} mismatch entries (will produce duplicates)", file=sys.stderr)
        else:
            print(f"ERROR: state-vs-extracted mismatch: {len(mismatch)} session(s) appear in extracted/ "
                  f"but not in state. Likely mid-batch-crash or manual edit. "
                  f"Run with --force-rebuild to absorb.", file=sys.stderr)
            return 2

    # ----- Discover -----
    all_sessions = discover_raw_sessions(raw_root)
    pending = [(d, s, p) for (d, s, p) in all_sessions if (d, s) not in state_set]
    state["sessions_pending_count"] = len(pending)
    state["last_attempt_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"discover: {len(all_sessions)} total, {len(pending)} pending", file=sys.stderr)

    if args.limit:
        pending = pending[: args.limit]
        print(f"--limit: processing first {len(pending)}", file=sys.stderr)

    # ----- Process loop -----
    total_written = 0
    last_error = None
    for date, sid, raw_path in pending:
        if args.verbose:
            print(f"  process: {date}/{sid}", file=sys.stderr)
        try:
            n = process_session(
                raw_path,
                date=date,
                session_id=sid,
                extracted_dir=extracted_dir,
                compiled_dir=compiled_dir,
                alias_map=alias_map,
                system_prompt=system_prompt,
                args=args,
            )
        except RawSchemaError as e:
            # H5: FATAL exit 2. Schema-version mismatch is not a per-session
            # skip - if one raw-file is incompatible, others may be too, and
            # coordinated bump-policy requires explicit operator intervention.
            notify(f"(FATAL) raw schema validation: {e}")
            state["last_error"] = f"raw_schema: {e}"
            state["last_attempt_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            save_state(state_path, state)
            return 2
        except Exception as e:  # noqa: BLE001
            last_error = f"{date}/{sid}: {e}"
            print(f"  ERROR: {last_error}", file=sys.stderr)
            # Don't update state for failed sessions - retry next run
            continue

        if not args.dry_run:
            state["processed_session_ids_by_date"].setdefault(date, []).append(sid)
            state["sessions_pending_count"] = max(0, state["sessions_pending_count"] - 1)
            # Per-session last_success_at update. Without this, runs that end
            # via `timeout 30m` SIGTERM (rather than extract.py's clean-exit
            # block) leave last_success_at frozen at the previous clean run -
            # misleading the session-start staleness warning. Updating per
            # session reflects ground truth: this specific session was just
            # processed successfully.
            state["last_success_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            state["last_error"] = None
            save_state(state_path, state)  # atomic per session
        total_written += n
        if args.verbose:
            print(f"    wrote {n} entries", file=sys.stderr)

    # ----- Final state update -----
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["last_attempt_at"] = now
    if last_error is None:
        state["last_success_at"] = now
        state["last_error"] = None
    else:
        state["last_error"] = last_error
    state["last_run_produced_output"] = total_written > 0
    if not args.dry_run:
        save_state(state_path, state)

    print(
        f"extract: processed={len(pending)} entries-written={total_written} "
        f"errors={'1+' if last_error else '0'} "
        f"produced_output={state['last_run_produced_output']}",
        file=sys.stderr,
    )
    return 0 if last_error is None else 1


if __name__ == "__main__":
    sys.exit(main())
