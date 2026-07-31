#!/usr/bin/env python3
"""_extracted_entries: the shared parse layer for ``extracted/`` quarter-files.

The shared-entry-parser of PLAN-memory-multilayer-retrieval. Owns the ENTIRE
parse layer for the extracted memory layer - the heading regex, the ``Entry``
model, ``parse_quarter_file()``, ``scan_extracted()`` - plus the merge primitives
that operate on an ``Entry`` (``wikilink_target()``, ``merged_topics()``,
``merged_sources_after()``, ``spec48_citations()``).

These lived in ``reorganize-extracted-dedup.py`` until the retrieval index needed
the same corpus. That script now imports them and re-exports for its existing
callers; this module is the single home. Consumers must NEVER re-implement entry
parsing - ``_retrieval_index.py`` and the read-time clustering both read through
here, so a format fix lands once.

Scope: ``extracted/`` only. Parsing of ``compiled/`` frontmatter stays with its
own consumers (``compiled_source_index()`` in the dedup script, the corpus
builder in ``_retrieval_index.py``) - the two layers have different roles and no
shared schema. ``extracted/`` stands on ``schema_version: 1`` and the format is
locked (fredet egenskap A4): only additive optional metadata lines are permitted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Format constants (extracted/ schema_version 1 - locked, additive-only)
# ---------------------------------------------------------------------------

ENTRY_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2}) - (.+)$", re.MULTILINE)
TOPICS_LINE_RE = re.compile(r"^topics:\s*(.+)$", re.MULTILINE)
SOURCE_LINE_RE = re.compile(r"^source:\s*(.+)$", re.MULTILINE)
MERGED_SOURCES_LINE_RE = re.compile(r"^merged_sources:\s*(.+)$", re.MULTILINE)
SUPERSEDES_LINE_RE = re.compile(r"^supersedes:\s*(.+)$", re.MULTILINE)
TOPIC_TAG_RE = re.compile(r"¤[a-z0-9][a-z0-9-]*")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
QUARTER_FILE_RE = re.compile(
    r"^(observation|decision|learning|error|pattern|intent)-(\d{4})-Q([1-4])\.md$"
)
# Word-tokenizer for body Jaccard. Unicode \w keeps Norwegian and prose intact;
# bodies are a mix of English and Norwegian technical text.
BODY_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Metadata lines that sit between the heading and the body. Kept as one tuple so
# a new optional line (A4: additive only) is taught to every consumer at once.
METADATA_PREFIXES = ("topics:", "source:", "merged_sources:", "supersedes:")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def tokenize_body(body: str) -> set[str]:
    """Return the set of lowercased word-tokens in an entry body."""
    return {t for t in BODY_TOKEN_RE.findall(body.lower()) if t}


def wikilink_target(wikilink: str) -> str:
    """``[[2026-05-08/abc-def]]`` -> ``2026-05-08/abc-def`` (path kept).

    Returns the raw inner target (before any ``#anchor`` / ``|alias``) so two
    source wikilinks can be compared for SPEC #48 citation-matching, and so a
    retrieval hit can drill down to the ``raw/`` file the entry came from.
    """
    m = WIKILINK_RE.search(wikilink)
    return m.group(1).strip() if m else wikilink.strip()


# ---------------------------------------------------------------------------
# Entry model + parsing
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """One ``## YYYY-MM-DD - slug`` heading-block in an extracted quarter-file."""

    file: Path
    type: str
    quarter: str
    date: str
    slug: str
    topics: list[str]              # full ¤-tag list (order preserved)
    source: str                   # full ``[[...]]`` wikilink string (may be "")
    merged_sources: list[str]     # existing ``[[...]]`` strings (transitive)
    body: str
    block: str                    # full original block text (heading -> next)
    order: int                    # scan order index (file name, then position)
    # Additive optional field (A4): the slug of an earlier entry this one
    # revises. Defaulted so every existing construction keeps working.
    supersedes: str = ""
    _tokens: set[str] = field(default_factory=set, repr=False)

    @property
    def ident(self) -> str:
        """Stable cross-rescan identity for one entry."""
        return f"{self.type}:{self.date}:{self.slug}:{wikilink_target(self.source)}"


def _block_body(block: str) -> str:
    """Body text of a block: all lines after the heading, minus the entry
    metadata lines (topics/source/merged_sources)."""
    lines = block.splitlines()[1:]
    kept = [ln for ln in lines if not ln.startswith(METADATA_PREFIXES)]
    return "\n".join(kept).strip()


def parse_quarter_file(text: str, path: Path, type_name: str, quarter: str,
                       start_order: int) -> tuple[str, list[Entry]]:
    """Split a quarter-file into (header, [Entry]). Header is everything before
    the first entry-heading (frontmatter + intro callout)."""
    matches = list(ENTRY_HEADING_RE.finditer(text))
    if not matches:
        return text, []
    header = text[: matches[0].start()]
    entries: list[Entry] = []
    for i, m in enumerate(matches):
        date = m.group(1)
        slug = m.group(2).strip()
        s = m.start()
        e = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[s:e].strip("\n")
        topics_m = TOPICS_LINE_RE.search(block)
        topics = TOPIC_TAG_RE.findall(topics_m.group(1)) if topics_m else []
        source_m = SOURCE_LINE_RE.search(block)
        source = ""
        if source_m:
            wl = WIKILINK_RE.search(source_m.group(1))
            source = f"[[{wl.group(1).strip()}]]" if wl else source_m.group(1).strip()
        merged_m = MERGED_SOURCES_LINE_RE.search(block)
        merged_sources = (
            [f"[[{t}]]" for t in WIKILINK_RE.findall(merged_m.group(1))]
            if merged_m else []
        )
        supersedes_m = SUPERSEDES_LINE_RE.search(block)
        supersedes = supersedes_m.group(1).strip() if supersedes_m else ""
        body = _block_body(block)
        entries.append(Entry(
            file=path, type=type_name, quarter=quarter, date=date, slug=slug,
            topics=topics, source=source, merged_sources=merged_sources,
            body=body, block=block, order=start_order + i,
            supersedes=supersedes, _tokens=tokenize_body(body),
        ))
    return header, entries


def scan_extracted(extracted_dir: Path) -> tuple[list[Entry], dict[Path, str]]:
    """Return (all entries, {file: header}) for every quarter-file."""
    entries: list[Entry] = []
    headers: dict[Path, str] = {}
    order = 0
    if not extracted_dir.exists():
        return entries, headers
    for p in sorted(extracted_dir.glob("*.md")):
        qm = QUARTER_FILE_RE.match(p.name)
        if not qm:
            continue
        type_name = qm.group(1)
        quarter = f"{qm.group(2)}-Q{qm.group(3)}"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        header, file_entries = parse_quarter_file(text, p, type_name, quarter, order)
        headers[p] = header
        entries.extend(file_entries)
        order += len(file_entries)
    return entries, headers


# ---------------------------------------------------------------------------
# Merge primitives (operate on Entry; used by the dedup manifest + clustering)
# ---------------------------------------------------------------------------

def merged_topics(keep: Entry, delete: Entry) -> list[str]:
    """Union of topic tags, kept-order first then delete-only tags."""
    out = list(keep.topics)
    seen = set(keep.topics)
    for t in delete.topics:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def merged_sources_after(keep: Entry, delete: Entry) -> list[str]:
    """The kept entry's merged_sources list after the merge: existing +
    delete.source + delete.merged_sources, deduped, excluding keep.source."""
    out: list[str] = []
    seen: set[str] = set()
    for wl in (*keep.merged_sources, delete.source, *delete.merged_sources):
        if not wl:
            continue
        tgt = wikilink_target(wl)
        if not tgt or tgt == wikilink_target(keep.source) or tgt in seen:
            continue
        seen.add(tgt)
        out.append(f"[[{tgt}]]")
    return out


def spec48_citations(entry: Entry,
                     source_index: dict[str, list[dict]]) -> list[dict]:
    """Compiled chunks that cite ``entry``'s source wikilink (SPEC #48).

    ``source_index`` is built by the caller that owns compiled/-parsing
    (``compiled_source_index()`` in reorganize-extracted-dedup.py).
    """
    if not entry.source:
        return []
    return source_index.get(wikilink_target(entry.source), [])
