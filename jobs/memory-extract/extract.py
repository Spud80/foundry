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
- Per claude call: one usage record (raw wrapper subset) appended to the
  invoker-designated JSONL spool for fleet-control-plane metering
  (`EXTRACT_USAGE_DIR` + `EXTRACT_RUN_CORRELATION`; disabled when unset).

Authoritative file format spec:
`cortex/docs/contracts/memory-knowledge-contract.md`.

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
import tempfile
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

# Aliases-loading lives in a shared helper so the CLI
# (`memory-sources-append`) and this extract-cron use the exact same
# load_aliases semantics. Sources-append was dropped in cortex-memory-v2
# (compile-pass owns sources; extract no longer mutates compiled/).
from _aliases import ALIASES_FILENAME, AliasesError, load_aliases

SCHEMA_VERSION = 1
TYPES = ("observation", "decision", "learning", "error", "pattern", "intent")
MANIFEST_FILENAME = "_capture-manifest.json"
STATE_FILENAME = ".compile-state.json"
# Raw-side schema floor (H5, 2026-05-14). Per memory-knowledge-contract.md
# raw-frontmatter contains `raw_schema_version: N`. Pre-H5 files carry only
# legacy `contract-version: 1`; we accept those as version-1-equivalent for
# backward-compat reads of the 4749+ historical raw-files. A future
# raw_schema_version > MIN aborts FATAL (exit 2) to force coordinated bump
# rather than silently feeding incompatible raw-format to the LLM.
MIN_RAW_SCHEMA_VERSION = 1
# An `incomplete` date-dir means "capture or sync still in flight" - transient by
# design, skipped quietly, retried next cron. That reading expires: after this
# many days nothing is in flight any more and the dir is stranded. Nothing else
# in the pipeline says so, because a stranded dir drops out of `verified_names`,
# its sessions stop counting toward the backlog, and only `mismatched` notified.
# Two days is two full capture+extract cycles (18:00 / 18:30 daily).
INCOMPLETE_NOTIFY_AGE_DAYS = 2
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
                    # Optional, additive: the slug of an earlier entry this one
                    # revises. Declared here because the model is constrained to
                    # this schema - a field absent from it can never be emitted.
                    # extracted/ stays on schema_version 1 (A4 permits additive
                    # optional fields).
                    "supersedes": {"type": "string"},
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


def _kebab(value: str) -> str:
    """Best-effort kebab-case: camelCase boundaries become hyphens, separators
    collapse, everything outside [a-z0-9-] is dropped. Returns "" when nothing
    usable remains - the caller keeps the original and lets validation reject it.
    """
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", value)
    s = re.sub(r"[\s_.]+", "-", s).lower()
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def normalize_entry(entry: dict) -> list[str]:
    """Repair deterministically-fixable deviations in-place; return a list of
    human-readable repairs (empty when nothing was touched).

    The model occasionally emits camelCase slugs and topics ("topicsToResearch-x")
    that validate_entry rejects. Dropping those entries loses real content for a
    formatting slip we can fix without asking anyone, so normalize first and let
    validation judge the result. Only shape is repaired, never meaning: a slug
    that normalizes to nothing is left alone and fails validation as before.
    """
    repairs: list[str] = []
    slug = entry.get("slug")
    if isinstance(slug, str) and not SLUG_RE.match(slug):
        fixed = _kebab(slug)
        if fixed and SLUG_RE.match(fixed):
            entry["slug"] = fixed
            repairs.append(f"slug {slug!r} -> {fixed!r}")
    topics = entry.get("topics")
    if isinstance(topics, list):
        for i, tag in enumerate(topics):
            if not isinstance(tag, str) or TOPIC_RE.match(tag):
                continue
            fixed = "¤" + _kebab(tag.lstrip("¤#"))
            if TOPIC_RE.match(fixed):
                topics[i] = fixed
                repairs.append(f"topics[{i}] {tag!r} -> {fixed!r}")
    return repairs


def quarantine_entry(
    extracted_dir: Path,
    *,
    date: str,
    session_id: str,
    entry: dict,
    violations: list[str],
) -> Path:
    """Append a rejected entry to extracted/.rejected/<date>-<session>.json.

    An entry that survives normalization but still violates the contract must not
    disappear silently - the session is marked processed either way, so a dropped
    entry would be unrecoverable without re-running the whole pipeline. The
    quarantine file holds the raw entry, so a replay needs no new model call.
    """
    rejected_dir = extracted_dir / ".rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    target = rejected_dir / f"{date}-{session_id}.json"
    records = []
    if target.exists():
        try:
            records = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            records = []
    records.append({
        "rejected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "violations": violations,
        "entry": entry,
    })
    atomic_write(target, json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    return target


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
    from _paths import resolve_vault_root
    return resolve_vault_root(args.vault_root)


def quarter_for(date_iso: str) -> str:
    """ '2026-05-08' -> '2026-Q2'. """
    y, m, _ = date_iso.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"{y}-Q{q}"


# ----- Notify helper (aliases-consumption/sources-append fail-mode signaling) -----

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


# ----- Aliases (aliases-consumption: producer-side topic normalisation) -----

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


# ----- Existing-slug injection (delta-writing as an instruction) -----

# How many recent slugs to show the model for one project. The join decides
# MEMBERSHIP, not volume: `claude-code-skills` alone holds 1721 entries, so "all
# slugs for this project" would recreate exactly the unmanageable prompt this is
# meant to avoid.
SLUG_INJECT_LIMIT = 100
# How far back to walk the corpus while building the index. Entries are visited
# newest-first, and each one may cost a raw-file open, so the walk is bounded:
# the newest few thousand entries cover every project that is actually active,
# and a project dormant beyond that has nothing worth showing anyway.
SLUG_SCAN_LIMIT = 2000

PROJECT_FM_RE = re.compile(r'^project:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)
# Frontmatter sits at the top; a raw file can be 200 KB and we need 12 lines.
FRONTMATTER_PROBE_BYTES = 2048


def raw_project(raw_path: Path, cache: dict[Path, str]) -> str:
    """The ``project:`` of a raw session file, or "" when absent/unreadable.

    Cached per run so each raw file is opened at most once.
    """
    if raw_path in cache:
        return cache[raw_path]
    project = ""
    try:
        with raw_path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(FRONTMATTER_PROBE_BYTES)
        m = PROJECT_FM_RE.search(head)
        if m:
            project = m.group(1).strip()
    except OSError:
        pass
    cache[raw_path] = project
    return project


def build_slug_index(memory_dir: Path, raw_root: Path) -> dict[str, list[str]]:
    """``{project: [recent entry slugs, newest first]}``.

    ``extracted/`` entries carry no project field, so the join goes through the
    one link they do carry: ``source:`` names the raw session file, and that file
    has ``project:`` in its frontmatter.

    Degrades to ``{}`` rather than failing - a missing index costs a prompt
    section, never a session.
    """
    try:
        from _extracted_entries import scan_extracted, wikilink_target
    except ImportError:
        return {}
    try:
        entries, _headers = scan_extracted(memory_dir / "extracted")
    except OSError:
        return {}
    entries.sort(key=lambda e: (e.date, e.order), reverse=True)

    index: dict[str, list[str]] = {}
    cache: dict[Path, str] = {}
    for e in entries[:SLUG_SCAN_LIMIT]:
        if not e.source:
            continue
        target = wikilink_target(e.source)
        if not target:
            continue
        project = raw_project(raw_root / f"{target}.md", cache)
        if not project:
            continue
        bucket = index.setdefault(project, [])
        if len(bucket) < SLUG_INJECT_LIMIT:
            bucket.append(e.slug)
    return index


def build_slug_section(slugs: list[str]) -> str:
    """Render the existing-entry block appended to the system prompt.

    This is what makes delta-writing an INSTRUCTION rather than a mechanism: the
    model is told what has already been captured for this project and asked to
    write only what is new. A miss costs one redundant entry - which read-time
    clustering turns into corroboration - where a write-time gate would have cost
    the insight itself, unrecoverably, since ``extracted/`` is not git-tracked.
    """
    if not slugs:
        return ""
    listed = "\n".join(f"- {s}" for s in slugs)
    return (
        "\n\n# Existing entries for this project\n\n"
        "These insights are already captured. Write what THIS session adds - a "
        "new angle, a correction, a measurement, a consequence - not a restatement "
        "of what is listed here.\n\n"
        "If this session shows one of them to be wrong or incomplete, write the "
        "corrected entry and set `supersedes` to that slug. Do not skip a genuine "
        "new insight because it sits near an existing one; overlap is fine, "
        "repetition of the same claim is not.\n\n"
        f"{listed}\n"
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


def atomic_write(target: Path, content: str, *, mode: int = 0o664) -> None:
    """Atomic write with explicit chmod (preserves POSIX ACL mask on filehub)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, target)
        try:
            os.chmod(target, mode)
        except OSError:
            pass  # Windows / non-POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ----- Manifest pre-flight -----

class PreflightError(Exception):
    """Raised for transient pre-flight failures. Caller exits 0 (retry next cron)."""


def incomplete_dir_is_aged(
    dir_name: str,
    today: date,
    max_age_days: int = INCOMPLETE_NOTIFY_AGE_DAYS,
) -> bool:
    """True when an `incomplete` date-dir is too old to still be in flight.

    Date-dirs are named ``YYYY-MM-DD``. A name that is not a date can never
    become fresh by waiting, so it is reported rather than hidden - a silently
    skipped dir is the failure mode this predicate exists to make visible.
    """
    try:
        dir_date = date.fromisoformat(dir_name)
    except ValueError:
        return True
    return (today - dir_date).days > max_age_days


def verify_manifests(
    raw_root: Path,
) -> tuple[list[Path], list[str], list[tuple[str, str]]]:
    """Classify every non-empty date-dir as verified / mismatched / incomplete.

    Returns (verified, mismatched, incomplete):
      - verified:   date-dirs whose manifest is present and every listed file's
                    sha256 matches on disk. Safe to extract.
      - mismatched: reasons for date-dirs with a sha256 mismatch (persistent
                    drift - typically post-restore or an external/Syncthing edit
                    of a raw file). SKIPPED until `reconcile-manifest.py --apply`.
      - incomplete: ``(date-dir name, reason)`` for date-dirs whose manifest is
                    missing/unparseable or lists a file not present locally
                    (capture/sync in flight). SKIPPED this run, retried next
                    cron. The name is returned alongside the reason so the
                    caller can age the dir without parsing it back out of prose.

    Graceful degradation (2026-06-09): a problem in ONE date-dir no longer aborts
    the whole run. This previously raised on the first sha mismatch -> exit 3 ->
    the entire extract pass skipped, so a single drifted historical file stalled
    the pipeline for days (50h incident 2026-06-06). Now only the offending
    date-dir is skipped; every clean dir extracts normally.
    """
    verified: list[Path] = []
    mismatched: list[str] = []
    incomplete: list[tuple[str, str]] = []
    if not raw_root.is_dir():
        return verified, mismatched, incomplete
    for date_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        md_files = sorted(date_dir.glob("*.md"))
        if not md_files:
            continue
        manifest_path = date_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            incomplete.append((
                date_dir.name,
                f"missing manifest for {date_dir.name} "
                f"({len(md_files)} raw-files present without manifest)",
            ))
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            listed = {entry["path"]: entry["sha256"] for entry in manifest.get("sessions", [])}
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            incomplete.append(
                (date_dir.name, f"manifest unparseable for {date_dir.name}: {e}")
            )
            continue

        # raw_root = .../8.Cortex/Memory/raw, rel_path = raw/<date>/<file>.md,
        # so the on-disk file is raw_root.parent / rel_path.
        problem: str | None = None
        problem_is_mismatch = False
        for rel_path, expected_sha in listed.items():
            local = raw_root.parent / rel_path
            if not local.exists():
                problem = (
                    f"manifest for {date_dir.name} lists {rel_path} "
                    f"but file is not present locally"
                )
                break
            actual = sha256_of(local)
            if actual != expected_sha:
                problem = (
                    f"sha256 mismatch on {rel_path} "
                    f"(manifest={expected_sha[:8]}.., actual={actual[:8]}..). "
                    f"Run reconcile-manifest.py --apply to fix."
                )
                problem_is_mismatch = True
                break
        if problem is None:
            verified.append(date_dir)
        elif problem_is_mismatch:
            mismatched.append(problem)
        else:
            incomplete.append((date_dir.name, problem))
    return verified, mismatched, incomplete


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
    """Scan extracted/*.md for processed raw-sessions.

    Counts both the per-entry ``source: [[<date>/<session-id>]]`` ref and any
    ``merged_sources: [[<date>/<session-id>]], ...`` refs left behind by the
    extracted-dedup sweep (reorganize-extracted-dedup.py). Counting
    merged_sources is what keeps ``--force-rebuild`` from re-extracting - and
    thereby re-duplicating - a raw-session whose entry was merged away.
    """
    out: set[tuple[str, str]] = set()
    if not extracted_dir.is_dir():
        return out
    pattern = re.compile(r"^source:\s*\[\[(\d{4}-\d{2}-\d{2})/([0-9a-f-]+)\]\]", re.M)
    merged_line = re.compile(r"^merged_sources:\s*(.+)$", re.M)
    sid_pat = re.compile(r"\[\[(\d{4}-\d{2}-\d{2})/([0-9a-f-]+)\]\]")
    for f in extracted_dir.glob("*.md"):
        text = f.read_text(encoding="utf-8")
        for date, sid in pattern.findall(text):
            out.add((date, sid))
        for line in merged_line.findall(text):
            for date, sid in sid_pat.findall(line):
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


# ----- Usage spool (fleet-control-plane metering) -----
#
# Whoever makes the claude call captures the usage: extract.py appends one
# JSONL record per `claude -p` call to a spool file the invoker designates.
# The record carries the RAW wrapper usage-subset - ledger normalisation is
# the control-plane's job (it owns ledger semantics), not cortex's. With the
# env contract absent the spool is disabled and extract.py runs standalone.

_CORRELATION_SAFE_RE = re.compile(r"[A-Za-z0-9._-]+")
USAGE_WRAPPER_FIELDS = ("modelUsage", "total_cost_usd", "subtype", "is_error")


def usage_spool_target() -> tuple[Path, str] | None:
    """Resolve spool destination from env, or None when disabled.

    EXTRACT_USAGE_DIR (spool directory) + EXTRACT_RUN_CORRELATION (run-level
    correlation id, filename-safe) are injected by the invoker (foundry
    run.sh / control-plane lane). Either absent/empty -> disabled.
    """
    spool_dir = os.environ.get("EXTRACT_USAGE_DIR", "").strip()
    correlation = os.environ.get("EXTRACT_RUN_CORRELATION", "").strip()
    if not spool_dir or not correlation:
        return None
    if not _CORRELATION_SAFE_RE.fullmatch(correlation):
        print(
            f"  WARN: EXTRACT_RUN_CORRELATION {correlation!r} is not filename-safe; "
            "usage-spool disabled for this run",
            file=sys.stderr,
        )
        return None
    return Path(spool_dir), correlation


def append_usage_record(
    spool_dir: Path,
    correlation: str,
    *,
    date: str,
    session_id: str,
    model: str,
    wrapper: dict,
) -> None:
    """Append one usage record (JSONL line) for a single claude call.

    Per-call correlation is `<run-correlation>#<session-id>` - the dedup key
    the control-plane ledger uses, so re-ingesting the same spool file never
    double-counts. One O_APPEND line-write per call; records are small enough
    that a torn write is not a practical concern, and the ledger side
    tolerates a trailing partial line.
    """
    record: dict = {
        "correlation": f"{correlation}#{session_id}",
        "date": date,
        "session_id": session_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "model_requested": model,
    }
    for key in USAGE_WRAPPER_FIELDS:
        if key in wrapper:
            record[key] = wrapper[key]
    spool_dir.mkdir(parents=True, exist_ok=True)
    with open(spool_dir / f"{correlation}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def make_usage_sink(
    *, date: str, session_id: str, model: str
) -> Callable[[dict], None] | None:
    """Build a per-session usage sink for call_claude, or None when disabled."""
    spool = usage_spool_target()
    if spool is None:
        return None
    spool_dir, correlation = spool

    def sink(wrapper: dict) -> None:
        append_usage_record(
            spool_dir, correlation,
            date=date, session_id=session_id, model=model, wrapper=wrapper,
        )

    return sink


class BudgetStop(Exception):
    """Raised when `claude -p` halted on its per-call --max-budget-usd backstop
    (subtype=error_max_budget_usd). NOT a failure: real tokens were spent (and are
    metered before this raise), but no usable output came back. The caller defers the
    session to the next run instead of counting it as an error - a budget stop firing is
    expected graceful degradation, not a broken run.
    """

    def __init__(self, cost_usd: object) -> None:
        super().__init__(f"per-call budget backstop hit (cost {cost_usd})")
        self.cost_usd = cost_usd


def _budget_stop_wrapper(stdout: str) -> dict | None:
    """Return the parsed result wrapper iff stdout is the structured error_max_budget_usd
    stop (the per-call --max-budget-usd backstop firing), else None. A budget stop exits
    non-zero but carries a full usage wrapper - distinct from a true failure
    (auth/oversize/api) whose stdout is empty or unparseable."""
    try:
        wrapper = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(wrapper, dict) and wrapper.get("subtype") == "error_max_budget_usd":
        return wrapper
    return None


def call_claude(
    raw_text: str,
    *,
    model: str,
    system_prompt: str,
    max_budget_usd: float | None,
    fallback_model: str | None,
    usage_sink: Callable[[dict], None] | None = None,
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
        # The per-call --max-budget-usd backstop exits non-zero but returns a full
        # result wrapper (subtype=error_max_budget_usd). That spend is real, so meter it
        # here (same as a clean call) and raise a budget-stop the caller defers - it is
        # NOT a failure. Metering must happen on this branch: it is the most-expensive
        # call of all (it ran until the cap), and the generic-error raise below would
        # otherwise drop exactly it from the ledger.
        budget_wrapper = _budget_stop_wrapper(proc.stdout)
        if budget_wrapper is not None:
            if usage_sink is not None:
                try:
                    usage_sink(budget_wrapper)
                except Exception as e:  # noqa: BLE001 - metering must never mask the stop
                    print(f"  WARN: usage-spool write failed: {e}", file=sys.stderr)
            raise BudgetStop(budget_wrapper.get("total_cost_usd"))
        # True failure: stdout often surfaces the actual error (e.g. "Prompt is too
        # long") with empty stderr, especially on api-errors.
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
    # Emit usage BEFORE structured-output extraction: even a response whose
    # payload turns out unusable has spent real tokens and must be metered.
    if isinstance(wrapper, dict) and usage_sink is not None:
        try:
            usage_sink(wrapper)
        except Exception as e:
            # Metering must never kill extraction; under-metering is visible in
            # the log and bounded by the invoker's budget reservation.
            print(f"  WARN: usage-spool write failed: {e}", file=sys.stderr)
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
    lines = [
        f"## {entry['date']} - {entry['slug']}",
        f"topics: {topics}",
        f"source: [[{date}/{session_id}]]",
    ]
    # Additive optional metadata line. Any consumer that strips entry metadata
    # must know it (`_extracted_entries.METADATA_PREFIXES`), or it leaks into
    # bodies.
    supersedes = (entry.get("supersedes") or "").strip()
    if supersedes:
        lines.append(f"supersedes: {supersedes}")
    return "\n".join(lines) + f"\n\n{body}\n\n"


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


# ----- Per-session processing -----

def process_session(
    raw_path: Path,
    *,
    date: str,
    session_id: str,
    extracted_dir: Path,
    alias_map: dict[str, str],
    system_prompt: str,
    args: argparse.Namespace,
    slug_index: dict[str, list[str]] | None = None,
    project_cache: dict[Path, str] | None = None,
) -> tuple[int, int]:
    """Returns (entries written, entries quarantined) for this session.

    aliases-consumption (alias-resolution): every tag in entry['topics'] is mapped through
    alias_map BEFORE the heading-block is formatted. Tags absent from the map
    pass through (genuinely new subjects). Modal tags are appended later by
    format_heading_block and are never alias-resolved.

    sources-append (Sources-append) was dropped in cortex-memory-v2: extract.py no longer
    mutates compiled/, sources are owned by compile-pass.
    """
    raw_text = raw_path.read_text(encoding="utf-8")
    validate_raw_schema(raw_text, raw_path)  # H5: raises RawSchemaError on FATAL
    raw_text = sanitize_harness_tags(raw_text)  # strip prompt-injection vectors
    # Existing-slug injection is per session, not per run: the block depends on
    # which project this session belongs to.
    if slug_index:
        project = raw_project(raw_path, project_cache if project_cache is not None else {})
        slug_section = build_slug_section(slug_index.get(project, []))
        if slug_section:
            system_prompt = system_prompt + slug_section
    if args.dry_run:
        print(f"  [dry-run] would call claude for {date}/{session_id}", file=sys.stderr)
        return 0, 0
    response = call_claude(
        raw_text,
        model=args.model,
        system_prompt=system_prompt,
        max_budget_usd=args.max_budget_usd,
        fallback_model=args.fallback_model,
        usage_sink=make_usage_sink(date=date, session_id=session_id, model=args.model),
    )
    entries = response.get("entries", [])
    written = 0
    rejected = 0
    # Per-session batching: collect blocks per target, flush once at end of session.
    # Cuts atomic_write count from N entries to M unique target files (typically 1-3),
    # shrinking the Syncthing race-window between writes against the same file.
    blocks_by_target: dict[Path, list[str]] = {}
    for idx, entry in enumerate(entries):
        # Validate intent has modal
        if entry.get("type") == "intent" and not entry.get("modal"):
            print(f"  WARN: intent entry without modal in {session_id}, defaulting to speculative", file=sys.stderr)
            entry["modal"] = "speculative"
        repairs = normalize_entry(entry)
        if repairs:
            print(f"  repaired entry {idx} in {session_id}: {'; '.join(repairs)}", file=sys.stderr)
        violations = validate_entry(entry)
        if violations:
            target = quarantine_entry(
                extracted_dir,
                date=date,
                session_id=session_id,
                entry=entry,
                violations=violations,
            )
            print(
                f"  WARN: quarantined entry {idx} in {session_id}: {'; '.join(violations)} "
                f"-> {target.name}",
                file=sys.stderr,
            )
            rejected += 1
            continue
        # aliases-consumption: alias-resolve topics in-place (safety-net for LLM not following vocab hint)
        if alias_map:
            entry["topics"] = [resolve_topic(t, alias_map) for t in entry["topics"]]
        q = quarter_for(entry["date"])
        target = ensure_quarter_file(extracted_dir, entry["type"], q)
        block = format_heading_block(entry, date=date, session_id=session_id)
        blocks_by_target.setdefault(target, []).append(block)
        written += 1
    # Flush batched blocks - one atomic_write per target instead of one per entry
    for target, blocks in blocks_by_target.items():
        append_blocks(target, blocks)
    return written, rejected


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
    parser.add_argument("--backlog-alert-threshold", type=int, default=None,
                        help="If sessions still pending after this run exceeds N, emit a "
                             "backlog-depth NOTIFY (inflow outpacing throughput). Off if unset.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover + pre-flight, no claude calls, no state writes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    vault = vault_root_from_args(args)
    memory_dir = vault / "8.Cortex" / "Memory"
    raw_root = memory_dir / "raw"
    extracted_dir = memory_dir / "extracted"
    state_path = memory_dir / STATE_FILENAME

    if not args.system_prompt_file.exists():
        print(f"ERROR: system-prompt-file not found: {args.system_prompt_file}", file=sys.stderr)
        return 2
    system_prompt = args.system_prompt_file.read_text(encoding="utf-8")

    # ----- Aliases (aliases-consumption: producer-side topic normalisation) -----
    # Failure semantics per foundry CONTRACT.md "Aliases-konsumering > Fail-modes":
    #   missing  -> graceful degrade + WARNING notify + exit 0
    #   empty    -> silent run without normalisation (valid pre-bootstrap state)
    #   corrupt / schema-mismatch -> hard fail exit 2 + (FATAL) notify
    # strict=True: the contract above requires exit 2 on corrupt/schema-mismatch,
    # so this caller must keep raising where the nightly detectors degrade.
    try:
        aliases = load_aliases(memory_dir / ALIASES_FILENAME, strict=True)
    except AliasesError as e:
        notify(f"(FATAL) aliases.yaml unusable: {e}")
        return 2
    alias_map = aliases.alias_to_canonical
    canonical_list = aliases.canonicals()
    alias_status = aliases.status
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

    # Built once per run, consumed per session (the block is project-scoped).
    slug_index = build_slug_index(memory_dir, raw_root)
    project_cache: dict[Path, str] = {}
    if slug_index:
        print(f"slug-injection: {len(slug_index)} project(s) indexed", file=sys.stderr)

    # ----- Pre-flight 1: manifest (graceful per-dir degradation) -----
    # A problem in one date-dir skips ONLY that dir; all clean dirs still extract.
    verified, mismatched, incomplete = verify_manifests(raw_root)
    today = datetime.now(timezone.utc).date()
    for dir_name, reason in incomplete:
        # Transient (capture/sync in flight) - skip quietly, retried next cron.
        print(f"preflight (manifest): skip dir - {reason}", file=sys.stderr)
        if incomplete_dir_is_aged(dir_name, today):
            # No longer plausibly in flight. Nothing downstream reports this:
            # the dir is out of verified_names and its sessions are out of the
            # backlog, so without a notify a stranded dir is indistinguishable
            # from a healthy one.
            notify(
                f"preflight-degraded: date-dir stranded "
                f"{INCOMPLETE_NOTIFY_AGE_DAYS}+ days (incomplete manifest) - {reason}"
            )
    for reason in mismatched:
        # Persistent drift - skip the dir but notify so the operator reconciles.
        print(f"preflight (manifest): skip dir - {reason}", file=sys.stderr)
        notify(f"preflight-degraded: date-dir skipped (sha mismatch) - {reason}")
    verified_names = {d.name for d in verified}
    print(
        f"preflight (manifest): {len(verified)} verified, "
        f"{len(mismatched)} sha-mismatch skipped, {len(incomplete)} incomplete skipped",
        file=sys.stderr,
    )

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
    # Only sessions in manifest-verified date-dirs are eligible; sessions in
    # skipped (mismatched/incomplete) dirs are deferred until their manifest is clean.
    pending = [
        (d, s, p) for (d, s, p) in all_sessions
        if (d, s) not in state_set and d in verified_names
    ]
    state["sessions_pending_count"] = len(pending)
    state["last_attempt_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"discover: {len(all_sessions)} total, {len(pending)} pending", file=sys.stderr)

    if args.limit:
        pending = pending[: args.limit]
        print(f"--limit: processing first {len(pending)}", file=sys.stderr)

    # ----- Process loop -----
    total_written = 0
    total_rejected = 0
    last_error = None
    budget_skipped: list[tuple[str, str]] = []
    for date, sid, raw_path in pending:
        if args.verbose:
            print(f"  process: {date}/{sid}", file=sys.stderr)
        try:
            n, rejected = process_session(
                raw_path,
                date=date,
                session_id=sid,
                extracted_dir=extracted_dir,
                alias_map=alias_map,
                system_prompt=system_prompt,
                args=args,
                slug_index=slug_index,
                project_cache=project_cache,
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
        except BudgetStop as e:
            # The per-call budget backstop fired: spend is metered (in call_claude), but
            # no output was produced. Defer, do not fail - the session stays pending
            # (state untouched) and is retried next run, and the run does not error on it.
            # Conflating this graceful stop with a true error is what tripped the
            # memory-state alarm on an otherwise-successful run.
            budget_skipped.append((date, sid))
            print(
                f"  budget-skip: {date}/{sid} deferred (cost {e.cost_usd}); retry next run",
                file=sys.stderr,
            )
            continue
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
        total_rejected += rejected
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

    # Budget-skips are deferrals, not failures (they do not set last_error or exit 1), but
    # a persistent skip means a single compile no longer fits the per-call cap - surface it
    # once per run so the operator can raise the cap rather than let the backlog grow silently.
    if budget_skipped:
        notify(
            f"budget-skip: {len(budget_skipped)} session(s) hit the per-call "
            f"--max-budget-usd cap ({args.max_budget_usd}) and were deferred; spend was "
            f"metered, sessions retry next run. Raise the per-call cap if this persists."
        )

    # Backlog-depth alert: even after processing this run, more than the threshold
    # remains pending - inflow is outpacing throughput and the queue will not drain
    # on the daily cadence. state["sessions_pending_count"] is the true remainder
    # (total discovered minus sessions marked done this run). Skipped under --dry-run,
    # where nothing is decremented and the count always == total (false positive).
    remaining = state["sessions_pending_count"]
    if (args.backlog_alert_threshold is not None
            and not args.dry_run
            and remaining > args.backlog_alert_threshold):
        notify(
            f"backlog-depth: {remaining} session(s) still pending after run "
            f"(processed {len(pending)}, limit {args.limit}, "
            f"threshold {args.backlog_alert_threshold}). Inflow is outpacing "
            f"throughput - raise --limit or run a one-off burndown."
        )

    # Quarantined entries are content the pipeline could not write but did not lose:
    # the session is marked processed regardless, so silence here would mean the
    # operator never learns that something needs a replay from extracted/.rejected/.
    if total_rejected:
        notify(
            f"rejected-entries: {total_rejected} entry/entries failed validation after "
            f"normalization and were quarantined in extracted/.rejected/. The sessions "
            f"are marked processed - replay from the quarantine files, they hold the raw "
            f"entries and need no new model call."
        )

    print(
        f"extract: processed={len(pending)} entries-written={total_written} "
        f"errors={'1+' if last_error else '0'} "
        f"budget-skipped={len(budget_skipped)} "
        f"rejected={total_rejected} "
        f"produced_output={state['last_run_produced_output']}",
        file=sys.stderr,
    )
    return 0 if last_error is None else 1


if __name__ == "__main__":
    sys.exit(main())
