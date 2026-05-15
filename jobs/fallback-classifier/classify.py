#!/usr/bin/env python3
"""classify.py - foundry-fallback per-file frontmatter classifier.

Globs 1.Inbox/pending-foundry-*.md, recovery-classifies each file by state,
runs claude -p on partial/none entries, atomic-writes normalized frontmatter,
and renames to ai-capture-*.md for batch-processor pickup.

Spec authority:
  dev-environment/docs/reference/capture-vocabulary.md (schema_version 1)
  jobs/fallback-classifier/CONTRACT.md (runtime interface)

Exit-codes:
  0  OK (all pending files processed cleanly, or empty glob)
  1  transient (per-file LLM/parse errors; failed files left as pending-foundry-*.md)
  2  fatal (missing system-prompt.md, OBSIDIAN_VAULT_ROOT invalid)
  124 hard timeout (propagated from outer run.sh / per-file claude timeout)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import yaml

# ---------- Constants ----------

JOB_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = JOB_DIR / "system-prompt.md"

PENDING_GLOB = "pending-foundry-*.md"
PENDING_RE = re.compile(r"^pending-foundry-(?P<sid>[^-]+(?:-[^-]+)*)-(?P<ts>\d{8}T\d{6})\.md$")

HARD_REQUIRED = [
    "title", "created", "updated", "capture", "intent", "source",
    "source_session", "user_id", "scope", "dedup_hash",
    "processing_state", "pre_classified",
]

CAPTURE_ENUM = {"idea", "quote", "book", "movie", "tv_series", "podcast", "person", "note"}
INTENT_ENUM = {"followup", "reminder", "someday", "question", "decision", None}
STATUS_ENUM = {"active", "snoozed", "superseded", "done", "archived"}

CLAUDE_PER_FILE_TIMEOUT = int(os.environ.get("FALLBACK_CLAUDE_TIMEOUT_SECONDS", "1200"))  # 20m

OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "capture": {"type": "string", "enum": sorted(CAPTURE_ENUM)},
        "intent": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": sorted(STATUS_ENUM)},
        "due": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "creator": {"type": "string"},
        "year": {"type": "integer"},
        "genre": {"type": "array", "items": {"type": "string"}},
        "attribution": {"type": "string"},
    },
}

# ---------- Notify ----------

def notify(msg: str) -> None:
    """Send Telegram alert via FOUNDRY_NOTIFY_SH. Best-effort, never raises."""
    notify_path = os.environ.get("FOUNDRY_NOTIFY_SH")
    if not notify_path or not Path(notify_path).exists():
        print(f"NOTIFY-skipped (no FOUNDRY_NOTIFY_SH): {msg}", file=sys.stderr)
        return
    try:
        subprocess.run([notify_path, f"fallback-classifier: {msg}"], timeout=10, check=False)
    except Exception as e:
        print(f"NOTIFY-subprocess-failed: {e}", file=sys.stderr)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


# ---------- Frontmatter parsing ----------

FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def load_frontmatter(path: Path) -> tuple[dict, str]:
    """Return (frontmatter-dict, body-string). Raises ValueError on malformed YAML."""
    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if not m:
        raise ValueError(f"no frontmatter block in {path.name}")
    fm_yaml, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_yaml) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error in {path.name}: {e}")
    if not isinstance(fm, dict):
        raise ValueError(f"frontmatter is not a mapping in {path.name}")
    return fm, body


def dump_frontmatter(fm: dict, body: str) -> str:
    """Serialize frontmatter + body. Uses block-style YAML for readability."""
    fm_yaml = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{fm_yaml}---\n{body}"


def atomic_write(path: Path, content: str) -> None:
    """Write content to path via tempfile + os.replace. Tempfile pattern .tmp-fallback-*
    must be in Syncthing .stignore on filehub-side to avoid in-flight propagation."""
    tmp = path.parent / f".tmp-fallback-{uuid.uuid4().hex}.md"
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


# ---------- State classification (recovery-scan) ----------

def has_hard_required(fm: dict) -> bool:
    return all(k in fm for k in HARD_REQUIRED)


def conditionally_required_ok(fm: dict) -> bool:
    intent = fm.get("intent")
    if intent is None:
        return True
    if "status" not in fm:
        return False
    if intent == "reminder" and "due" not in fm:
        return False
    return True


def classify_state(fm: dict) -> str:
    """Return one of: 'normal', 'rename-only', 'recovery-reclassify', 'unknown'.

    Distinguishes 'marker absent' from 'marker explicitly false/null'.
    Explicit `foundry_pending: false` is suspicious and routed to 'unknown'
    (would not occur in normal pipeline; possibly manual edit gone wrong).
    """
    has_marker = "foundry_pending" in fm
    foundry_pending = fm.get("foundry_pending")
    pre_classified = fm.get("pre_classified")

    if has_marker:
        return "normal" if foundry_pending is True else "unknown"

    # Marker absent (key not present in frontmatter).
    if has_hard_required(fm) and conditionally_required_ok(fm) and pre_classified == "full":
        return "rename-only"
    if not has_hard_required(fm):
        return "recovery-reclassify"
    # All hard-required set but pre_classified != 'full' or conditional missing - suspicious.
    return "unknown"


# ---------- Claude subprocess ----------

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


_ENUM_FIELDS = {
    "capture": CAPTURE_ENUM,
    "intent": INTENT_ENUM,
    "status": STATUS_ENUM,
}


def _is_invalid_enum_value(field: str, value) -> bool:
    """True when the existing field value fails enum validation.

    intent=None and status=None are valid (None is in INTENT_ENUM; status has
    conditional-required semantics handled elsewhere). Only invalid when the
    value is a non-empty value that does not match the allowed set.
    """
    enum_set = _ENUM_FIELDS.get(field)
    if enum_set is None:
        return False
    if value in (None, "", []):
        return False  # empty handled by missing-only merge path
    return value not in enum_set


def call_claude(body: str, current_fm: dict, current_date: str) -> dict:
    """Invoke `claude -p` headless with --json-schema validation. Return parsed dict.

    Raises RuntimeError on subprocess-failure, TimeoutExpired on per-file timeout.
    """
    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    # Strip off-limits fields from the frontmatter we show to the LLM, so it
    # cannot accidentally echo them back (would be merged-skip anyway, but
    # cleaner prompt).
    sanitized_fm = {
        k: v for k, v in current_fm.items()
        if k not in {"foundry_pending"}
    }
    fm_yaml = yaml.safe_dump(sanitized_fm, allow_unicode=True, sort_keys=False, default_flow_style=False)

    user_msg = (
        f"Current date: {current_date}\n\n"
        f"Existing frontmatter:\n{fm_yaml}\n"
        f"Body:\n{body}"
    )

    cmd = [
        "claude", "-p",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(OUTPUT_JSON_SCHEMA),
        "--system-prompt", system_prompt,
        "--input-format", "text",
    ]

    proc = subprocess.run(
        cmd,
        input=user_msg,
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
        raise RuntimeError(f"classifier response not JSON: {e}; first-200={result_text[:200]}")


# ---------- Classification merge ----------

def validate_llm_output(llm: dict) -> list[str]:
    """Return list of validation errors (empty = OK)."""
    errs: list[str] = []
    capture = llm.get("capture")
    if capture is not None and capture not in CAPTURE_ENUM:
        errs.append(f"capture={capture!r} not in enum")
    intent = llm.get("intent")
    if intent is not None and intent not in INTENT_ENUM:
        errs.append(f"intent={intent!r} not in enum")
    status = llm.get("status")
    if status is not None and status not in STATUS_ENUM:
        errs.append(f"status={status!r} not in enum")
    if intent == "reminder" and "due" not in llm:
        errs.append("intent=reminder but due missing")
    if intent is not None and intent != "null" and "status" not in llm and "status" not in {}:
        # status may already exist in current_fm; checked in merge
        pass
    return errs


def merge_classification(current_fm: dict, llm_output: dict) -> dict:
    """Merge LLM output into current frontmatter.

    Merge-policy: take LLM value when current is missing/empty OR when the
    current value fails enum validation (capture/intent/status). Valid
    existing values are preserved. pre_classified is always overwritten to
    'full' below.

    The enum-overwrite path closes the case where save.py wrote an entry
    with an invalid enum (e.g. ``intent: 'todo'``); without it, the invalid
    value survived through fallback and inbox-handler re-routed back to
    pending-foundry indefinitely.
    """
    merged = dict(current_fm)

    fillable = ["title", "capture", "intent", "status", "due", "topics",
                "creator", "year", "genre", "attribution"]
    for k in fillable:
        if k not in llm_output:
            continue
        current_val = merged.get(k)
        if current_val in (None, "", []) or _is_invalid_enum_value(k, current_val):
            merged[k] = llm_output[k]

    # Always set/overwrite:
    merged["pre_classified"] = "full"
    # Touch updated to reflect classifier-pass time.
    merged["updated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    # Default tags for thought-pipeline entries (per capture-vocabulary.md
    # "Type-emoji for thought-pipeline entries"). Apply only if missing.
    if merged.get("tags") in (None, []):
        merged["tags"] = ["📥", "💭"]

    # Default processor-fillable fields per capture-vocabulary.md "Processor-default fields".
    merged.setdefault("topics", [])
    merged.setdefault("snooze_until", None)
    merged.setdefault("notify", "none")
    merged.setdefault("links", [])
    merged.setdefault("related", [])
    merged.setdefault("plan", None)
    merged.setdefault("action", None)

    # Conditionally-required: status when intent != null.
    if merged.get("intent") is not None and "status" not in merged:
        merged["status"] = "active"

    # Remove trigger-marker.
    merged.pop("foundry_pending", None)

    return merged


# ---------- Per-file processing ----------

def derive_ai_capture_path(pending_path: Path) -> Path:
    """pending-foundry-<sid>-<ts>.md -> ai-capture-<sid>-<ts>.md."""
    m = PENDING_RE.match(pending_path.name)
    if not m:
        raise ValueError(f"filename does not match pending-foundry pattern: {pending_path.name}")
    return pending_path.parent / f"ai-capture-{m.group('sid')}-{m.group('ts')}.md"


def process_file(pending_path: Path, dry_run: bool = False) -> str:
    """Return one of: 'classified', 'rename-only', 'reclassified', 'skipped-unknown',
    'failed-still-pending'. Raises only on per-file fatal scenarios."""

    try:
        fm, body = load_frontmatter(pending_path)
    except ValueError as e:
        log(f"FAIL parse {pending_path.name}: {e}")
        notify(f"frontmatter parse failed: {pending_path.name}: {e}")
        return "failed-still-pending"

    state = classify_state(fm)
    log(f"  state={state} file={pending_path.name}")

    if state == "unknown":
        notify(f"unknown state for {pending_path.name} (foundry_pending={fm.get('foundry_pending')!r}, pre_classified={fm.get('pre_classified')!r}); manual intervention required")
        return "skipped-unknown"

    if state == "rename-only":
        # Crash between mutate and rename. Just complete the rename.
        target = derive_ai_capture_path(pending_path)
        if dry_run:
            log(f"  [dry-run] would rename {pending_path.name} -> {target.name}")
            return "rename-only"
        os.rename(pending_path, target)
        log(f"  renamed (recovery): {pending_path.name} -> {target.name}")
        return "rename-only"

    # state in ('normal', 'recovery-reclassify') - run claude -p.
    current_date = dt.date.today().isoformat()
    try:
        llm = call_claude(body, fm, current_date)
    except subprocess.TimeoutExpired:
        log(f"FAIL timeout {pending_path.name} after {CLAUDE_PER_FILE_TIMEOUT}s")
        notify(f"TIMEOUT classifying {pending_path.name} (>{CLAUDE_PER_FILE_TIMEOUT}s)")
        return "failed-still-pending"
    except RuntimeError as e:
        log(f"FAIL claude {pending_path.name}: {e}")
        notify(f"claude -p failed on {pending_path.name}: {e}")
        return "failed-still-pending"

    errs = validate_llm_output(llm)
    if errs:
        log(f"FAIL validate {pending_path.name}: {errs}")
        notify(f"LLM output invalid for {pending_path.name}: {'; '.join(errs)}")
        return "failed-still-pending"

    merged = merge_classification(fm, llm)

    # Post-merge sanity: all hard-required + conditionally-required must be set.
    missing = [k for k in HARD_REQUIRED if k not in merged]
    if missing:
        log(f"FAIL incomplete {pending_path.name}: missing {missing}")
        notify(f"classifier left missing hard-required {missing} on {pending_path.name}; retry next cron")
        return "failed-still-pending"
    if not conditionally_required_ok(merged):
        log(f"FAIL conditional {pending_path.name}: status/due missing for intent={merged.get('intent')!r}")
        notify(f"classifier left missing conditional fields on {pending_path.name}")
        return "failed-still-pending"

    new_text = dump_frontmatter(merged, body)
    target = derive_ai_capture_path(pending_path)

    if dry_run:
        log(f"  [dry-run] would mutate+rename {pending_path.name} -> {target.name}")
        log(f"  [dry-run] new frontmatter: {merged}")
        return "classified" if state == "normal" else "reclassified"

    # Mutate first (atomic), then rename. See CONTRACT.md "Atomic write-back-semantikk".
    atomic_write(pending_path, new_text)
    os.rename(pending_path, target)
    log(f"  classified+renamed: {pending_path.name} -> {target.name}")
    return "classified" if state == "normal" else "reclassified"


# ---------- Main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="foundry-fallback classifier")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N files (for smoke-tests)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify and report, but do not write or rename")
    ap.add_argument("--inbox-dir", default=None,
                    help="Override 1.Inbox/ path (default: $OBSIDIAN_VAULT_ROOT/1.Inbox)")
    args = ap.parse_args()

    # ---------- Pre-flight ----------
    if not SYSTEM_PROMPT_PATH.exists():
        log(f"FATAL: system-prompt.md missing at {SYSTEM_PROMPT_PATH}")
        notify(f"(FATAL) system-prompt.md missing at {SYSTEM_PROMPT_PATH}")
        return 2

    vault_root = os.environ.get("OBSIDIAN_VAULT_ROOT")
    if not vault_root and not args.inbox_dir:
        log("FATAL: OBSIDIAN_VAULT_ROOT not set and --inbox-dir not provided")
        notify("(FATAL) OBSIDIAN_VAULT_ROOT not set")
        return 2

    inbox_dir = Path(args.inbox_dir) if args.inbox_dir else Path(vault_root) / "1.Inbox"
    if not inbox_dir.is_dir():
        log(f"FATAL: inbox path is not a directory: {inbox_dir}")
        notify(f"(FATAL) inbox path is not a directory: {inbox_dir}")
        return 2

    # ---------- Glob ----------
    pending_files = sorted(inbox_dir.glob(PENDING_GLOB))
    if args.limit is not None:
        pending_files = pending_files[: args.limit]

    log(f"found {len(pending_files)} pending-foundry-*.md files in {inbox_dir}")

    if not pending_files:
        log("=== run complete (exit 0, no pending files) ===")
        return 0

    # ---------- Per-file processing ----------
    stats = {
        "classified": 0,
        "rename-only": 0,
        "reclassified": 0,
        "skipped-unknown": 0,
        "failed-still-pending": 0,
    }
    for p in pending_files:
        result = process_file(p, dry_run=args.dry_run)
        stats[result] = stats.get(result, 0) + 1

    log(f"summary: {stats}")

    # ---------- Aggregated exit code ----------
    if stats["failed-still-pending"] > 0 or stats["skipped-unknown"] > 0:
        log("=== run complete (exit 1, transient errors) ===")
        return 1

    log("=== run complete (exit 0) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
