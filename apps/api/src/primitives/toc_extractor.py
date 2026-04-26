"""TOC extraction (spec §3.4, §4.1.1 step 7).

Parses a book's combined markdown into a `TocEntryDraft` list — a flat,
ordered list with parent pointers — ready for persistence as `TocEntry` rows.

Two detection strategies run in order:

1. **Explicit Contents block.** If the markdown contains a "Contents" or
   "Table of Contents" heading followed by a block of dotted-leader entries
   ("Chapter 1 . . . 5") OR a bare "Title — Page" listing, we prefer this as
   the authoritative source of titles + page numbers.

2. **Heading-based fallback.** Otherwise (or if the Contents block didn't
   parse), we scan for markdown headings (`^#{1,6} `) and snap each heading's
   `source_page` to the nearest preceding `PageMarker.source_page`. Headings
   that appear before the first page marker are stamped with the first marker's
   page — this is mildly wrong for front-matter but better than -1.

Parent relationships are derived from heading level alone — a `##` after the
nearest-preceding `#` is that heading's child; a `###` is the child of the
nearest-preceding `##`; and so on. We return `parent_offset` as the index into
the returned list where the parent lives so the caller can build FK pointers
at persistence time (each entry is saved in order, so forward references
cannot occur).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from primitives.ingest import PageMarker

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TocEntryDraft:
    """Pre-persistence form of a TOC entry.

    `parent_offset` indexes earlier in the same returned list; None marks a
    root-level (level-1) entry. Callers iterate in order and resolve FK ids
    from the already-persisted ancestor.
    """

    level: int
    title: str
    source_page: int
    order_index: int
    parent_offset: int | None


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------


# Contents-block opener: a heading line "Contents" / "Table of Contents" with
# optional emphasis. Case-insensitive. The surrounding `\s*` plus `.+?`
# capture accepts "Table of Contents" and "CONTENTS" variants.
_CONTENTS_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*{1,3}\s*)?"
    r"(?:Table\s+of\s+)?Contents"
    r"\s*(?:\*{1,3})?\s*$",
    re.IGNORECASE,
)

# Dotted-leader TOC line: "Chapter 1   . . .   5" or "Introduction ...... 1".
# We match anything up to the trailing integer, tolerating dots / spaces /
# tabs / underscores in the filler.
_CONTENTS_ROW_RE = re.compile(
    r"^\s*(?P<title>\S.*?\S)"
    r"\s*[\.\s_]{2,}\s*"
    r"(?P<page>\d+)\s*$"
)

# Bare-pipe TOC line variant: "Chapter 1 | 5"
_CONTENTS_PIPE_RE = re.compile(
    r"^\s*(?P<title>\S.*?)\s*\|\s*(?P<page>\d+)\s*$"
)

# Generic markdown heading.
_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$"
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_toc(
    markdown: str,
    page_markers: list[PageMarker],
) -> list[TocEntryDraft]:
    """Parse a TOC out of `markdown`, snapping to provided `page_markers`.

    Strategy:
      1. If an explicit "Contents" block is present AND yields at least one
         parseable row, return those rows (levels inferred from leading
         whitespace / `#` prefix).
      2. Otherwise, scan every `^#{1,6} ` heading that falls within the
         body of the markdown and snap each to the nearest preceding page
         marker. Parent-child links come from heading level.

    Returns [] if neither strategy finds anything.
    """
    if not markdown.strip():
        return []

    # ----- Strategy 1: explicit Contents block
    contents_rows = _parse_contents_block(markdown)
    if contents_rows:
        return _assign_parents(contents_rows)

    # ----- Strategy 2: inline headings, snapped to page markers
    return _extract_from_headings(markdown, page_markers)


# ---------------------------------------------------------------------------
# Strategy 1: explicit Contents block
# ---------------------------------------------------------------------------


@dataclass
class _RawRow:
    level: int
    title: str
    source_page: int


def _parse_contents_block(markdown: str) -> list[_RawRow]:
    """Find "Contents" / "Table of Contents" and parse the block that follows.

    Returns an empty list if either the header isn't found, or the block
    beneath it has no parseable rows. A successfully-parsed Contents block
    stops at the first blank-line-separated group that doesn't match the
    row regex — which is how we distinguish the TOC from the body that
    follows.
    """
    lines = markdown.splitlines()

    # Find the Contents header line (first occurrence wins — TOCs in
    # chapter-level "contents of this chapter" sub-blocks are ignored, which
    # is what we want since we're extracting the book-level TOC).
    start = -1
    for idx, line in enumerate(lines):
        if _CONTENTS_HEADER_RE.match(line):
            start = idx + 1
            break
    if start == -1:
        return []

    # Skip blank lines right after the header.
    while start < len(lines) and lines[start].strip() == "":
        start += 1

    rows: list[_RawRow] = []
    blank_streak = 0

    for idx in range(start, len(lines)):
        line = lines[idx]
        if line.strip() == "":
            blank_streak += 1
            # Two consecutive blanks OR a blank after any rows = end of block.
            if rows and blank_streak >= 1:
                # But tolerate a single blank inside a Contents block — some
                # books put blank lines between Parts. Only a heading-shaped
                # non-TOC line or two blanks ends the block.
                pass
            continue
        blank_streak = 0

        # A subsequent markdown heading (not matching a row regex) ends the
        # Contents block.
        if _HEADING_RE.match(line) and not (
            _CONTENTS_ROW_RE.match(line) or _CONTENTS_PIPE_RE.match(line)
        ):
            # Unless it IS a TOC-style heading listed inside the contents.
            heading_match = _HEADING_RE.match(line)
            heading_text = heading_match.group("text") if heading_match else ""
            if ". . ." not in heading_text and "…" not in heading_text:
                # Looks like body content, not TOC; stop.
                if rows:
                    break
                # No rows yet? Maybe the "Contents" header itself was a
                # decorative block; keep looking for rows below it.
                continue

        row = _parse_contents_row(line)
        if row is not None:
            rows.append(row)
        elif rows:
            # A non-blank, non-row line after we started collecting rows —
            # tolerate up to 2 such lines, they're usually column headers or
            # "Part I" section dividers we don't capture here.
            continue

    return rows


def _parse_contents_row(line: str) -> _RawRow | None:
    """Parse a single TOC row into (level, title, source_page) if possible.

    Level is inferred from (a) leading whitespace (4-space indent per level),
    (b) leading `#` markers if present. Default level=1.
    """
    # Strip a leading `#` markdown heading if present and use it for level.
    heading_match = _HEADING_RE.match(line)
    content_line = line
    explicit_level: int | None = None
    if heading_match is not None:
        explicit_level = len(heading_match.group("hashes"))
        content_line = heading_match.group("text")

    # Now match the row body.
    match = _CONTENTS_ROW_RE.match(content_line) or _CONTENTS_PIPE_RE.match(content_line)
    if match is None:
        return None

    title = match.group("title").strip().strip("*_").strip()
    if not title:
        return None
    try:
        page = int(match.group("page"))
    except ValueError:
        return None

    if explicit_level is not None:
        level = explicit_level
    else:
        # Infer from leading whitespace on the original line.
        indent = len(line) - len(line.lstrip(" "))
        level = 1 + (indent // 4)
        level = min(max(level, 1), 6)

    return _RawRow(level=level, title=title, source_page=page)


# ---------------------------------------------------------------------------
# Strategy 2: inline headings
# ---------------------------------------------------------------------------


def _extract_from_headings(
    markdown: str,
    page_markers: list[PageMarker],
) -> list[TocEntryDraft]:
    """Scan for markdown headings and snap to page markers.

    We skip any heading that lives inside a Contents-style section (e.g.,
    directly following a "Contents" header) so heading-based extraction
    doesn't double-count a TOC we already tried and rejected.
    """
    lines = markdown.splitlines()

    rows: list[_RawRow] = []
    for line_idx, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        level = len(match.group("hashes"))
        title = match.group("text").strip()
        if not title:
            continue
        # Filter the "Contents" header itself — if it made it here, Strategy
        # 1 failed to parse the block, so treating the header as a TOC entry
        # would be confusing. Simpler to just skip.
        if _CONTENTS_HEADER_RE.match(line):
            continue
        source_page = _snap_to_marker(line_idx, page_markers)
        rows.append(_RawRow(level=level, title=title, source_page=source_page))

    if not rows:
        return []
    return _assign_parents(rows)


def _snap_to_marker(line_idx: int, markers: list[PageMarker]) -> int:
    """Return the source_page of the nearest preceding page marker.

    If `line_idx` precedes all markers, use the first marker's page (so
    front-matter headings don't get -1). If there are no markers at all,
    return 0 — caller must tolerate this.
    """
    if not markers:
        return 0
    best = markers[0].source_page
    for m in markers:
        if m.line_index <= line_idx:
            best = m.source_page
        else:
            break
    return best


# ---------------------------------------------------------------------------
# Shared: parent assignment from level + order
# ---------------------------------------------------------------------------


def _assign_parents(rows: list[_RawRow]) -> list[TocEntryDraft]:
    """Walk the row list in order, tracking the last-seen entry at each
    level. The parent of row `r` is the last-seen entry at level `< r.level`.

    This is the standard outline-to-tree algorithm (`-1` = root).
    """
    out: list[TocEntryDraft] = []
    # parent_stack[level] = offset into `out` of the most recent entry at that
    # level (or a deeper level). When we see level L, we pop stack entries
    # with level >= L before recording.
    last_at_level: dict[int, int] = {}

    for i, row in enumerate(rows):
        # Find parent: scan `last_at_level` for the deepest level < row.level.
        parent_offset: int | None = None
        for lvl in range(row.level - 1, 0, -1):
            if lvl in last_at_level:
                parent_offset = last_at_level[lvl]
                break

        out.append(
            TocEntryDraft(
                level=row.level,
                title=row.title,
                source_page=row.source_page,
                order_index=i,
                parent_offset=parent_offset,
            )
        )

        # Record this entry as the latest at its own level, and clear any
        # deeper levels (they can't be ancestors of later siblings).
        last_at_level[row.level] = i
        for deeper in list(last_at_level.keys()):
            if deeper > row.level:
                del last_at_level[deeper]

    return out


__all__ = ["TocEntryDraft", "extract_toc"]
