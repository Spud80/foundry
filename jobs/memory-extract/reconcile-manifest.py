#!/usr/bin/env python3
"""Reconcile _capture-manifest.json sha256 entries after backup restore.

When raw-files are restored from backup, their disk-sha256 typically differs
from the sha256 recorded in `_capture-manifest.json` (written by
`memory-capture.py` at original capture time). extract.py preflight refuses
to process date-dirs with mismatched sha to avoid feeding half-synced data
into the compiled layer.

This tool walks `<raw-root>/*/_capture-manifest.json` (raw-root resolved by
`_paths.resolve_raw_dir`: --raw-root > CORTEX_RAW_ROOT > vault-root default),
recomputes sha256 from disk, and updates the manifest entry to match. Dry-run
by default - require --apply to write.

USAGE
-----
    # Dry-run all date-dirs:
    reconcile-manifest.py --vault-root '/data/sync/obsidian/My Vault'

    # Limit to one date:
    reconcile-manifest.py --vault-root '/data/sync/obsidian/My Vault' --date 2026-02-24

    # Apply changes:
    reconcile-manifest.py --vault-root '/data/sync/obsidian/My Vault' --apply

    # Apply with foundry deploy-lock (recommended when extract.py / memory-capture
    # might run concurrently - e.g. running reconcile on foundry-CT itself):
    reconcile-manifest.py --vault-root "$HOME/vault/My Vault" --apply \
        --lock-file "$HOME/foundry/.deploy.lock"

EXIT CODES
----------
    0 - dry-run completed (or --apply success)
    1 - usage error (vault-root not found, etc.)
    2 - mismatch detected with no manifest entry (file present on disk but
        not listed in manifest - reconcile cannot fix this; manual review)
    3 - --lock-file given but flock could not be acquired within timeout

SAFETY
------
- Default is dry-run. Must pass --apply to write.
- Atomic write via unique tempfile + os.replace (race-safe).
- Files listed in manifest but missing from disk are reported but NOT removed
  from manifest (could indicate sync-in-progress, not a stale entry).
- Path-traversal defence: manifest entries that resolve outside the raw/-tree
  are rejected with warning (defensive against tampered manifests).

CONCURRENCY
-----------
- Reconcile and extract.py MUST NOT run on the same date-dir simultaneously.
  Race: extract reads manifest at preflight start; if reconcile rewrites it
  mid-flight, extract sees an inconsistent sha-set.
- Reconcile and memory-capture.py MUST NOT run on the same date-dir
  simultaneously. Race: capture appends a session entry; reconcile may "fix"
  the sha for the post-capture file content (different from operator-intended
  post-restore content).
- Mitigation: pass --lock-file <path> to acquire flock on the foundry deploy
  lock (~/foundry/.deploy.lock) which all foundry jobs respect. flock is
  non-blocking with a 10-second retry budget; exits 3 if not acquired.
- The lock-file flag is optional - default behaviour is unchanged (no lock).
  Recommended whenever reconcile runs on foundry-CT or alongside cron-scheduled
  jobs. Safe to skip when reconcile runs from a dev-machine against a vault
  copy nobody else is writing to.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# This tool used to be deliberately import-free so it could be copied to any
# host on its own. Raw-root resolution ended that: a recovery tool that guesses
# its own root is a recovery tool that can report a clean corpus it never
# looked at. _paths.py now ships with it (declared in deploy-sets.yaml).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import RAW_LEAF, RawRootUnavailable, require_raw_dir  # noqa: E402


MANIFEST_FILENAME = "_capture-manifest.json"
LOCK_RETRY_SECONDS = 10  # total budget for flock acquisition before giving up


def acquire_flock(lock_path: Path):
    """Acquire non-blocking flock on lock_path with retry budget.

    Returns the open file-handle (caller MUST keep it alive for the duration
    of the critical section; closing releases the lock). Returns None on
    timeout. Lazy-imports fcntl so the rest of the script remains importable
    on Windows where fcntl is unavailable.
    """
    try:
        import fcntl
    except ImportError:
        print(
            "ERROR: --lock-file requires fcntl (Unix-only). This script "
            "should run on filehub or foundry-CT, not directly on Windows.",
            file=sys.stderr,
        )
        return None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a")
    deadline = time.monotonic() + LOCK_RETRY_SECONDS
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(0.5)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    """Write content atomically via unique tempfile + rename.

    Uses tempfile.NamedTemporaryFile (delete=False) in the same directory so
    os.replace is a same-volume atomic rename. Cleanup-on-failure unlinks the
    tempfile if the write or rename raises. The unique-suffix pattern avoids
    collision if two reconcile processes target the same manifest concurrently.

    Forces LF newlines and chmod 0664 after replace so the rewritten manifest
    keeps the POSIX ACL mask rw on filehub (mkstemp creates 0600, which would
    collapse the mask to --- and block Syncthing reads). Mirrors the
    capture-layer atomic_write in _manifest.py; kept inline here so this
    recovery tool stays a self-contained single file portable to any host.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_path, path)
        os.chmod(path, 0o664)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def reconcile_date_dir(date_dir: Path, apply: bool) -> tuple[int, int, list[str]]:
    """Return (mismatches_found, mismatches_fixed, warnings)."""
    manifest_path = date_dir / MANIFEST_FILENAME
    warnings: list[str] = []
    if not manifest_path.exists():
        md_files = list(date_dir.glob("*.md"))
        if md_files:
            warnings.append(
                f"  {date_dir.name}/: no manifest, but {len(md_files)} .md files present"
            )
        return (0, 0, warnings)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        warnings.append(f"  {date_dir.name}/: manifest unparseable: {e}")
        return (0, 0, warnings)

    sessions = manifest.get("sessions", [])
    mismatches = 0
    fixed = 0
    changed_entries: list[tuple[str, str, str]] = []

    # Containment-root for path-traversal defence: the raw/-tree itself.
    # All listed paths must resolve inside this directory; reject anything that
    # escapes via "../" or absolute-path injection in a malformed manifest.
    raw_root = (date_dir.parent.parent / "raw").resolve()

    for entry in sessions:
        rel_path = entry["path"]
        expected = entry["sha256"]
        # rel_path = "raw/<date>/<file>.md"; date_dir.parent.parent = the raw
        # root's parent. Anchored on the manifest's own convention, so it
        # follows the corpus wherever the root is configured to live.
        local = date_dir.parent.parent / rel_path
        try:
            resolved = local.resolve()
            resolved.relative_to(raw_root)
        except ValueError:
            warnings.append(
                f"  {date_dir.name}/: manifest entry {rel_path!r} escapes raw/-root "
                f"(possible tampering); skipped"
            )
            continue
        if not local.exists():
            warnings.append(
                f"  {date_dir.name}/{Path(rel_path).name}: listed in manifest "
                f"but file missing on disk (sync-in-progress?)"
            )
            continue
        actual = sha256_of(local)
        if actual != expected:
            mismatches += 1
            changed_entries.append((rel_path, expected, actual))
            if apply:
                entry["sha256"] = actual
                fixed += 1

    if mismatches:
        print(f"\n{date_dir.name}/  ({mismatches} mismatch{'es' if mismatches != 1 else ''}):")
        for rel_path, expected, actual in changed_entries:
            print(f"  {Path(rel_path).name}")
            print(f"    manifest sha: {expected[:16]}..")
            print(f"    disk sha:     {actual[:16]}..")

    # Also report files on disk not in manifest (extra files, possible drift).
    listed_paths = {entry["path"] for entry in sessions}
    md_on_disk = {f"raw/{date_dir.name}/{p.name}" for p in date_dir.glob("*.md")}
    extras = md_on_disk - listed_paths
    for extra in sorted(extras):
        warnings.append(
            f"  {date_dir.name}/{Path(extra).name}: file present on disk "
            f"but NOT listed in manifest (manual review needed)"
        )

    if apply and fixed > 0:
        atomic_write(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        print(f"  -> manifest rewritten ({fixed} entries updated)")

    return (mismatches, fixed, warnings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--vault-root",
        default=os.environ.get("OBSIDIAN_VAULT_ROOT"),
        help="Path to vault root (default: $OBSIDIAN_VAULT_ROOT)",
    )
    p.add_argument(
        "--raw-root",
        default=None,
        help=("Path to the raw session corpus (default: $CORTEX_RAW_ROOT, else "
              "the raw/ dir under the resolved vault root). Wins over "
              "--vault-root, which is kept for backward compatibility."),
    )
    p.add_argument(
        "--date",
        help="Limit to single date-dir (YYYY-MM-DD format)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default: dry-run, report only)",
    )
    p.add_argument(
        "--lock-file",
        type=str,
        default=None,
        metavar="<path>",
        help=("Acquire non-blocking flock on this file before reconciling "
              "(recommended: ~/foundry/.deploy.lock when running on foundry-CT "
              "or alongside scheduled jobs). Exits 3 if lock cannot be acquired "
              "within 10 seconds. See CONCURRENCY section in module docstring."),
    )
    args = p.parse_args(argv)

    try:
        raw_root = require_raw_dir(
            cli_raw_root=args.raw_root, cli_vault_root=args.vault_root
        )
    except RawRootUnavailable as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # The manifest records each session as "raw/<date>/<file>.md" and every
    # consumer resolves it against the raw root's PARENT. That only holds while
    # the root's own leaf is "raw" - which the relocation deliberately keeps.
    # Say so here rather than let a differently-named root produce a clean
    # "mismatches found: 0" against files it never opened; this tool is the
    # gate the corpus deletion hangs on.
    if raw_root.name != RAW_LEAF:
        print(f"ERROR: raw-root must end in {RAW_LEAF!r}, got: {raw_root}",
              file=sys.stderr)
        return 1

    # Acquire concurrency-lock if requested. Holding lock_fh open keeps the
    # flock alive; it releases automatically at process-exit (or when fh
    # garbage-collected) but we keep a named reference for clarity.
    lock_fh = None
    if args.lock_file:
        lock_fh = acquire_flock(Path(args.lock_file))
        if lock_fh is None:
            print(
                f"ERROR: could not acquire flock on {args.lock_file} within "
                f"{LOCK_RETRY_SECONDS}s (another foundry job likely running)",
                file=sys.stderr,
            )
            return 3
        print(f"[lock] acquired {args.lock_file}", file=sys.stderr)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanning {raw_root}")

    if args.date:
        date_dirs = [raw_root / args.date]
        if not date_dirs[0].is_dir():
            print(f"ERROR: date-dir not found: {date_dirs[0]}", file=sys.stderr)
            return 1
    else:
        date_dirs = sorted(p for p in raw_root.iterdir() if p.is_dir())

    total_mismatch = 0
    total_fixed = 0
    all_warnings: list[str] = []

    for date_dir in date_dirs:
        m, f, w = reconcile_date_dir(date_dir, args.apply)
        total_mismatch += m
        total_fixed += f
        all_warnings.extend(w)

    print(f"\n=== SUMMARY ===")
    print(f"date-dirs scanned: {len(date_dirs)}")
    print(f"mismatches found:  {total_mismatch}")
    if args.apply:
        print(f"mismatches fixed:  {total_fixed}")
    if all_warnings:
        print(f"\nWARNINGS ({len(all_warnings)}):")
        for w in all_warnings:
            print(w)

    if not args.apply and total_mismatch > 0:
        print(f"\nDry-run complete. Re-run with --apply to write changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
