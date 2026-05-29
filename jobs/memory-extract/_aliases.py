r"""Aliases-loading primitives for obsidian-memory pipeline.

Shared between ``extract.py`` (LLM vocabulary-injection + post-LLM canonical-
mapping) and the ``memory-sources-append`` CLI (resolving note frontmatter
topics -> canonical compiled-file names).

Split out from the former ``_k6_source_append.py`` 2026-05-24 as part of
cortex-memory-v2 Phase 200 Substep 6c. The Sources-append surface was
dropped in v2 (compile-pass owns sources; extract.py and the CLI no longer
mutate compiled/); only the aliases-loading primitives remain.

Symbols:
  - ``load_aliases(memory_dir)`` -> (alias_map, canonical_list, status)
  - ``AliasesError`` raised for corrupt YAML / schema-version mismatch
  - ``ALIASES_FILENAME`` / ``MIN_ALIASES_SCHEMA_VERSION`` constants

Any change to aliases schema MUST be coordinated with consumers - verify
with ``grep -r "def load_aliases\|class AliasesError" scripts/memory/`` ->
exactly one hit per symbol (this file).
"""
from __future__ import annotations

from pathlib import Path

ALIASES_FILENAME = "aliases.yaml"
MIN_ALIASES_SCHEMA_VERSION = 1


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
