"""K6 Sources-append shared library (+ aliases-loading primitives).

Authoritative implementation of two K6-related contracts:

1. The K6 cross-project Sources-append contract: appending source-links to
   the ``## Sources`` section of ``compiled/<canonical>.md`` files in an
   atomic, idempotent, concurrency-safe manner.

2. The aliases-loading primitives (``load_aliases`` + ``AliasesError`` +
   constants) used by both ``extract.py`` (LLM vocabulary-injection +
   post-LLM canonical-mapping) and the ``memory-sources-append`` CLI
   (resolving note frontmatter topics -> canonical compiled-file names).

Per PLAN-obsidian-memory G3a-4 Runde 10 acceptance ("ekte ekstraksjon, ikke
kopi"):

- ``extract.py`` imports ``append_to_compiled_sources``, ``load_aliases``,
  and ``AliasesError`` from this module (re-exported for backwards-compat
  with smoke_k5_k6 and other ``extract.<symbol>`` callers).
- The ``memory-sources-append`` CLI is a thin wrapper that loads the note's
  frontmatter, resolves topics through ``aliases.yaml`` (via
  ``load_aliases`` from this module - NOT via extract.py, since
  ``/usr/local/bin/`` deploy does not carry extract.py), and calls
  ``append_to_compiled_sources`` once per canonical.

Any change to atomic-rename / flock mechanics OR aliases schema here MUST
update both flows in the same commit. Verify with
``grep -r "def append_to_compiled_sources\|def load_aliases\|class AliasesError" scripts/memory/``
-> exactly one hit per symbol (this file).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_SOURCES_HEADER_RE = re.compile(r"^## Sources\s*$", re.M)

ALIASES_FILENAME = "aliases.yaml"
MIN_ALIASES_SCHEMA_VERSION = 1

try:
    import fcntl as _fcntl  # POSIX file locks
    _HAS_FCNTL = True
except ImportError:  # Windows local smoke-tests
    _fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False


class AliasesError(Exception):
    """Raised for hard-fail aliases conditions (corrupt YAML, schema mismatch).

    Caller exits 2 + notifies FATAL.
    """


def load_aliases(memory_dir: Path) -> tuple[dict[str, str], list[str], str]:
    """Load aliases.yaml. Returns (alias_to_canonical_map, canonical_list, status).

    Map contains alias-slug -> canonical-slug PLUS canonical -> canonical
    self-entries (idempotent for safety-net post-mapping).

    Status values:
      'ok'              - loaded successfully, alias_map populated
      'missing'         - aliases.yaml not present in memory_dir (graceful degrade)
      'empty'           - file loaded but canonicals: {} (no normalisation, no warn)

    Raises AliasesError on corrupt YAML or schema-version mismatch (file
    schema_version > MIN_ALIASES_SCHEMA_VERSION). Caller maps to exit 2 +
    (FATAL) notify.
    """
    aliases_path = memory_dir / ALIASES_FILENAME
    if not aliases_path.exists():
        return ({}, [], "missing")
    try:
        import yaml  # local import - keeps module importable without pyyaml
    except ImportError as e:
        raise AliasesError(f"pyyaml not installed: {e}")
    try:
        raw = aliases_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise AliasesError(f"aliases.yaml unparseable: {e}")
    except OSError as e:
        raise AliasesError(f"aliases.yaml read error: {e}")
    if not isinstance(data, dict):
        raise AliasesError("aliases.yaml root is not a mapping")
    schema_v = data.get("schema_version")
    if not isinstance(schema_v, int):
        raise AliasesError(
            f"aliases.yaml missing or non-integer schema_version (got {schema_v!r})"
        )
    if schema_v > MIN_ALIASES_SCHEMA_VERSION:
        raise AliasesError(
            f"aliases.yaml schema_version={schema_v} exceeds minimum-supported="
            f"{MIN_ALIASES_SCHEMA_VERSION} - coordinated bump required"
        )
    canonicals = data.get("canonicals", {})
    if not isinstance(canonicals, dict):
        raise AliasesError("aliases.yaml.canonicals is not a mapping")
    if not canonicals:
        return ({}, [], "empty")
    alias_map: dict[str, str] = {}
    canonical_list: list[str] = []
    for canon_slug, info in canonicals.items():
        if not isinstance(canon_slug, str) or not canon_slug:
            continue
        canonical_list.append(canon_slug)
        alias_map[canon_slug] = canon_slug  # idempotent self-map
        if not isinstance(info, dict):
            continue
        aliases = info.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            if isinstance(alias, str) and alias:
                alias_map[alias] = canon_slug
    return (alias_map, sorted(canonical_list), "ok")


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
