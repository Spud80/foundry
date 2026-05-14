"""K6 Sources-append shared library.

Authoritative implementation of the K6 cross-project Sources-append contract:
appending source-links to the ``## Sources`` section of
``compiled/<canonical>.md`` files in an atomic, idempotent, concurrency-safe
manner.

Per PLAN-obsidian-memory G3a-4 Runde 10 acceptance ("ekte ekstraksjon, ikke
kopi"):

- ``extract.py`` imports ``append_to_compiled_sources`` from this module so
  the foundry-side extract-cron and the CLI consumers share a single source
  of truth.
- The ``memory-sources-append`` CLI is a thin wrapper that loads the note's
  frontmatter, resolves topics through ``aliases.yaml``, and calls
  ``append_to_compiled_sources`` once per canonical.

Any change to atomic-rename / flock mechanics here MUST update both flows in
the same commit. Verify with ``grep -r "def append_to_compiled_sources"
scripts/`` -> exactly one hit (this file).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_SOURCES_HEADER_RE = re.compile(r"^## Sources\s*$", re.M)

try:
    import fcntl as _fcntl  # POSIX file locks
    _HAS_FCNTL = True
except ImportError:  # Windows local smoke-tests
    _fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False


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


def _insert_under_sources(content: str, line: str) -> str:
    """Return content with ``line`` appended at end of ## Sources section.

    Creates the section at EOF if missing. The line is inserted without
    surrounding blank lines (Obsidian vault rule: no blank between list items).
    """
    match = _SOURCES_HEADER_RE.search(content)
    if not match:
        # No Sources section yet - append at EOF
        sep = "" if content.endswith("\n") else "\n"
        return content + sep + "\n## Sources\n\n" + line + "\n"
    header_end = match.end()
    # Find next H2 after ## Sources, or EOF
    next_h2_match = re.search(r"^## ", content[header_end:], re.M)
    if next_h2_match:
        section_end = header_end + next_h2_match.start()
        section_body = content[header_end:section_end].rstrip()
        new_section = section_body + "\n" + line + "\n\n"
        return content[:header_end] + new_section + content[section_end:]
    # ## Sources is the last section
    section_body = content[header_end:].rstrip()
    return content[:header_end] + section_body + "\n" + line + "\n"


def append_to_compiled_sources(compiled_path: Path, source_link: str) -> str:
    """Atomic-append source-link to ## Sources in compiled file.

    Returns:
      'appended'         - line was added
      'already-present'  - idempotent skip (line already in file)
      'missing'          - compiled file does not exist (no-op)
      'error: <msg>'     - transient I/O / lock failure; caller logs but does
                           not abort (per contract: missing/transient compiled
                           updates are no-op; ground-truth in extracted/)

    Uses ``fcntl.flock`` on POSIX with a sidecar lock file to serialise
    concurrent writes against the same compiled file. The atomic rename via
    ``atomic_write`` keeps readers consistent. On non-POSIX platforms (local
    Windows smoke-tests) the lock is a best-effort no-op; production runs on
    foundry are Linux.
    """
    if not compiled_path.exists():
        return "missing"
    lock_path = compiled_path.parent / f".{compiled_path.name}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Create lock file if missing; open r+ so we can hold flock without truncating
        if not lock_path.exists():
            lock_path.touch(exist_ok=True)
    except OSError as e:
        return f"error: lock setup: {e}"
    try:
        with open(lock_path, "r", encoding="utf-8") as lockf:
            if _HAS_FCNTL:
                _fcntl.flock(lockf.fileno(), _fcntl.LOCK_EX)
            try:
                existing = compiled_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return "missing"
            except OSError as e:
                return f"error: read: {e}"
            if source_link in existing:
                return "already-present"
            new_content = _insert_under_sources(existing, source_link)
            try:
                atomic_write(compiled_path, new_content)
            except OSError as e:
                return f"error: write: {e}"
            return "appended"
    except OSError as e:
        return f"error: lock: {e}"
