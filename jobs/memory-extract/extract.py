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
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
TYPES = ("observation", "decision", "learning", "error", "pattern", "intent")
MANIFEST_FILENAME = "_capture-manifest.json"
STATE_FILENAME = ".compile-state.json"
SCRIPT_DIR = Path(__file__).resolve().parent

OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "slug", "topics", "body", "date"],
                "properties": {
                    "type": {"type": "string", "enum": list(TYPES)},
                    "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
                    "topics": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {"type": "string", "pattern": "^¤[a-z0-9-]+$"},
                    },
                    "modal": {"type": "string", "enum": ["actionable", "speculative", "question"]},
                    "body": {"type": "string", "minLength": 1},
                    "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                },
            },
        },
    },
    "required": ["entries"],
}

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


# ----- Atomic write -----

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
                raise PreflightError(
                    f"capture sync incomplete: sha256 mismatch on {rel_path} "
                    f"(manifest={expected_sha[:8]}.., actual={actual[:8]}..)"
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
    cmd = [
        "claude", "-p",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(OUTPUT_JSON_SCHEMA),
        "--system-prompt", system_prompt,
        "--model", model,
    ]
    if fallback_model:
        cmd += ["--fallback-model", fallback_model]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    # Pass raw transcript via stdin to avoid argv-length limits and to keep
    # claude from misparsing leading `---` as an option flag.
    cmd += ["--input-format", "text"]

    proc = subprocess.run(
        cmd,
        input=raw_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exit {proc.returncode}: {proc.stderr[:500]}"
        )
    # claude --output-format json wraps the response. Extract the result text.
    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude stdout not JSON: {e}; raw[:200]={proc.stdout[:200]}")
    # Wrapper format from claude CLI: {"type":"result","result":"...", "is_error":false, ...}
    if isinstance(wrapper, dict) and "result" in wrapper:
        result_text = wrapper["result"]
    else:
        result_text = proc.stdout  # fall back
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
    atomic_write(target, QUARTER_TEMPLATE.format(type=type_, quarter=quarter, today=today))
    return target


def append_block(target: Path, block: str) -> None:
    """Append a heading-block to a quarter-file (read-modify-write, atomic)."""
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if not existing.endswith("\n"):
        existing += "\n"
    atomic_write(target, existing + block)


# ----- Per-session processing -----

def process_session(
    raw_path: Path,
    *,
    date: str,
    session_id: str,
    extracted_dir: Path,
    system_prompt: str,
    args: argparse.Namespace,
) -> int:
    """Returns number of entries written for this session."""
    raw_text = raw_path.read_text(encoding="utf-8")
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
    for entry in entries:
        # Validate intent has modal
        if entry["type"] == "intent" and not entry.get("modal"):
            print(f"  WARN: intent entry without modal in {session_id}, defaulting to speculative", file=sys.stderr)
            entry["modal"] = "speculative"
        # Determine target quarter file from entry's own date
        q = quarter_for(entry["date"])
        target = ensure_quarter_file(extracted_dir, entry["type"], q)
        block = format_heading_block(entry, date=date, session_id=session_id)
        append_block(target, block)
        written += 1
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
    state_path = memory_dir / STATE_FILENAME

    if not args.system_prompt_file.exists():
        print(f"ERROR: system-prompt-file not found: {args.system_prompt_file}", file=sys.stderr)
        return 2
    system_prompt = args.system_prompt_file.read_text(encoding="utf-8")

    # ----- Pre-flight 1: manifest -----
    try:
        verified = verify_manifests(raw_root)
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
                system_prompt=system_prompt,
                args=args,
            )
        except Exception as e:  # noqa: BLE001
            last_error = f"{date}/{sid}: {e}"
            print(f"  ERROR: {last_error}", file=sys.stderr)
            # Don't update state for failed sessions - retry next run
            continue

        if not args.dry_run:
            state["processed_session_ids_by_date"].setdefault(date, []).append(sid)
            state["sessions_pending_count"] = max(0, state["sessions_pending_count"] - 1)
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
