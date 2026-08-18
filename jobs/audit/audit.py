#!/usr/bin/env python3
"""audit.py - foundry-audit nightly vault audit-pass.

Scans the Obsidian vault per `cortex/docs/contracts/audit-pass-spec.md`
(schema_version 1), runs six audit-checks, writes a daily report-file to
`5.Utility/Pipeline/Audit-Reports/YYYY-MM-DD.md`, updates heartbeat-state at
`~/.audit-state.json`, and emits tiered Telegram alerts.

Spec authority:
  cortex/docs/contracts/audit-pass-spec.md
  jobs/audit/CONTRACT.md (runtime interface)

Exit-codes:
  0    OK (with or without findings)
  1    unexpected runtime-error
  2    schema-mismatch on runner config (cannot proceed safely)
  124  vault unavailable (Syncthing-mount missing or not readable)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import traceback
import uuid
from pathlib import Path

import yaml

from _capture_declare import declare_capture

# ---------- Constants ----------

JOB_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = JOB_DIR / "system-prompt.md"
RUNNER_VERSION = "1.0.0"
RUNNER_SCHEMA_VERSION = 1

# `reference` is Quick-Capture/audio-only (mobile/web thought-pipeline), added
# as the 9th capture value in capture-vocabulary.md 2026-06-25. /save never emits
# it; included here so the audit does not flag thought-pipeline reference entries.
CAPTURE_ENUM = {"idea", "quote", "book", "movie", "tv_series", "podcast", "person", "reference", "note"}
INTENT_ENUM = {"followup", "reminder", "someday", "question", "decision", None}
STATUS_ENUM = {"active", "snoozed", "superseded", "done", "archived"}

# Raw-root contract. The declared home for where the raw session corpus lives,
# and for what the lookup is called, is cortex/scripts/memory/_paths.py. This
# job cannot import it: jobs/audit/ is a standalone job-dir with no shared
# module, foundry has no precedent for importing between job-dirs, and a fourth
# vendored file copy is the expensive way to spell two constants. So it reads
# the same env var and falls back to the same segments - both pinned by
# cortex/tests/memory/raw-root-parity-fixtures.json, checked from this side in
# _test/smoke_phase_1000.py.
#
# This job is why the check exists. It builds the index every `source_session:`
# is validated against, so a root pointing at nothing does not degrade - it
# reports every note with `source: ai-session` as `kritisk`, i.e. the whole of
# 2.Resources/Notes/ on the first 04:00 pass after a cutover.
RAW_ROOT_ENV = "CORTEX_RAW_ROOT"
RAW_SUBDIR = ("8.Cortex", "Memory", "raw")


class RawRootUnavailable(RuntimeError):
    """The configured raw session corpus is not there."""


def raw_root_for(vault_root: Path) -> Path:
    """The raw session corpus: $CORTEX_RAW_ROOT, else the historical location."""
    env = os.environ.get(RAW_ROOT_ENV) or None
    return Path(env) if env else vault_root.joinpath(*RAW_SUBDIR)


HARD_REQUIRED_FULL = [
    "title", "created", "updated", "capture", "intent", "source",
    "source_session", "user_id", "scope", "dedup_hash",
    "processing_state", "pre_classified",
]
MINIMUM_BASELINE = ["title", "created", "source", "pre_classified"]

# Multi-field signature for pipeline-emitted entries. All four fields are
# hard-required by capture-vocabulary.md schema_version 1 for ANY thought-
# pipeline emitter (/save, mobile, web, foundry-fallback). Manual user
# notes (Books, Ideas, Quotes, Reference, etc.) lack at least one of these
# even when they reuse overlapping field-names from user templates.
#
# Multi-field gate (vs single-field) is robust against partial reuse: any
# single field could collide with a future pipeline that emits it with
# different semantics (e.g. vault-sentinel could add dedup_hash for
# URL-hashing). Requiring the full schema-signature avoids false-positives.
#
# See audit-pass-spec.md "Pipeline-emitted vs manual notes" for rationale.
PIPELINE_MARKERS = ("dedup_hash", "pre_classified", "user_id", "scope")


def is_pipeline_entry(fm: dict) -> bool:
    """True iff frontmatter carries the full pipeline schema-signature.

    Audit checks 2 (schema), 3 (wikilinks), 5 (tags), 6 (missing-required)
    enforce pipeline-schema and apply ONLY to pipeline-emitted entries.
    Checks 1 (dedup) and 4 (sampling) are inherently pipeline-scoped via
    their own filters (dedup_hash presence; source=ai-session).
    """
    return all(k in fm for k in PIPELINE_MARKERS)

FORBIDDEN_GROWTH_EMOJI = {"📝", "🌱", "🌿", "🌲"}
FORBIDDEN_STATUS_EMOJI = {"🟥", "🟧", "🟨", "🟪", "🟩"}
REQUIRED_TAGS = ["📥", "💭"]
CAPTURE_REQUIRES_CREATOR = {"book", "movie", "tv_series", "podcast", "person"}

CLAUDE_PER_FILE_TIMEOUT = int(os.environ.get("AUDIT_CLAUDE_TIMEOUT_SECONDS", "300"))
CLAUDE_HARD_CAP = 50  # per audit-pass-spec.md "Resource budget"
SAMPLING_RATE = 0.10
SAMPLING_MIN_FRESH = 5
SAMPLING_FRESH_DAYS = 7
# Structural divergence threshold: applies to capture+intent disagreement only.
# Topics-divergence is reported as informational (sevarity-neutral) since LLM
# free-form topic-tag selection has high inherent variance even at temperature=0.
# See audit-pass-spec.md "Sampling-classification: structural vs topics axes".
DIVERGENCE_THRESHOLD = 0.20
# Topics-Jaccard threshold: stored.topics vs audit.topics count as "agree" when
# |stored ∩ audit| / |stored ∪ audit| >= this. Empty-empty is perfect (1.0).
TOPICS_JACCARD_MATCH = 0.5

FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
SOURCE_SESSION_RAW_RE = re.compile(r"^raw/(\d{4}-\d{2}-\d{2})/([^/]+)$")
SYNC_CONFLICT_RE = re.compile(r"\.sync-conflict-")

# ---------- Notify ----------

def notify(msg: str) -> None:
    """Send Telegram alert via FOUNDRY_NOTIFY_SH. Best-effort, never raises."""
    notify_path = os.environ.get("FOUNDRY_NOTIFY_SH")
    if not notify_path or not Path(notify_path).exists():
        print(f"NOTIFY-skipped (no FOUNDRY_NOTIFY_SH): {msg}", file=sys.stderr)
        return
    try:
        subprocess.run([notify_path, f"audit: {msg}"], timeout=10, check=False)
    except Exception as e:
        print(f"NOTIFY-subprocess-failed: {e}", file=sys.stderr)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


# ---------- Frontmatter parsing ----------

def load_frontmatter(path: Path) -> tuple[dict, str] | tuple[None, str]:
    """Return (frontmatter-dict, body-string). On YAML parse-error or missing
    frontmatter, return (None, error-msg) - caller registers as schema-violation."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"read error: {e}"
    m = FM_RE.match(text)
    if not m:
        return None, "no frontmatter block"
    fm_yaml, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_yaml) or {}
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"
    if not isinstance(fm, dict):
        return None, "frontmatter is not a mapping"
    return fm, body


def atomic_write(path: Path, content: str) -> None:
    """Write content to path via tempfile + os.replace. Tempfile pattern
    .tmp-audit-* must be in Syncthing .stignore to avoid in-flight propagation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp-audit-{uuid.uuid4().hex}.md"
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomic write of JSON state-file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# ---------- Scope/glob ----------

def is_excluded(rel_path: Path) -> bool:
    """Apply audit-pass-spec.md exclusion rules."""
    parts = rel_path.parts
    if any(p in {"_test", "archive", "templates"} for p in parts):
        return True
    if rel_path.name.startswith("_"):
        return True
    if SYNC_CONFLICT_RE.search(rel_path.name):
        return True
    return False


def collect_entries(vault_root: Path) -> dict:
    """Return dict with collected entry-paths grouped by area:
    {
      'inbox': [Path, ...],         # 1.Inbox/*.md (capture-pipeline files)
      'notes': [Path, ...],         # 2.Resources/Notes/**/*.md
      'raw_index': {(date, sid): Path},  # <raw-root>/<date>/<sid>.md index for wikilink-validation
    }
    """
    inbox_dir = vault_root / "1.Inbox"
    notes_dir = vault_root / "2.Resources" / "Notes"
    raw_dir = raw_root_for(vault_root)

    inbox: list[Path] = []
    if inbox_dir.is_dir():
        for p in sorted(inbox_dir.glob("*.md")):
            rel = p.relative_to(vault_root)
            if is_excluded(rel):
                continue
            name = p.name
            if not (name.startswith("ai-capture-") or name.startswith("pending-foundry-")):
                continue
            inbox.append(p)

    notes: list[Path] = []
    if notes_dir.is_dir():
        for p in sorted(notes_dir.rglob("*.md")):
            rel = p.relative_to(vault_root)
            if is_excluded(rel):
                continue
            notes.append(p)

    raw_index: dict[tuple[str, str], Path] = {}
    if not raw_dir.is_dir():
        # An empty index is not a benign default here: check_wikilinks() reads
        # a miss as a broken source_session, so "root not found" would be
        # published as corpus-wide corruption. Refuse the pass instead.
        raise RawRootUnavailable(
            f"raw root not found: {raw_dir} -- set {RAW_ROOT_ENV} to the "
            f"session corpus. Continuing would report every ai-session note "
            f"as kritisk."
        )
    if raw_dir.is_dir():
        for p in raw_dir.rglob("*.md"):
            try:
                rel = p.relative_to(raw_dir)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) != 2:
                continue
            date_part, fname = parts
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_part):
                continue
            sid = fname[:-3] if fname.endswith(".md") else fname
            raw_index[(date_part, sid)] = p

    return {"inbox": inbox, "notes": notes, "raw_index": raw_index}


def build_link_index(vault_root: Path) -> dict[str, list[Path]]:
    """Build an index of stem -> [matching paths] for shortest-unique-path
    wikilink resolution. Used by check 3."""
    index: dict[str, list[Path]] = {}
    for p in vault_root.rglob("*.md"):
        if any(part in {".git", "_test", "archive"} for part in p.parts):
            continue
        stem = p.stem
        index.setdefault(stem, []).append(p)
    return index


def resolve_wikilink(target: str, link_index: dict[str, list[Path]], vault_root: Path) -> bool:
    """Resolve an Obsidian wikilink target. Strip alias-segment (after '|') and
    heading-anchor (after '#'). Return True if any matching file exists in vault."""
    # Strip alias and heading.
    raw = target.split("|", 1)[0]
    raw = raw.split("#", 1)[0]
    raw = raw.strip()
    if not raw:
        return False
    # Path-form (contains /): resolve relative to vault-root.
    if "/" in raw:
        candidate = vault_root / (raw if raw.endswith(".md") else raw + ".md")
        return candidate.exists()
    # Stem-form: look up in link-index.
    matches = link_index.get(raw, [])
    return len(matches) > 0


# ---------- Check 1: Dedup-verification ----------

def check_dedup(notes: list[Path], vault_root: Path) -> dict:
    """Return findings dict: {'count': int, 'collisions': [collision_records]}."""
    groups: dict[str, list[tuple[Path, dict]]] = {}
    for p in notes:
        fm, _ = load_frontmatter(p)
        if fm is None or "dedup_hash" not in fm:
            continue
        h = str(fm["dedup_hash"])
        groups.setdefault(h, []).append((p, fm))

    collisions = []
    for h, members in groups.items():
        if len(members) <= 1:
            continue
        sorted_members = sorted(members, key=lambda m: str(m[0]))
        record = {
            "dedup_hash_prefix": h[:16],
            "entries": [
                {
                    "path": str(member[0].relative_to(vault_root)),
                    "created": str(member[1].get("created", "")),
                    "capture": str(member[1].get("capture", "")),
                    "status": str(member[1].get("status", "")),
                }
                for member in sorted_members
            ],
        }
        collisions.append(record)
    collisions.sort(key=lambda c: c["dedup_hash_prefix"])
    return {"count": len(collisions), "collisions": collisions}


# ---------- Check 2: Schema-compliance ----------

KNOWN_FIELDS = {
    "title", "created", "updated", "capture", "intent", "status", "due",
    "source", "source_session", "source_url", "user_id", "scope", "dedup_hash",
    "processing_state", "pre_classified", "tags", "topics", "snooze_until",
    "notify", "links", "related", "plan", "action", "creator", "year", "genre",
    "attribution", "schema_version", "foundry_pending",
    # Pipeline-emitted optional fields (capture-vocabulary.md schema_version 1):
    # - session_id_source: marker from /save skill when $CLAUDE_SESSION_ID
    #   env-var was missing and a synthetic UUID was generated as fallback.
    #   Audit check 3 skips source_session wikilink-resolution for these.
    # - foundry_pending_reason: inbox-handler routing rationale when an entry
    #   was renamed to pending-foundry-* (e.g. "missing intent",
    #   "pre_classified='partial' (only 'full' is direct-routable)").
    "session_id_source", "foundry_pending_reason",
}


def check_schema(entries: list[Path], vault_root: Path) -> dict:
    """Validate entries against schema_version 1 hard-required and detect
    deprecated/legacy patterns. Returns findings grouped by severity."""
    violations: list[dict] = []
    for p in entries:
        fm, parse_err = load_frontmatter(p)
        rel = str(p.relative_to(vault_root))
        if fm is None:
            violations.append({
                "path": rel,
                "type": "parse_error",
                "severity": "hoy",
                "detail": parse_err,
            })
            continue

        # Manual notes (missing one or more pipeline-markers) follow user's
        # own conventions and are not audited against pipeline-schema.
        # parse_error already caught above applies to all files.
        if not is_pipeline_entry(fm):
            continue

        schema_ver = fm.get("schema_version", 1)
        if isinstance(schema_ver, int) and schema_ver > RUNNER_SCHEMA_VERSION:
            violations.append({
                "path": rel,
                "type": "schema_version_mismatch",
                "severity": "kritisk",
                "detail": f"entry declares schema_version={schema_ver}, runner supports {RUNNER_SCHEMA_VERSION}",
            })
            continue

        pre = fm.get("pre_classified")
        if pre == "full":
            for k in HARD_REQUIRED_FULL:
                if k not in fm:
                    violations.append({
                        "path": rel,
                        "type": "missing_hard_required",
                        "severity": "hoy",
                        "detail": f"pre_classified=full but missing field {k!r}",
                    })
        else:
            for k in MINIMUM_BASELINE:
                if k not in fm:
                    violations.append({
                        "path": rel,
                        "type": "missing_baseline",
                        "severity": "hoy",
                        "detail": f"pre_classified={pre!r} but missing baseline field {k!r}",
                    })
            if fm.get("source") == "ai-session" and "source_session" not in fm:
                # Include session_id_source context if present - helps operator
                # distinguish "missing because emitter has bug" from "missing
                # because fallback-uuid path was used but emitter forgot to
                # write the placeholder wikilink" (latter is a clear emitter-bug).
                sid_src = fm.get("session_id_source")
                hint = (
                    f" (note: session_id_source={sid_src!r} present)"
                    if sid_src else ""
                )
                violations.append({
                    "path": rel,
                    "type": "missing_baseline",
                    "severity": "hoy",
                    "detail": f"source=ai-session but source_session missing{hint}",
                })

        unknown = set(fm.keys()) - KNOWN_FIELDS
        for u in sorted(unknown):
            violations.append({
                "path": rel,
                "type": "unknown_field",
                "severity": "lav",
                "detail": f"unknown field {u!r}",
            })

        tags = fm.get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                if t in FORBIDDEN_STATUS_EMOJI:
                    violations.append({
                        "path": rel,
                        "type": "legacy_status_emoji",
                        "severity": "hoy",
                        "detail": f"legacy Status-emoji {t!r} in tags (Phase 800 regression)",
                    })

        src = fm.get("source")
        if isinstance(src, str) and (src.startswith("http://") or src.startswith("https://")):
            violations.append({
                "path": rel,
                "type": "url_in_source",
                "severity": "hoy",
                "detail": f"source contains URL {src!r} (Phase 800 regression; URLs belong in source_url)",
            })

    violations.sort(key=lambda v: (v["path"], v["type"]))
    by_severity = {"kritisk": 0, "hoy": 0, "lav": 0}
    for v in violations:
        by_severity[v["severity"]] += 1
    return {"count": len(violations), "violations": violations, "by_severity": by_severity}


# ---------- Check 3: Wikilink-validation ----------

def check_wikilinks(
    entries: list[Path],
    raw_index: dict[tuple[str, str], Path],
    link_index: dict[str, list[Path]],
    vault_root: Path,
) -> dict:
    """Validate source_session, links[], related[] for every entry."""
    broken: list[dict] = []
    for p in entries:
        fm, _ = load_frontmatter(p)
        if fm is None:
            continue
        # Manual notes don't carry source_session/links/related from pipeline
        # conventions; skip wikilink-validation for them.
        if not is_pipeline_entry(fm):
            continue
        rel = str(p.relative_to(vault_root))

        source = fm.get("source")
        source_session = fm.get("source_session")
        if source_session:
            # Skip source_session wikilink resolution when session_id_source is
            # fallback-uuid: the /save skill generates a synthetic UUID when
            # $CLAUDE_SESSION_ID was not exposed (headless invocations), so no
            # raw file is expected to exist at the wikilink target by design.
            # Field is informational; flagging would be noise. See
            # capture-vocabulary.md "session_id_source" for the convention.
            sid_source = str(fm.get("session_id_source", "")).strip()
            if sid_source != "fallback-uuid":
                target = str(source_session).strip()
                m = WIKILINK_RE.match(target) or WIKILINK_RE.search(target)
                inner = m.group(1) if m else target
                raw_m = SOURCE_SESSION_RAW_RE.match(inner)
                if raw_m:
                    key = (raw_m.group(1), raw_m.group(2))
                    exists = key in raw_index
                else:
                    exists = resolve_wikilink(inner, link_index, vault_root)
                if not exists:
                    severity = "kritisk" if source == "ai-session" else "hoy"
                    broken.append({
                        "path": rel,
                        "field": "source_session",
                        "target": inner,
                        "severity": severity,
                    })

        for field in ("links", "related"):
            val = fm.get(field)
            if not isinstance(val, list):
                continue
            for entry_val in val:
                if not isinstance(entry_val, str):
                    continue
                m = WIKILINK_RE.search(entry_val)
                inner = m.group(1) if m else entry_val
                if not resolve_wikilink(inner, link_index, vault_root):
                    broken.append({
                        "path": rel,
                        "field": field,
                        "target": inner,
                        "severity": "lav",
                    })

    broken.sort(key=lambda b: (b["severity"], b["path"], b["field"], b["target"]))
    by_severity = {"kritisk": 0, "hoy": 0, "lav": 0}
    for b in broken:
        by_severity[b["severity"]] += 1
    return {"count": len(broken), "broken": broken, "by_severity": by_severity}


# ---------- Check 4: Sampling-classification (LLM) ----------

def _strip_md_json_fence(text: str) -> str:
    """Strip Markdown ```json ... ``` wrapper that LLMs sometimes add to JSON
    output despite the prompt asking for raw JSON. No-op when no fence detected.
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


def call_claude_classify(body: str) -> dict:
    """Invoke `claude -p` with audit system-prompt; return parsed {capture, intent, topics}.

    Raises RuntimeError on subprocess-failure, TimeoutExpired on per-file timeout.
    """
    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    cmd = [
        "claude", "-p",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", system_prompt,
        "--input-format", "text",
    ]
    proc = subprocess.run(
        cmd,
        # Declared, not guessed - see _capture_declare.py. `body` is somebody
        # else's note being classified, so the marker must come from here and
        # never from the content.
        input=declare_capture(body, "foundry", "audit-pass"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CLAUDE_PER_FILE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exit {proc.returncode}: "
            f"stderr={proc.stderr[:300]!r} stdout={proc.stdout[:300]!r}"
        )
    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude stdout not JSON: {e}; raw[:200]={proc.stdout[:200]}")
    if isinstance(wrapper, dict) and isinstance(wrapper.get("structured_output"), dict):
        return wrapper["structured_output"]
    if isinstance(wrapper, dict) and "result" in wrapper:
        result_text = wrapper["result"]
    else:
        result_text = proc.stdout
    result_text = _strip_md_json_fence(result_text)
    try:
        return json.loads(result_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"audit response not JSON: {e}; first-200={result_text[:200]}")


def topics_jaccard(a: set, b: set) -> float:
    """Jaccard similarity for topic-sets. Empty-empty is perfect match (1.0)."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def topics_agree(stored: set, audit: set) -> bool:
    """Hybrid topic-set agreement model.

    Short topic-lists (<=2 on either side) suffer outsized Jaccard penalty for
    single swaps (e.g. [a,b] vs [a,c] -> Jaccard 0.33, below 0.5 threshold).
    For short lists, use absolute-overlap: agree if >=1 topic in common (both
    non-empty) OR both empty. Longer lists use the Jaccard threshold.

    Rationale: short topic-lists naturally have higher per-element variance
    impact. LLM re-classification picking 1-of-2 different topics doesn't
    indicate classifier-drift, just topic-vocabulary granularity choice.
    """
    if not stored and not audit:
        return True
    short = max(len(stored), len(audit)) <= 2
    if short:
        # Both non-empty: at least one common topic.
        # One empty + one non-empty: no overlap possible -> disagree.
        return bool(stored & audit)
    return topics_jaccard(stored, audit) >= TOPICS_JACCARD_MATCH


def _axis_disagreement(stored, audit):
    """Compute weighted disagreement for a single classification axis.

    Returns (weight, kind) tuple, or None for agreement.

    Per audit-pass-spec.md §4 asymmetric null-handling:
    - "abstain" (exactly one side is None): weight 0.5
    - "drift" (both non-None but different values): weight 1.0
    - Equal values (including both None): None returned (not a disagreement)

    Rationale: when audit-Claude returns None for intent while capture-Claude
    committed (or vice versa), the two are not in genuine disagreement on the
    enum value; one classifier declined to commit. Counting this at full
    weight inflates structural divergence with confidence-mismatch noise.
    """
    if stored == audit:
        return None
    is_abstain = (stored is None) != (audit is None)
    return (0.5, "abstain") if is_abstain else (1.0, "drift")


def check_sampling(notes: list[Path], vault_root: Path, today: dt.date, skip_llm: bool = False) -> dict:
    """Sample 10% of fresh ai-session entries (last 7 days); independent re-classify
    via claude -p; report per-axis divergence with topics demoted to informational.

    Tier-driver is STRUCTURAL divergence (capture + intent), which represents
    actual classifier-drift on the schema-enforced enum axes. Topics divergence
    is reported separately as informational since LLM free-form topic-tag
    selection has high inherent variance even at temperature=0 (different
    runs pick rimelig-but-different subsets of the vocabulary). Topics use
    Jaccard similarity with TOPICS_JACCARD_MATCH threshold rather than
    exact-set-equality.

    Structural disagreements use asymmetric null-handling (see _axis_disagreement):
    one-side-None counts as 0.5 (abstain), both-non-None-different as 1.0 (drift).
    capture_disagreements / intent_disagreements are weighted floats; drift/abstain
    breakdowns are reported as separate integer counters for visibility.
    """
    fresh: list[tuple[Path, dict, str]] = []
    cutoff = today - dt.timedelta(days=SAMPLING_FRESH_DAYS)
    for p in notes:
        fm, body = load_frontmatter(p)
        if fm is None:
            continue
        if fm.get("source") != "ai-session":
            continue
        if fm.get("processing_state") != "completed":
            continue
        created = fm.get("created")
        if not created:
            continue
        try:
            created_dt = dt.datetime.fromisoformat(str(created))
            created_date = created_dt.date()
        except ValueError:
            continue
        if created_date < cutoff:
            continue
        fresh.append((p, fm, body))

    if len(fresh) < SAMPLING_MIN_FRESH:
        return {
            "skipped": True,
            "reason": f"only {len(fresh)} fresh ai-session entries (< {SAMPLING_MIN_FRESH})",
            "fresh_count": len(fresh),
            "sample_size": 0,
            "structural_divergence_rate": None,
            "topics_divergence_rate": None,
            "capture_disagreements": 0.0,
            "intent_disagreements": 0.0,
            "capture_drifts": 0,
            "capture_abstains": 0,
            "intent_drifts": 0,
            "intent_abstains": 0,
            "topics_disagreements": 0,
            # Backwards-compat alias: equals structural_divergence_rate when set.
            "divergence_rate": None,
            "divergences": [],
        }

    fresh.sort(key=lambda t: str(t[0]))
    sample_size = max(SAMPLING_MIN_FRESH, int(len(fresh) * SAMPLING_RATE))
    sample_size = min(sample_size, CLAUDE_HARD_CAP)
    sample = fresh[:sample_size]

    divergences: list[dict] = []
    capture_dis = 0.0
    intent_dis = 0.0
    capture_drifts = 0
    capture_abstains = 0
    intent_drifts = 0
    intent_abstains = 0
    topics_dis = 0
    samples_classified = 0
    for p, fm, body in sample:
        rel = str(p.relative_to(vault_root))
        stored_capture = fm.get("capture")
        stored_intent = fm.get("intent")
        stored_topics = set(fm.get("topics") or [])

        if skip_llm:
            continue

        try:
            llm = call_claude_classify(body)
        except subprocess.TimeoutExpired:
            log(f"  sample timeout: {p.name}")
            continue
        except RuntimeError as e:
            log(f"  sample claude-error: {p.name}: {e}")
            continue

        samples_classified += 1
        llm_capture = llm.get("capture")
        llm_intent = llm.get("intent")
        llm_topics = set(llm.get("topics") or [])

        diff_detail: dict = {}
        cap_dis = _axis_disagreement(stored_capture, llm_capture)
        if cap_dis is not None:
            weight, kind = cap_dis
            capture_dis += weight
            if kind == "drift":
                capture_drifts += 1
            else:
                capture_abstains += 1
            diff_detail["capture"] = {
                "stored": stored_capture,
                "audit": llm_capture,
                "weight": weight,
                "kind": kind,
            }
        int_dis = _axis_disagreement(stored_intent, llm_intent)
        if int_dis is not None:
            weight, kind = int_dis
            intent_dis += weight
            if kind == "drift":
                intent_drifts += 1
            else:
                intent_abstains += 1
            diff_detail["intent"] = {
                "stored": stored_intent,
                "audit": llm_intent,
                "weight": weight,
                "kind": kind,
            }
        jaccard = topics_jaccard(stored_topics, llm_topics)
        if not topics_agree(stored_topics, llm_topics):
            topics_dis += 1
            diff_detail["topics"] = {
                "stored": sorted(stored_topics),
                "audit": sorted(llm_topics),
                "added": sorted(llm_topics - stored_topics),
                "removed": sorted(stored_topics - llm_topics),
                "jaccard": round(jaccard, 4),
                "model": "short-list-overlap" if max(len(stored_topics), len(llm_topics)) <= 2 else "jaccard",
            }
        if diff_detail:
            structural_points = (
                diff_detail.get("capture", {}).get("weight", 0.0)
                + diff_detail.get("intent", {}).get("weight", 0.0)
            )
            divergences.append({
                "path": rel,
                "structural_points": structural_points,
                "topics_disagrees": "topics" in diff_detail,
                "diff": diff_detail,
            })

    # Structural rate excludes topics: only capture + intent count toward tier.
    structural_denom = samples_classified * 2 if samples_classified > 0 else 0
    structural_rate = (capture_dis + intent_dis) / structural_denom if structural_denom > 0 else 0.0
    topics_rate = topics_dis / samples_classified if samples_classified > 0 else 0.0
    return {
        "skipped": False,
        "fresh_count": len(fresh),
        "sample_size": sample_size,
        "samples_classified": samples_classified,
        "structural_divergence_rate": structural_rate,
        "topics_divergence_rate": topics_rate,
        "capture_disagreements": capture_dis,
        "intent_disagreements": intent_dis,
        "capture_drifts": capture_drifts,
        "capture_abstains": capture_abstains,
        "intent_drifts": intent_drifts,
        "intent_abstains": intent_abstains,
        "topics_disagreements": topics_dis,
        # Backwards-compat alias for frontmatter / external consumers.
        "divergence_rate": structural_rate,
        "structural_above_threshold": structural_rate > DIVERGENCE_THRESHOLD,
        # Backwards-compat alias - tier-driver is structural-only now.
        "above_threshold": structural_rate > DIVERGENCE_THRESHOLD,
        "divergences": divergences,
    }


# ---------- Check 5: Tag-consistency ----------

def check_tags(notes: list[Path], vault_root: Path) -> dict:
    """Verify tags-list contains exactly [📥, 💭] and no forbidden emoji."""
    violations: list[dict] = []
    for p in notes:
        fm, _ = load_frontmatter(p)
        if fm is None:
            continue
        # Manual notes (Books with 📖, Quotes with 📜, etc.) follow user's own
        # tag conventions; the REQUIRED_TAGS/FORBIDDEN_*_EMOJI rules are
        # pipeline-specific (thought-pipeline class+type emoji uniformity).
        if not is_pipeline_entry(fm):
            continue
        rel = str(p.relative_to(vault_root))
        tags = fm.get("tags", [])
        if not isinstance(tags, list):
            violations.append({
                "path": rel,
                "type": "non_list_tags",
                "severity": "hoy",
                "observed": str(tags),
            })
            continue

        tagset = set(tags)
        for t in tags:
            if t in FORBIDDEN_STATUS_EMOJI:
                violations.append({
                    "path": rel,
                    "type": "forbidden_status_emoji",
                    "severity": "hoy",
                    "observed": t,
                })
            if t in FORBIDDEN_GROWTH_EMOJI:
                violations.append({
                    "path": rel,
                    "type": "forbidden_growth_emoji",
                    "severity": "hoy",
                    "observed": t,
                })

        for required in REQUIRED_TAGS:
            if required not in tagset:
                violations.append({
                    "path": rel,
                    "type": "missing_required_emoji",
                    "severity": "hoy",
                    "observed": f"missing {required!r}; tags={tags}",
                })

        extras = tagset - set(REQUIRED_TAGS) - FORBIDDEN_GROWTH_EMOJI - FORBIDDEN_STATUS_EMOJI
        for e in sorted(extras):
            violations.append({
                "path": rel,
                "type": "extra_tag",
                "severity": "lav",
                "observed": e,
            })

    violations.sort(key=lambda v: (v["path"], v["type"]))
    by_severity = {"hoy": 0, "lav": 0}
    for v in violations:
        by_severity[v["severity"]] = by_severity.get(v["severity"], 0) + 1
    return {"count": len(violations), "violations": violations, "by_severity": by_severity}


# ---------- Check 6: Missing-required-field ----------

def check_missing_required(entries: list[Path], vault_root: Path) -> dict:
    """Targeted check on entries marked pre_classified=full for conditional fields."""
    gaps: list[dict] = []
    for p in entries:
        fm, _ = load_frontmatter(p)
        if fm is None:
            continue
        # Defensive gate (also caught by pre_classified=full requirement below,
        # but explicit for clarity and consistency with other checks).
        if not is_pipeline_entry(fm):
            continue
        if fm.get("pre_classified") != "full":
            continue
        rel = str(p.relative_to(vault_root))
        intent = fm.get("intent")
        capture = fm.get("capture")
        source = fm.get("source")

        if intent is not None:
            status = fm.get("status")
            if status in (None, ""):
                gaps.append({
                    "path": rel,
                    "type": "missing_status",
                    "detail": f"intent={intent!r} but status is null",
                })

        if intent == "reminder":
            due = fm.get("due")
            if not due:
                gaps.append({
                    "path": rel,
                    "type": "missing_due",
                    "detail": "intent=reminder but due is null",
                })
            else:
                try:
                    dt.datetime.fromisoformat(str(due))
                except ValueError:
                    gaps.append({
                        "path": rel,
                        "type": "unparseable_due",
                        "detail": f"intent=reminder but due={due!r} not ISO8601-parseable",
                    })

        if capture in CAPTURE_REQUIRES_CREATOR:
            creator = fm.get("creator")
            if not creator:
                gaps.append({
                    "path": rel,
                    "type": "missing_creator",
                    "detail": f"capture={capture!r} but creator is null",
                })

        if source == "ai-session":
            ss = fm.get("source_session")
            if not ss:
                gaps.append({
                    "path": rel,
                    "type": "missing_source_session",
                    "detail": (
                        f"source=ai-session but source_session is null"
                        + (
                            f" (note: session_id_source={fm.get('session_id_source')!r} present)"
                            if fm.get("session_id_source") else ""
                        )
                    ),
                })
            elif not WIKILINK_RE.search(str(ss)):
                gaps.append({
                    "path": rel,
                    "type": "unformatted_source_session",
                    "detail": f"source_session={ss!r} not wikilink-formatted",
                })

    gaps.sort(key=lambda g: (g["path"], g["type"]))
    return {"count": len(gaps), "gaps": gaps}


# ---------- Tier aggregation ----------

def aggregate_tier(findings: dict) -> str:
    """Compute overall tier per audit-pass-spec.md tier-policy."""
    has_kritisk = False
    has_hoy = False
    has_lav = False

    dedup = findings.get("dedup", {})
    if dedup.get("count", 0) > 0:
        has_hoy = True

    schema = findings.get("schema", {})
    sev = schema.get("by_severity", {})
    if sev.get("kritisk", 0) > 0:
        has_kritisk = True
    if sev.get("hoy", 0) > 0:
        has_hoy = True
    if sev.get("lav", 0) > 0:
        has_lav = True

    wikilinks = findings.get("wikilinks", {})
    sev = wikilinks.get("by_severity", {})
    if sev.get("kritisk", 0) > 0:
        has_kritisk = True
    if sev.get("hoy", 0) > 0:
        has_hoy = True
    if sev.get("lav", 0) > 0:
        has_lav = True

    sampling = findings.get("sampling", {})
    if sampling.get("above_threshold"):
        has_hoy = True
    elif not sampling.get("skipped") and sampling.get("divergence_rate", 0) > 0:
        has_lav = True

    tags = findings.get("tags", {})
    sev = tags.get("by_severity", {})
    if sev.get("hoy", 0) > 0:
        has_hoy = True
    if sev.get("lav", 0) > 0:
        has_lav = True

    missing = findings.get("missing_required", {})
    if missing.get("count", 0) > 0:
        has_hoy = True

    if has_kritisk:
        return "kritisk"
    if has_hoy:
        return "hoy"
    if has_lav:
        return "lav"
    return "silent"


# ---------- Report rendering ----------

def render_report(
    audit_date: dt.datetime,
    duration_seconds: float,
    entries_scanned: dict,
    findings: dict,
    tier: str,
    exit_code: int,
) -> str:
    """Render the YYYY-MM-DD.md report-file per audit-pass-spec.md "Report-file"."""
    dedup = findings.get("dedup", {})
    schema = findings.get("schema", {})
    wikilinks = findings.get("wikilinks", {})
    sampling = findings.get("sampling", {})
    tags = findings.get("tags", {})
    missing = findings.get("missing_required", {})

    fm = {
        "audit_date": audit_date.isoformat(timespec="seconds"),
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner": "foundry-audit-cron",
        "runner_version": RUNNER_VERSION,
        "entries_scanned": {
            "inbox": entries_scanned.get("inbox", 0),
            "notes": entries_scanned.get("notes", 0),
            "total": entries_scanned.get("inbox", 0) + entries_scanned.get("notes", 0),
        },
        "tier": tier,
        "exit_code": exit_code,
        "findings": {
            "dedup_collisions": dedup.get("count", 0),
            "schema_violations": schema.get("count", 0),
            "broken_source_session": sum(
                1 for b in wikilinks.get("broken", []) if b.get("field") == "source_session"
            ),
            "broken_links": sum(
                1 for b in wikilinks.get("broken", [])
                if b.get("field") in ("links", "related")
            ),
            "classification_divergence_rate": (
                round(sampling.get("divergence_rate"), 4)
                if sampling.get("divergence_rate") is not None
                else None
            ),
            "classification_structural_divergence_rate": (
                round(sampling.get("structural_divergence_rate"), 4)
                if sampling.get("structural_divergence_rate") is not None
                else None
            ),
            "classification_topics_divergence_rate": (
                round(sampling.get("topics_divergence_rate"), 4)
                if sampling.get("topics_divergence_rate") is not None
                else None
            ),
            "classification_capture_drifts": sampling.get("capture_drifts", 0),
            "classification_capture_abstains": sampling.get("capture_abstains", 0),
            "classification_intent_drifts": sampling.get("intent_drifts", 0),
            "classification_intent_abstains": sampling.get("intent_abstains", 0),
            "classification_check_skipped": sampling.get("skipped", False),
            "tag_violations": tags.get("count", 0),
            "missing_required_field": missing.get("count", 0),
        },
        "duration_seconds": round(duration_seconds, 2),
    }
    fm_yaml = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)

    lines = [f"---\n{fm_yaml}---\n"]

    # Section 1
    lines.append("## 1. Dedup-verification (full-vault)\n")
    lines.append(f"**Findings:** {dedup.get('count', 0)}\n")
    if dedup.get("count", 0) == 0:
        lines.append("(No collisions detected.)\n")
    else:
        for c in dedup.get("collisions", []):
            lines.append(f"### dedup_hash {c['dedup_hash_prefix']}...\n")
            for e in c["entries"]:
                lines.append(f"- `{e['path']}` (created={e['created']}, capture={e['capture']}, status={e['status']})\n")
            lines.append("\n")

    # Section 2
    lines.append("## 2. Schema-compliance\n")
    sev = schema.get("by_severity", {})
    lines.append(
        f"**Findings:** {schema.get('count', 0)} "
        f"({sev.get('kritisk', 0)} kritisk, {sev.get('hoy', 0)} hoy, {sev.get('lav', 0)} lav)\n"
    )
    if schema.get("count", 0) == 0:
        lines.append("(No violations.)\n")
    else:
        for v in schema.get("violations", []):
            lines.append(f"- `{v['path']}` [{v['severity']}] {v['type']}: {v['detail']}\n")

    # Section 3
    lines.append("\n## 3. Wikilink-validation\n")
    sev = wikilinks.get("by_severity", {})
    lines.append(
        f"**Findings:** {wikilinks.get('count', 0)} "
        f"({sev.get('kritisk', 0)} kritisk, {sev.get('hoy', 0)} hoy, {sev.get('lav', 0)} lav)\n"
    )
    if wikilinks.get("count", 0) == 0:
        lines.append("(No broken wikilinks.)\n")
    else:
        critical_or_hoy = [b for b in wikilinks.get("broken", []) if b["severity"] in ("kritisk", "hoy")]
        lav = [b for b in wikilinks.get("broken", []) if b["severity"] == "lav"]
        if critical_or_hoy:
            lines.append("\n### Critical and hoy\n")
            for b in critical_or_hoy:
                lines.append(f"- `{b['path']}` [{b['severity']}] {b['field']}: `[[{b['target']}]]`\n")
        if lav:
            lines.append("\n### Lav\n")
            for b in lav:
                lines.append(f"- `{b['path']}` {b['field']}: `[[{b['target']}]]`\n")

    # Section 4
    lines.append("\n## 4. Sampling-classification\n")
    if sampling.get("skipped"):
        lines.append(f"**Skipped:** {sampling.get('reason', 'insufficient fresh entries')}\n")
    else:
        n_classified = sampling.get("samples_classified", sampling.get("sample_size", 0))
        lines.append(
            f"**Sample size:** {sampling.get('sample_size', 0)} "
            f"(of {sampling.get('fresh_count', 0)} fresh ai-session-entries; "
            f"{n_classified} classified by LLM)\n"
        )
        struct_rate = sampling.get("structural_divergence_rate", 0.0) or 0.0
        topics_rate = sampling.get("topics_divergence_rate", 0.0) or 0.0
        threshold_state = "above" if sampling.get("structural_above_threshold") else "below"
        cap_dis_w = sampling.get('capture_disagreements', 0.0) or 0.0
        int_dis_w = sampling.get('intent_disagreements', 0.0) or 0.0
        cap_drift = sampling.get('capture_drifts', 0)
        cap_abstain = sampling.get('capture_abstains', 0)
        int_drift = sampling.get('intent_drifts', 0)
        int_abstain = sampling.get('intent_abstains', 0)
        lines.append(
            f"**Structural divergence (capture+intent):** {struct_rate:.4f} "
            f"(`{cap_dis_w}` capture [{cap_drift} drift + {cap_abstain} abstain] "
            f"+ `{int_dis_w}` intent [{int_drift} drift + {int_abstain} abstain] "
            f"weighted disagreements; "
            f"{threshold_state} {DIVERGENCE_THRESHOLD} threshold; tier-driver)\n"
        )
        lines.append(
            f"**Topics divergence:** {topics_rate:.4f} "
            f"(`{sampling.get('topics_disagreements', 0)}` topic-set Jaccard "
            f"< {TOPICS_JACCARD_MATCH}; informational only, does not drive tier)\n"
        )
        divs = sampling.get("divergences", [])
        if divs:
            lines.append("\n### Divergent entries\n")
            for d in divs:
                struct_pts = d.get("structural_points", 0)
                topic_flag = "+topics" if d.get("topics_disagrees") else ""
                lines.append(
                    f"- `{d['path']}` ({struct_pts} structural{topic_flag})\n"
                )
                for field, detail in d["diff"].items():
                    if field == "topics":
                        lines.append(
                            f"  - topics (jaccard={detail.get('jaccard', 0):.2f}): "
                            f"added={detail['added']}, removed={detail['removed']}\n"
                        )
                    else:
                        kind = detail.get("kind", "drift")
                        weight = detail.get("weight", 1.0)
                        lines.append(
                            f"  - {field}: stored={detail['stored']!r}, "
                            f"audit={detail['audit']!r} ({kind}, weight={weight})\n"
                        )

    # Section 5
    lines.append("\n## 5. Tag-consistency\n")
    sev = tags.get("by_severity", {})
    lines.append(
        f"**Findings:** {tags.get('count', 0)} "
        f"({sev.get('hoy', 0)} hoy, {sev.get('lav', 0)} lav)\n"
    )
    if tags.get("count", 0) == 0:
        lines.append("(No tag-violations.)\n")
    else:
        for v in tags.get("violations", []):
            lines.append(f"- `{v['path']}` [{v['severity']}] {v['type']}: {v['observed']}\n")

    # Section 6
    lines.append("\n## 6. Missing-required-field\n")
    lines.append(f"**Findings:** {missing.get('count', 0)}\n")
    if missing.get("count", 0) == 0:
        lines.append("(No missing conditional-required fields.)\n")
    else:
        for g in missing.get("gaps", []):
            lines.append(f"- `{g['path']}` {g['type']}: {g['detail']}\n")

    # Summary
    lines.append("\n---\n\n## Summary\n")
    lines.append(f"- Tier: {tier}\n")
    lines.append(f"- Telegram alert: {'emitted' if tier in ('hoy', 'kritisk') else 'not emitted'}\n")
    lines.append(f"- Total findings: {sum_findings(findings)}\n")

    return "".join(lines)


def sum_findings(findings: dict) -> int:
    return (
        findings.get("dedup", {}).get("count", 0)
        + findings.get("schema", {}).get("count", 0)
        + findings.get("wikilinks", {}).get("count", 0)
        + findings.get("tags", {}).get("count", 0)
        + findings.get("missing_required", {}).get("count", 0)
        + (len(findings.get("sampling", {}).get("divergences", []))
           if not findings.get("sampling", {}).get("skipped") else 0)
    )


# ---------- Telegram emission ----------

def emit_telegram(tier: str, audit_date: dt.date, entries_scanned: dict, findings: dict, exit_code: int) -> None:
    """Emit Telegram alert per audit-pass-spec.md tier-policy."""
    if tier in ("silent", "lav"):
        return

    inbox_n = entries_scanned.get("inbox", 0)
    notes_n = entries_scanned.get("notes", 0)
    total = inbox_n + notes_n

    if tier == "hoy":
        prefix = "🔍"
        msg_lines = [
            f"{prefix} Audit-pass {audit_date.isoformat()}: tier=hoy",
            f"Scanned: {total} entries ({inbox_n} inbox, {notes_n} notes)",
        ]
        parts = []
        sc = findings.get("schema", {}).get("count", 0)
        if sc:
            parts.append(f"{sc} schema")
        bss = sum(1 for b in findings.get("wikilinks", {}).get("broken", []) if b.get("field") == "source_session")
        if bss:
            parts.append(f"{bss} broken source_session")
        dc = findings.get("dedup", {}).get("count", 0)
        if dc:
            parts.append(f"{dc} dedup")
        mc = findings.get("missing_required", {}).get("count", 0)
        if mc:
            parts.append(f"{mc} missing required-field")
        tc = findings.get("tags", {}).get("count", 0)
        if tc:
            parts.append(f"{tc} tag")
        if parts:
            msg_lines.append("Findings: " + ", ".join(parts))
        notify(" | ".join(msg_lines))
        return

    # kritisk
    prefix = "🚨"
    msg_lines = [f"{prefix} Audit-pass {audit_date.isoformat()}: tier=kritisk (exit_code={exit_code})"]
    sev = findings.get("schema", {}).get("by_severity", {})
    if sev.get("kritisk", 0):
        msg_lines.append(f"schema-kritisk: {sev['kritisk']} entries")
    sev = findings.get("wikilinks", {}).get("by_severity", {})
    if sev.get("kritisk", 0):
        msg_lines.append(f"broken source_session on ai-session: {sev['kritisk']}")
    notify(" | ".join(msg_lines))


# ---------- Heartbeat-state ----------

def update_heartbeat(state_file: Path, now: dt.datetime, exit_code: int, tier: str, findings_total: int) -> None:
    """Update heartbeat-state with this run's outcome."""
    prev: dict = {}
    if state_file.exists():
        try:
            prev = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    silent_prev = int(prev.get("consecutive_silent_runs", 0))
    failed_prev = int(prev.get("consecutive_failed_runs", 0))

    new_state = {
        "last_run": now.astimezone().isoformat(timespec="seconds"),
        "last_exit_code": exit_code,
        "last_tier": tier,
        "last_findings_total": findings_total,
        "consecutive_silent_runs": silent_prev + 1 if tier == "silent" else 0,
        "consecutive_failed_runs": failed_prev + 1 if exit_code != 0 else 0,
    }
    atomic_write_json(state_file, new_state)


# ---------- Main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="foundry-audit nightly vault audit-pass")
    ap.add_argument("--vault-root", default=None,
                    help="Override OBSIDIAN_VAULT_ROOT (for smoke-test)")
    ap.add_argument("--report-dir", default=None,
                    help="Override report-file output dir (for smoke-test)")
    ap.add_argument("--state-file", default=None,
                    help="Override heartbeat-state path (for smoke-test)")
    ap.add_argument("--skip-llm", action="store_true",
                    help="Skip claude -p calls in check 4 (sample collection only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run checks but do not write report or update state")
    args = ap.parse_args()

    start_ts = dt.datetime.now()

    # Pre-flight
    if not SYSTEM_PROMPT_PATH.exists():
        log(f"FATAL: system-prompt.md missing at {SYSTEM_PROMPT_PATH}")
        notify(f"(FATAL) system-prompt.md missing at {SYSTEM_PROMPT_PATH}")
        return 2

    vault_root_str = args.vault_root or os.environ.get("OBSIDIAN_VAULT_ROOT")
    if not vault_root_str:
        log("FATAL: OBSIDIAN_VAULT_ROOT not set and --vault-root not provided")
        notify("(FATAL) OBSIDIAN_VAULT_ROOT not set")
        return 2
    vault_root = Path(vault_root_str)
    if not vault_root.is_dir():
        log(f"vault unavailable: {vault_root}")
        notify(f"vault unavailable: {vault_root}")
        return 124

    # Same class of unavailability, separate root: the raw corpus lives beside
    # the vault, not in it, so a mounted vault says nothing about it. Checked
    # here rather than left to collect_entries() so the pass refuses before it
    # does any work - and so the operator gets one notify naming the root
    # instead of a traceback.
    raw_root = raw_root_for(vault_root)
    if not raw_root.is_dir():
        log(f"raw corpus unavailable: {raw_root}")
        notify(f"raw corpus unavailable: {raw_root} (set {RAW_ROOT_ENV}); "
               f"refusing the pass - an empty raw index would report every "
               f"ai-session note as kritisk")
        return 124

    state_file = Path(args.state_file or os.environ.get("AUDIT_STATE_FILE") or (Path.home() / ".audit-state.json"))

    if args.report_dir:
        report_dir = Path(args.report_dir)
    else:
        report_dir = vault_root / "5.Utility" / "Pipeline" / "Audit-Reports"

    try:
        log(f"=== audit start (pid {os.getpid()}) ===")
        log(f"vault_root={vault_root}")
        log(f"report_dir={report_dir}")

        # Collect
        log("collecting entries...")
        coll = collect_entries(vault_root)
        inbox = coll["inbox"]
        notes = coll["notes"]
        raw_index = coll["raw_index"]
        log(f"  inbox={len(inbox)} notes={len(notes)} raw_index={len(raw_index)}")

        log("building link-index...")
        link_index = build_link_index(vault_root)

        all_entries = inbox + notes

        # Run checks
        log("check 1: dedup-verification...")
        dedup_res = check_dedup(notes, vault_root)

        log("check 2: schema-compliance...")
        schema_res = check_schema(all_entries, vault_root)

        log("check 3: wikilink-validation...")
        wikilinks_res = check_wikilinks(all_entries, raw_index, link_index, vault_root)

        log("check 4: sampling-classification...")
        sampling_res = check_sampling(notes, vault_root, dt.date.today(), skip_llm=args.skip_llm)

        log("check 5: tag-consistency...")
        tags_res = check_tags(notes, vault_root)

        log("check 6: missing-required-field...")
        missing_res = check_missing_required(all_entries, vault_root)

        findings = {
            "dedup": dedup_res,
            "schema": schema_res,
            "wikilinks": wikilinks_res,
            "sampling": sampling_res,
            "tags": tags_res,
            "missing_required": missing_res,
        }

        tier = aggregate_tier(findings)
        log(f"tier={tier}")

        # Write report
        end_ts = dt.datetime.now()
        duration = (end_ts - start_ts).total_seconds()
        entries_scanned = {"inbox": len(inbox), "notes": len(notes)}
        report = render_report(end_ts, duration, entries_scanned, findings, tier, 0)
        report_path = report_dir / f"{dt.date.today().isoformat()}.md"
        if args.dry_run:
            log(f"[dry-run] would write report to {report_path}")
            log(f"[dry-run] would update state at {state_file}")
            log(f"[dry-run] findings_total={sum_findings(findings)}")
        else:
            atomic_write(report_path, report)
            log(f"report written: {report_path}")
            findings_total = sum_findings(findings)
            update_heartbeat(state_file, end_ts, 0, tier, findings_total)

        # Telegram
        if not args.dry_run:
            emit_telegram(tier, dt.date.today(), entries_scanned, findings, 0)

        log(f"=== audit complete (exit 0, tier={tier}) ===")
        return 0

    except Exception as e:
        log(f"FATAL unexpected: {e}")
        log(traceback.format_exc())
        notify(f"(FATAL) unexpected runtime error: {e}")
        try:
            if not args.dry_run:
                update_heartbeat(state_file, dt.datetime.now(), 1, "kritisk", 0)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
