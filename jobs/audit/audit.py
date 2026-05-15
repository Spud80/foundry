#!/usr/bin/env python3
"""audit.py - foundry-audit nightly vault audit-pass.

Scans the Obsidian vault per `dev-environment/docs/reference/audit-pass-spec.md`
(schema_version 1), runs six audit-checks, writes a daily report-file to
`5.Utility/Pipeline/Audit-Reports/YYYY-MM-DD.md`, updates heartbeat-state at
`~/.audit-state.json`, and emits tiered Telegram alerts.

Spec authority:
  dev-environment/docs/reference/audit-pass-spec.md
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

# ---------- Constants ----------

JOB_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = JOB_DIR / "system-prompt.md"
RUNNER_VERSION = "1.0.0"
RUNNER_SCHEMA_VERSION = 1

CAPTURE_ENUM = {"idea", "quote", "book", "movie", "tv_series", "podcast", "person", "note"}
INTENT_ENUM = {"followup", "reminder", "someday", "question", "decision", None}
STATUS_ENUM = {"active", "snoozed", "superseded", "done", "archived"}

HARD_REQUIRED_FULL = [
    "title", "created", "updated", "capture", "intent", "source",
    "source_session", "user_id", "scope", "dedup_hash",
    "processing_state", "pre_classified",
]
MINIMUM_BASELINE = ["title", "created", "source", "pre_classified"]

FORBIDDEN_GROWTH_EMOJI = {"📝", "🌱", "🌿", "🌲"}
FORBIDDEN_STATUS_EMOJI = {"🟥", "🟧", "🟨", "🟪", "🟩"}
REQUIRED_TAGS = ["📥", "💭"]
CAPTURE_REQUIRES_CREATOR = {"book", "movie", "tv_series", "podcast", "person"}

CLAUDE_PER_FILE_TIMEOUT = int(os.environ.get("AUDIT_CLAUDE_TIMEOUT_SECONDS", "300"))
CLAUDE_HARD_CAP = 50  # per audit-pass-spec.md "Resource budget"
SAMPLING_RATE = 0.10
SAMPLING_MIN_FRESH = 5
SAMPLING_FRESH_DAYS = 7
DIVERGENCE_THRESHOLD = 0.20

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
      'raw_index': {(date, sid): Path},  # 8.Cortex/Memory/raw/<date>/<sid>.md index for wikilink-validation
    }
    """
    inbox_dir = vault_root / "1.Inbox"
    notes_dir = vault_root / "2.Resources" / "Notes"
    raw_dir = vault_root / "8.Cortex" / "Memory" / "raw"

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
                violations.append({
                    "path": rel,
                    "type": "missing_baseline",
                    "severity": "hoy",
                    "detail": "source=ai-session but source_session missing",
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
        rel = str(p.relative_to(vault_root))

        source = fm.get("source")
        source_session = fm.get("source_session")
        if source_session:
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
        input=body,
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


def check_sampling(notes: list[Path], vault_root: Path, today: dt.date, skip_llm: bool = False) -> dict:
    """Sample 10% of fresh ai-session entries (last 7 days); independent re-classify
    via claude -p; aggregate divergence-rate."""
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
            "divergence_rate": None,
            "divergences": [],
        }

    fresh.sort(key=lambda t: str(t[0]))
    sample_size = max(SAMPLING_MIN_FRESH, int(len(fresh) * SAMPLING_RATE))
    sample_size = min(sample_size, CLAUDE_HARD_CAP)
    sample = fresh[:sample_size]

    divergences: list[dict] = []
    total_points = 0
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

        llm_capture = llm.get("capture")
        llm_intent = llm.get("intent")
        llm_topics = set(llm.get("topics") or [])

        points = 0
        diff_detail = {}
        if llm_capture != stored_capture:
            points += 1
            diff_detail["capture"] = {"stored": stored_capture, "audit": llm_capture}
        if llm_intent != stored_intent:
            points += 1
            diff_detail["intent"] = {"stored": stored_intent, "audit": llm_intent}
        if llm_topics != stored_topics:
            points += 1
            diff_detail["topics"] = {
                "stored": sorted(stored_topics),
                "audit": sorted(llm_topics),
                "added": sorted(llm_topics - stored_topics),
                "removed": sorted(stored_topics - llm_topics),
            }
        if points > 0:
            divergences.append({"path": rel, "points": points, "diff": diff_detail})
        total_points += points

    max_points = sample_size * 3
    divergence_rate = total_points / max_points if max_points > 0 else 0.0
    return {
        "skipped": False,
        "fresh_count": len(fresh),
        "sample_size": sample_size,
        "divergence_rate": divergence_rate,
        "divergences": divergences,
        "above_threshold": divergence_rate > DIVERGENCE_THRESHOLD,
    }


# ---------- Check 5: Tag-consistency ----------

def check_tags(notes: list[Path], vault_root: Path) -> dict:
    """Verify tags-list contains exactly [📥, 💭] and no forbidden emoji."""
    violations: list[dict] = []
    for p in notes:
        fm, _ = load_frontmatter(p)
        if fm is None:
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
                    "detail": "source=ai-session but source_session is null",
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
        lines.append(
            f"**Sample size:** {sampling.get('sample_size', 0)} "
            f"(of {sampling.get('fresh_count', 0)} fresh ai-session-entries)\n"
        )
        rate = sampling.get("divergence_rate", 0.0)
        threshold_state = "above" if sampling.get("above_threshold") else "below"
        lines.append(f"**Divergence rate:** {rate:.4f} ({threshold_state} {DIVERGENCE_THRESHOLD} threshold)\n")
        divs = sampling.get("divergences", [])
        if divs:
            lines.append("\n### Divergent entries\n")
            for d in divs:
                lines.append(f"- `{d['path']}` ({d['points']} divergence points)\n")
                for field, detail in d["diff"].items():
                    if field == "topics":
                        lines.append(
                            f"  - topics: added={detail['added']}, removed={detail['removed']}\n"
                        )
                    else:
                        lines.append(f"  - {field}: stored={detail['stored']!r}, audit={detail['audit']!r}\n")

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
