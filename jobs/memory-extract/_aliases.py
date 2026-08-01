r"""Aliases-loading primitives for obsidian-memory pipeline.

The intended single home for reading ``aliases.yaml``: a consumer that needs
alias-, canonical- or tier-data calls :func:`load_aliases` and reads fields off
the returned :class:`Aliases` rather than re-parsing the YAML.

**Not yet true of every consumer.** As of 2026-08-01 eight script-local loader
variants still exist and are scheduled for migration in phases 200-300 of
memory-shared-primitives; three of them still coerce an unknown tier to
``signal``. Until that lands, the verification command at the bottom of this
docstring reports more than one hit, and that is expected rather than a
regression.

Split out from the former ``_k6_source_append.py`` 2026-05-24 as part of
cortex-memory-v2 Phase 200 Substep 6c. The Sources-append surface was
dropped in v2 (compile-pass owns sources; extract.py and the CLI no longer
mutate compiled/); only the aliases-loading primitives remain.

Widened 2026-08-01 (memory-shared-primitives fase 100) from a three-tuple
over a *directory* to a dataclass over an *aliases-file path*. The old shape
was the reason eight consumers grew their own loader: four of them expose an
``--aliases-file`` override that a directory argument cannot express, and the
positional tuple made every new information need a new signature.

Symbols:
  - ``load_aliases(aliases_path, *, strict=False)`` -> ``Aliases``
  - ``Aliases`` dataclass with ``canonicals()``, ``resolve()``, ``slugs_for()``
  - ``AliasesError`` raised for corrupt YAML / schema mismatch (see ``strict``)
    and, unconditionally, for a tier value outside ``VALID_TIERS``
  - ``ALIASES_FILENAME`` / ``MIN_ALIASES_SCHEMA_VERSION`` / ``VALID_TIERS``

Two failure classes, deliberately not merged (charter hjørne-regel B1):
``strict`` governs *file state* - missing, empty, unreadable or corrupt - where
an unattended nightly job is right to degrade and the extract job is right to
exit 2. A tier value outside the enum is *data content*: a typo in a
hand-curated file, which fails hard in both modes rather than being silently
rewritten to ``signal``.

Any change to aliases schema MUST be coordinated with consumers - verify
with ``grep -r "def load_aliases\|class AliasesError" scripts/memory/`` ->
exactly one hit per symbol (this file).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ALIASES_FILENAME = "aliases.yaml"
MIN_ALIASES_SCHEMA_VERSION = 1
VALID_TIERS = ("signal", "noise", "archived")

# Canonicals carrying no explicit tier read as signal. This is absence, not a
# bad value: every pre-existing variant defaulted the same way, and A3 is about
# values outside the enum, which raise instead.
DEFAULT_TIER = "signal"


class AliasesError(Exception):
    """Raised for hard-fail aliases conditions (corrupt YAML, schema mismatch,
    tier outside the enum).

    Caller exits 2 + notifies FATAL.
    """


@dataclass(frozen=True)
class Aliases:
    """Parsed ``aliases.yaml``, in the shapes consumers actually ask for.

    ``alias_to_canonical`` maps every alias AND every canonical to its
    canonical, so a caller can resolve any tag with one dict-lookup. The
    canonical self-entries are what makes that single-lookup form work; a
    consumer that wants aliases *without* the self-map reads
    ``canonical_to_aliases`` instead. Those are the two self-map conventions
    found among the replaced variants, and both are served without a flag.

    ``canonical_to_tier`` holds a tier for every canonical whose entry is a
    mapping; canonicals with a non-mapping entry are absent from it, matching
    every variant this replaced.

    ``status`` is the file-state outcome: ``ok``, ``missing``, ``empty`` or
    ``error``. Only ``strict=False`` can produce ``error`` - in strict mode the
    same condition raises.
    """

    alias_to_canonical: dict[str, str] = field(default_factory=dict)
    canonical_to_tier: dict[str, str] = field(default_factory=dict)
    canonical_to_aliases: dict[str, list[str]] = field(default_factory=dict)
    status: str = "missing"

    def canonicals(self) -> list[str]:
        """Sorted canonical slugs."""
        return sorted(self.canonical_to_aliases)

    def resolve(self, tag: str) -> str:
        """Canonical for ``tag``; unknown tags pass through unchanged."""
        return self.alias_to_canonical.get(tag, tag)

    def slugs_for(self, canonical: str) -> list[str]:
        """``[canonical, *aliases]`` - the search-slug list for one topic.

        Returns ``[canonical]`` for an unknown topic, which is the fallback the
        two ``load_aliases_for_topic`` variants relied on.
        """
        return [canonical, *self.canonical_to_aliases.get(canonical, [])]


def _fail(message: str, strict: bool) -> Aliases:
    """Raise in strict mode; degrade to an empty error-status load otherwise."""
    if strict:
        raise AliasesError(message)
    return Aliases(status="error")


def load_aliases(aliases_path: Path, *, strict: bool = False) -> Aliases:
    """Load ``aliases.yaml`` from ``aliases_path`` (the FILE, not its directory).

    ``strict=True`` reproduces the historical hard-fail contract: corrupt YAML,
    a schema-version above ``MIN_ALIASES_SCHEMA_VERSION``, a missing PyYAML or
    an unreadable file all raise :class:`AliasesError`, and the caller maps that
    to exit 2 + (FATAL) notify. ``strict=False`` returns ``status='error'`` with
    empty maps instead, so an unattended nightly job degrades rather than dies.

    A missing file is NOT an error in either mode - it is the valid
    pre-bootstrap state and yields ``status='missing'``.

    A tier value outside :data:`VALID_TIERS` raises in BOTH modes: that is a
    typo in curated data, not a runtime disturbance (charter A3 / B1).
    """
    if not aliases_path.exists():
        return Aliases(status="missing")
    try:
        import yaml  # local import - keeps module importable without pyyaml
    except ImportError as e:
        return _fail(f"pyyaml not installed: {e}", strict)
    try:
        raw = aliases_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return _fail(f"aliases.yaml unparseable: {e}", strict)
    except OSError as e:
        return _fail(f"aliases.yaml read error: {e}", strict)
    if not isinstance(data, dict):
        return _fail("aliases.yaml root is not a mapping", strict)
    schema_v = data.get("schema_version")
    if not isinstance(schema_v, int):
        return _fail(
            f"aliases.yaml missing or non-integer schema_version (got {schema_v!r})",
            strict,
        )
    if schema_v > MIN_ALIASES_SCHEMA_VERSION:
        return _fail(
            f"aliases.yaml schema_version={schema_v} exceeds minimum-supported="
            f"{MIN_ALIASES_SCHEMA_VERSION} - coordinated bump required",
            strict,
        )
    canonicals = data.get("canonicals", {})
    if not isinstance(canonicals, dict):
        return _fail("aliases.yaml.canonicals is not a mapping", strict)
    if not canonicals:
        return Aliases(status="empty")

    alias_to_canonical: dict[str, str] = {}
    canonical_to_tier: dict[str, str] = {}
    canonical_to_aliases: dict[str, list[str]] = {}
    for canon_slug, info in canonicals.items():
        if not isinstance(canon_slug, str) or not canon_slug:
            continue
        alias_to_canonical[canon_slug] = canon_slug  # idempotent self-map
        canonical_to_aliases[canon_slug] = []
        if not isinstance(info, dict):
            continue
        # `or` and not `.get(key, default)`: a hand-edited `tier:` with nothing
        # after it parses as None, and an empty string is the same statement.
        # Both are the field being unstated, which A3 does not cover - A3 is
        # about a value outside the enum, i.e. a typo. Defaulting them keeps the
        # behaviour the replaced variants had, where every falsy tier read as
        # signal; a genuine wrong value is truthy and still raises below.
        tier = info.get("tier") or DEFAULT_TIER
        if tier not in VALID_TIERS:
            # Not behind `strict`: see the module docstring on the two failure
            # classes. Silently coercing this to signal is the exact failure the
            # consolidation removes.
            raise AliasesError(
                f"aliases.yaml canonical {canon_slug!r} has tier={tier!r} "
                f"not in {VALID_TIERS}"
            )
        canonical_to_tier[canon_slug] = tier
        aliases = info.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            if isinstance(alias, str) and alias and alias != canon_slug:
                alias_to_canonical[alias] = canon_slug
                canonical_to_aliases[canon_slug].append(alias)
    return Aliases(
        alias_to_canonical=alias_to_canonical,
        canonical_to_tier=canonical_to_tier,
        canonical_to_aliases=canonical_to_aliases,
        status="ok",
    )
