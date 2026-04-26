"""Rule-based block segmentation for casebook markdown (spec §4.1.3).

Takes the Marker-produced markdown of a single printed page plus the source page
number, and emits a list of typed `SegmentedBlock` dicts ready to persist as
`data.models.Block` rows.

This module is the rule-based layer. The LLM fallback (spec §4.1.3, prompt
`prompts/block_segmentation_fallback.md`) is a separate Phase 2 concern — when
a casebook uses an odd convention this layer can't recognize, the orchestrator
escalates there. Per spec §3.5 the block types are fixed; per spec §2.3 the
source_page on every block must survive intact to downstream retrieval.

Scope: one page at a time. A case opinion that flows across a page boundary
is emitted here as an opinion that ends at the page boundary; stitching across
pages is the job of `ingest_book` (not implemented in this module).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from data.models import BlockType

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentedBlock:
    """An immutable, DB-ready description of a typed block within one page.

    `order_index` is the 0-based position in the page. `markdown` is the
    verbatim slice (trailing whitespace stripped per spec §4.1.3 rule 3), and
    `block_metadata` holds the per-type metadata enumerated in spec §3.5.
    """

    type: BlockType
    order_index: int
    source_page: int
    markdown: str
    block_metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Regex vocabulary
# ---------------------------------------------------------------------------

# Party-v-Party heading. Spec §4.1.3 gives the base pattern, which we allow to
# be prefixed by up to two markdown heading markers or a bold-emphasis prefix
# such as `**` (Marker occasionally wraps case names in bold). The core
# capture groups `left` and `right` drive `case_name`.
_CASE_NAME_CHARS = r"[A-Za-z0-9 .’'\-&,]"
_CASE_HEADER_CORE = (
    rf"(?P<left>[A-Z]{_CASE_NAME_CHARS}+?)\s+v\.\s+(?P<right>[A-Z]{_CASE_NAME_CHARS}+?)"
)
# Allowed leading prefixes (markdown heading markers, bold stars, spaces).
_CASE_HEADER_PREFIX = r"(?:\s*(?:#{1,3}\s*|\*{2,4}\s*))?"
# Allowed trailing decoration (closing stars, trailing heading whitespace).
_CASE_HEADER_SUFFIX = r"(?:\s*\*{2,4})?\s*$"

_CASE_HEADER_RE = re.compile(
    rf"^{_CASE_HEADER_PREFIX}{_CASE_HEADER_CORE}{_CASE_HEADER_SUFFIX}",
)

# Court-and-year line (e.g., "Supreme Court of Missouri, 1948"). Accept an
# optional trailing period/dot, which Marker sometimes emits.
_COURT_LINE_RE = re.compile(
    r"^\s*(?P<court>[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z.]*)*"
    r"\s+Court\s+of\s+[A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+)*)"
    r",\s*(?P<year>\d{4})\.?\s*$"
)

# Citation patterns (spec §4.1.3). Two variants — "334 U.S. 1" and
# "123 S.E.2d 456 789" style reporter citations.
_CITATION_US_RE = re.compile(r"^\s*(?P<cite>\d+\s+U\.S\.\s+\d+(?:,\s*\d+)?)\s*$")
_CITATION_REPORTER_RE = re.compile(
    r"^\s*(?P<cite>\d+\s+[A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+)*\s+\d+[a-zA-Z]*\s+\d+)\s*$"
)

# Notes-and-Questions terminator.
_NOTES_QUESTIONS_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\*{1,4}\s*)?Notes\s+and\s+Questions\s*(?:\*{1,4}\s*)?$",
    re.IGNORECASE,
)

# Numbered note starter ("1. The first note..."). Must be followed by a space
# then some text — bare "1." is rejected as a section number.
_NUMBERED_NOTE_RE = re.compile(r"^\s*(?P<number>\d+)\.\s+\S")

# Standalone problem header. Case-sensitive on "Problem" per spec §4.1.3.
_PROBLEM_HEADER_RE = re.compile(r"^\s*Problem(?:\s+\d+)?\s*[:.]?\s*$")

# Inline "Problem:" inside a numbered note. Two flavors count per spec §4.1.3:
# (1) a line starting with `Problem\s*\d*\.?\s*` (i.e., the prefix form), and
# (2) an explicit "Problem:" label — anywhere in the body, case-sensitive.
_PROBLEM_INLINE_RE = re.compile(
    r"^\s*Problem(?:\s+\d+)?\s*[:.]|Problem:", re.MULTILINE
)

# Footnote body starter: "1 Text of footnote..." (note leading digit then space).
_FOOTNOTE_LINE_RE = re.compile(r"^(?P<fn>\d+)\s+\S")

# Horizontal rule (footnote-above-rule trigger).
_HRULE_RE = re.compile(r"^\s*(?:---+|\*)\s*$")

# Generic markdown heading.
_HEADER_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")

# Figure (lone image directive).
_FIGURE_RE = re.compile(r"^\s*!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)\s*$")

# Blockquote marker.
_BLOCKQUOTE_RE = re.compile(r"^\s*>")

# GFM table alignment row: at least two `|` segments of `---` / `:---:` / etc.
_TABLE_ALIGN_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def segment_page_markdown(
    page_markdown: str,
    source_page: int,
) -> list[SegmentedBlock]:
    """Segment a single printed page's markdown into typed blocks.

    Rules in summary (spec §4.1.3):
    - Empty page → one empty `narrative_text` block.
    - Blank-line separated groups become the raw unit of detection.
    - Case headers (`Party v. Party`) plus any immediately-following court /
      citation line are a single `case_header` block; the rest of the case up
      to the next terminator is a `case_opinion` block.
    - Numbered notes (`^\\d+\\. `) after a blank line terminate opinions.
    - Footnotes at page end, preceded by `---` or `*`, are classified as such;
      otherwise they stay inside the prior block.
    - Everything else falls through to tables / figures / headers /
      blockquotes / narrative_text in that priority order.
    """
    # Spec §4.1.3 rule 1: empty pages still emit one block so downstream
    # retrieval always sees at least one hit per page.
    if page_markdown.strip() == "":
        return [
            SegmentedBlock(
                type=BlockType.NARRATIVE_TEXT,
                order_index=0,
                source_page=source_page,
                markdown="",
                block_metadata={},
            )
        ]

    groups = _split_into_groups(page_markdown)
    typed_groups = _detect_footnotes_at_end(groups)

    blocks: list[SegmentedBlock] = []
    i = 0
    order_index = 0
    while i < len(typed_groups):
        tg = typed_groups[i]
        if tg.forced_type == "footnote":
            fn_md = _strip_block(tg.text)
            fn_match = _FOOTNOTE_LINE_RE.match(fn_md.lstrip())
            fn_number = int(fn_match.group("fn")) if fn_match else 0
            blocks.append(
                SegmentedBlock(
                    type=BlockType.FOOTNOTE,
                    order_index=order_index,
                    source_page=source_page,
                    markdown=fn_md,
                    block_metadata={
                        "footnote_number": fn_number,
                        "parent_block_id": None,
                    },
                )
            )
            order_index += 1
            i += 1
            continue

        group_text = tg.text

        # ---- case_header?
        header_match = _match_case_header(group_text)
        if header_match is not None:
            header_block, consumed, opinion_meta = _build_case_header(
                typed_groups, i, header_match, source_page, order_index
            )
            blocks.append(header_block)
            order_index += 1
            i += consumed

            # Now gather the opinion body until a terminator.
            opinion_block, consumed = _build_case_opinion(
                typed_groups, i, opinion_meta, source_page, order_index
            )
            if opinion_block is not None:
                blocks.append(opinion_block)
                order_index += 1
                i += consumed
            continue

        # ---- Notes-and-Questions header — classify as plain header.
        if _is_notes_questions(group_text):
            blocks.append(
                _make_header_block(group_text, source_page, order_index)
            )
            order_index += 1
            i += 1
            continue

        # ---- numbered note (after blank line, which our group split already gave us)?
        if _NUMBERED_NOTE_RE.match(group_text):
            number_match = _NUMBERED_NOTE_RE.match(group_text)
            number = int(number_match.group("number"))  # type: ignore[union-attr]
            # Greedily absorb following groups that belong to the same note:
            # (a) a Problem line under the same note (spec §4.1.3: Problem
            # inside a numbered_note stays with the note and flips the flag),
            # (b) plain continuation prose that isn't a terminator — we stop
            # before headers, case headers, tables, blockquotes, next numbers.
            note_parts: list[str] = [group_text]
            consumed = 1
            j = i + 1
            while j < len(typed_groups):
                ntg = typed_groups[j]
                if ntg.forced_type is not None:
                    break
                ntext = ntg.text
                nfirst = ntext.lstrip("\n").splitlines()[0] if ntext else ""
                if _NUMBERED_NOTE_RE.match(ntext):
                    break
                if _match_case_header(ntext) is not None:
                    break
                if _is_notes_questions(ntext):
                    break
                if nfirst.startswith("# "):
                    break
                if _PROBLEM_HEADER_RE.match(nfirst) or _PROBLEM_INLINE_RE.match(nfirst):
                    # Absorb the Problem header/label and its body (until a
                    # hard terminator). Spec §4.1.3: a Problem inside a
                    # numbered_note's body stays with the note and flips
                    # has_problem=True.
                    note_parts.append(ntext)
                    consumed += 1
                    j += 1
                    continue
                # Anything else: stop absorbing. Keep boundaries tight so
                # unrelated narrative stays as its own block.
                break
            md = _strip_block("\n\n".join(note_parts))
            blocks.append(
                SegmentedBlock(
                    type=BlockType.NUMBERED_NOTE,
                    order_index=order_index,
                    source_page=source_page,
                    markdown=md,
                    block_metadata={
                        "number": number,
                        "has_problem": bool(_PROBLEM_INLINE_RE.search(md)),
                    },
                )
            )
            order_index += 1
            i += consumed
            continue

        # ---- standalone problem header?
        first_line = group_text.lstrip("\n").splitlines()[0] if group_text else ""
        if _PROBLEM_HEADER_RE.match(first_line):
            md = _strip_block(group_text)
            blocks.append(
                SegmentedBlock(
                    type=BlockType.PROBLEM,
                    order_index=order_index,
                    source_page=source_page,
                    markdown=md,
                    block_metadata={},
                )
            )
            order_index += 1
            i += 1
            continue

        # ---- table?
        if _is_table_group(group_text):
            rows, cols = _table_dimensions(group_text)
            blocks.append(
                SegmentedBlock(
                    type=BlockType.TABLE,
                    order_index=order_index,
                    source_page=source_page,
                    markdown=_strip_block(group_text),
                    block_metadata={"rows": rows, "cols": cols},
                )
            )
            order_index += 1
            i += 1
            continue

        # ---- figure?
        fig_match = _FIGURE_RE.match(group_text.strip())
        if fig_match is not None and len(group_text.strip().splitlines()) == 1:
            blocks.append(
                SegmentedBlock(
                    type=BlockType.FIGURE,
                    order_index=order_index,
                    source_page=source_page,
                    markdown=_strip_block(group_text),
                    block_metadata={
                        "alt": fig_match.group("alt"),
                        "src": fig_match.group("src"),
                    },
                )
            )
            order_index += 1
            i += 1
            continue

        # ---- blockquote?
        if _is_blockquote_group(group_text):
            blocks.append(
                SegmentedBlock(
                    type=BlockType.BLOCK_QUOTE,
                    order_index=order_index,
                    source_page=source_page,
                    markdown=_strip_block(group_text),
                    block_metadata={},
                )
            )
            order_index += 1
            i += 1
            continue

        # ---- markdown header (non-case)?
        header_only_match = _header_match(group_text)
        if header_only_match is not None:
            level = len(header_only_match.group("hashes"))
            text = header_only_match.group("text").strip()
            blocks.append(
                SegmentedBlock(
                    type=BlockType.HEADER,
                    order_index=order_index,
                    source_page=source_page,
                    markdown=_strip_block(group_text),
                    block_metadata={"level": level, "text": text},
                )
            )
            order_index += 1
            i += 1
            continue

        # ---- fallback: narrative_text
        blocks.append(
            SegmentedBlock(
                type=BlockType.NARRATIVE_TEXT,
                order_index=order_index,
                source_page=source_page,
                markdown=_strip_block(group_text),
                block_metadata={},
            )
        )
        order_index += 1
        i += 1

    # If the whole page reduced to nothing meaningful (all groups empty), emit
    # the empty narrative placeholder. This matches the empty-page rule.
    if not blocks:
        return [
            SegmentedBlock(
                type=BlockType.NARRATIVE_TEXT,
                order_index=0,
                source_page=source_page,
                markdown="",
                block_metadata={},
            )
        ]

    return blocks


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _TypedGroup:
    """A blank-line-separated text group plus a pre-computed forced type hint.

    `forced_type` is "footnote" when the page-end footnote heuristic applies;
    other groups are classified lazily during the main pass.
    """

    text: str
    forced_type: str | None = None


def _split_into_groups(text: str) -> list[_TypedGroup]:
    """Split page markdown into blank-line-separated groups.

    Preserves the internal newlines of each group but drops the blank-line
    separators themselves (per spec §4.1.3 rule 4).
    """
    lines = text.splitlines()
    groups: list[_TypedGroup] = []
    buffer: list[str] = []
    for line in lines:
        if line.strip() == "":
            if buffer:
                groups.append(_TypedGroup(text="\n".join(buffer)))
                buffer = []
        else:
            buffer.append(line)
    if buffer:
        groups.append(_TypedGroup(text="\n".join(buffer)))
    return groups


def _detect_footnotes_at_end(groups: list[_TypedGroup]) -> list[_TypedGroup]:
    """Mark page-end groups that qualify as footnotes (spec §4.1.3).

    The detection is deliberately conservative: only the trailing run of
    numbered lines preceded by a `---` (or `*`) horizontal-rule group is
    classified as footnote. Earlier numbered-like lines inside narrative
    stay inside the prior block.
    """
    if not groups:
        return groups

    # Walk backwards collecting contiguous footnote-looking groups, and stop
    # as soon as we find a horizontal-rule group (which gets dropped since
    # footnotes absorb the visual separator).
    out = list(groups)
    footnote_indices: list[int] = []
    separator_index: int | None = None
    for idx in range(len(out) - 1, -1, -1):
        g = out[idx]
        text = g.text.strip()
        if _HRULE_RE.match(text):
            separator_index = idx
            break
        # A multi-line group that STARTS with a digit-space is the footnote body.
        first_line = text.splitlines()[0] if text else ""
        if _FOOTNOTE_LINE_RE.match(first_line):
            footnote_indices.append(idx)
        else:
            # Anything else before the separator aborts the footnote sequence.
            break

    if separator_index is None or not footnote_indices:
        return out

    for fi in footnote_indices:
        out[fi].forced_type = "footnote"
    # Drop the separator group from emission (it becomes part of no block).
    del out[separator_index]
    return out


def _match_case_header(group_text: str) -> re.Match[str] | None:
    """Match a case header anywhere in the first non-empty line of a group."""
    first_line = group_text.lstrip("\n").splitlines()[0] if group_text else ""
    return _CASE_HEADER_RE.match(first_line)


def _build_case_header(
    groups: list[_TypedGroup],
    start_index: int,
    header_match: re.Match[str],
    source_page: int,
    order_index: int,
) -> tuple[SegmentedBlock, int, dict[str, Any]]:
    """Build the `case_header` block.

    Consumes the header group and, if the next group is a court/citation line
    (within 3 non-blank lines of the header), folds it into the header's
    markdown AND carries its metadata forward to the opinion block.
    """
    header_group = groups[start_index]
    left = header_match.group("left").strip()
    right = header_match.group("right").strip()
    case_name = f"{left} v. {right}"

    header_lines = [header_group.text]
    consumed = 1

    metadata: dict[str, Any] = {"case_name": case_name}

    # Peek forward. The court/citation line is usually in its own group; the
    # spec says "within 3 non-blank lines" — at most one adjacent group fits.
    if start_index + 1 < len(groups):
        next_group = groups[start_index + 1]
        if next_group.forced_type is None:
            court_meta = _parse_court_or_citation(next_group.text)
            if court_meta is not None:
                header_lines.append(next_group.text)
                metadata.update(court_meta)
                consumed += 1

    block = SegmentedBlock(
        type=BlockType.CASE_HEADER,
        order_index=order_index,
        source_page=source_page,
        markdown=_strip_block("\n\n".join(header_lines)),
        block_metadata=dict(metadata),
    )
    return block, consumed, metadata


def _parse_court_or_citation(group_text: str) -> dict[str, Any] | None:
    """Return metadata for a court/citation line or None if it's prose."""
    # Consider the first up-to-3 non-blank lines of the group — spec says
    # "within 3 non-blank lines of the header." In practice the court/cite
    # line is usually alone, but Marker sometimes staples a citation right
    # after a court name with no blank line.
    lines = [ln for ln in group_text.splitlines() if ln.strip()][:3]
    if not lines:
        return None

    meta: dict[str, Any] = {}
    for line in lines:
        court_m = _COURT_LINE_RE.match(line)
        if court_m is not None:
            meta["court"] = court_m.group("court").strip()
            meta["year"] = int(court_m.group("year"))
            continue
        cite_m = _CITATION_US_RE.match(line) or _CITATION_REPORTER_RE.match(line)
        if cite_m is not None:
            meta["citation"] = cite_m.group("cite").strip()
            continue
        # If we already have metadata collected, additional lines are fine;
        # but if the very first line is ordinary prose we don't consume the
        # group at all.
        if not meta:
            return None
        break

    return meta or None


def _build_case_opinion(
    groups: list[_TypedGroup],
    start_index: int,
    header_meta: dict[str, Any],
    source_page: int,
    order_index: int,
) -> tuple[SegmentedBlock | None, int]:
    """Gather opinion body groups up to a terminator (spec §4.1.3)."""
    opinion_groups: list[str] = []
    consumed = 0
    i = start_index
    while i < len(groups):
        tg = groups[i]
        if tg.forced_type == "footnote":
            break
        text = tg.text
        # (a) next case_header
        if _match_case_header(text) is not None:
            break
        # (b) top-level `# ` header
        first_line = text.lstrip("\n").splitlines()[0] if text else ""
        if first_line.startswith("# "):
            break
        # (d) Notes and Questions line
        if _is_notes_questions(text):
            break
        # (c) numbered-note after a blank line — our group split means every
        # group that starts with `^\d+\. ` is preceded by a blank line by
        # construction.
        if _NUMBERED_NOTE_RE.match(text):
            break
        # Standalone Problem header ends the opinion too; downstream becomes
        # its own PROBLEM block.
        if _PROBLEM_HEADER_RE.match(first_line):
            break
        opinion_groups.append(text)
        consumed += 1
        i += 1

    if not opinion_groups:
        return None, 0

    opinion_md = _strip_block("\n\n".join(opinion_groups))
    opinion_meta = {
        k: header_meta[k] for k in ("case_name", "court", "year", "citation") if k in header_meta
    }
    block = SegmentedBlock(
        type=BlockType.CASE_OPINION,
        order_index=order_index,
        source_page=source_page,
        markdown=opinion_md,
        block_metadata=opinion_meta,
    )
    return block, consumed


def _is_notes_questions(text: str) -> bool:
    first_line = text.lstrip("\n").splitlines()[0] if text else ""
    return bool(_NOTES_QUESTIONS_RE.match(first_line))


def _is_blockquote_group(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all(_BLOCKQUOTE_RE.match(ln) for ln in lines)


def _is_table_group(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    # Need a pipe-bearing row AND an alignment row.
    has_pipe_row = any("|" in ln for ln in lines)
    has_align_row = any(_TABLE_ALIGN_RE.match(ln) for ln in lines)
    return has_pipe_row and has_align_row


def _table_dimensions(text: str) -> tuple[int, int]:
    """Count rows (excluding alignment row) and columns in a GFM table group."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    cols = 0
    data_rows = 0
    for ln in lines:
        if _TABLE_ALIGN_RE.match(ln):
            continue
        # Column count from the first data row.
        stripped = ln.strip().strip("|")
        parts = [p for p in stripped.split("|")]
        if cols == 0 and parts:
            cols = len(parts)
        data_rows += 1
    return data_rows, cols


def _header_match(group_text: str) -> re.Match[str] | None:
    first_line = group_text.lstrip("\n").splitlines()[0] if group_text else ""
    return _HEADER_RE.match(first_line)


def _make_header_block(
    group_text: str, source_page: int, order_index: int
) -> SegmentedBlock:
    """Build a non-case `header` block, used for Notes-and-Questions too."""
    first_line = group_text.lstrip("\n").splitlines()[0] if group_text else ""
    header_match = _HEADER_RE.match(first_line)
    if header_match is not None:
        level = len(header_match.group("hashes"))
        text = header_match.group("text").strip()
    else:
        # "Notes and Questions" may arrive as plain text or as bold — treat
        # it as a level-2 header by convention so the outline generator has
        # a structural anchor.
        level = 2
        text = _NOTES_QUESTIONS_RE.sub("Notes and Questions", first_line).strip(" *#")
    return SegmentedBlock(
        type=BlockType.HEADER,
        order_index=order_index,
        source_page=source_page,
        markdown=_strip_block(group_text),
        block_metadata={"level": level, "text": text},
    )


def _strip_block(markdown: str) -> str:
    """Strip trailing whitespace/blank lines but preserve internal structure.

    Leading whitespace on individual lines (indentation inside lists, etc.)
    is preserved; only the block as a whole is right-trimmed.
    """
    return markdown.rstrip()


__all__ = ["SegmentedBlock", "segment_page_markdown"]
