"""Unit tests for primitives/toc_extractor.py (spec §3.4, §4.1.1 step 7)."""

from __future__ import annotations

from primitives.ingest import PageMarker
from primitives.toc_extractor import TocEntryDraft, extract_toc

# ---------------------------------------------------------------------------
# Heading-based fallback
# ---------------------------------------------------------------------------


def test_extract_toc_from_headings() -> None:
    """Simple markdown with a Part / Chapter / Section structure and a few
    page markers. The headings each snap to the nearest preceding marker and
    the parent pointers honor heading levels."""
    md = "\n".join(
        [
            "# Part I",  # line 0 — before any page marker → snaps to first marker
            "",
            "1",  # line 2 — page 1 marker
            "",
            "## Chapter 1",  # line 4 — snaps to page 1
            "",
            "Opening prose.",
            "",
            "2",  # line 8 — page 2 marker
            "",
            "### Section A",  # line 10 — snaps to page 2
            "",
            "Body.",
            "",
            "3",  # line 14 — page 3 marker
            "",
            "## Chapter 2",  # line 16 — snaps to page 3
        ]
    )
    markers = [PageMarker(line_index=2, source_page=1), PageMarker(line_index=8, source_page=2), PageMarker(line_index=14, source_page=3)]

    result = extract_toc(md, markers)
    # 4 entries: Part I, Chapter 1, Section A, Chapter 2.
    assert len(result) == 4

    part = result[0]
    assert part == TocEntryDraft(
        level=1, title="Part I", source_page=1, order_index=0, parent_offset=None
    )

    ch1 = result[1]
    assert ch1.level == 2
    assert ch1.title == "Chapter 1"
    assert ch1.source_page == 1
    assert ch1.parent_offset == 0  # Part I is its parent

    sec = result[2]
    assert sec.level == 3
    assert sec.title == "Section A"
    assert sec.source_page == 2
    assert sec.parent_offset == 1  # Chapter 1 is its parent

    ch2 = result[3]
    assert ch2.level == 2
    assert ch2.title == "Chapter 2"
    assert ch2.source_page == 3
    assert ch2.parent_offset == 0  # Part I is its parent; Section A was at lvl 3


def test_extract_toc_empty_markdown() -> None:
    assert extract_toc("", []) == []
    assert extract_toc("   \n\n  ", []) == []


# ---------------------------------------------------------------------------
# Explicit Contents-block strategy
# ---------------------------------------------------------------------------


def test_extract_toc_prefers_explicit_contents_block() -> None:
    """When both a Contents block AND inline headings exist, the Contents
    block is authoritative (spec §4.1.1 step 7: "Parse the front matter and
    any 'Contents' headers")."""
    md = "\n".join(
        [
            "# Property: Cases and Materials",
            "",
            "## Contents",
            "",
            "Chapter 1 . . . . . . . . . 5",
            "Chapter 2 . . . . . . . . . 42",
            "Chapter 3 . . . . . . . . . 100",
            "",
            "",
            "1",
            "",
            "# Chapter 1",  # inline heading on page 1, but contents said 5
            "",
            "Body of chapter 1.",
            "",
            "5",
            "",
            "## Some Subsection",
        ]
    )
    markers = [
        PageMarker(line_index=9, source_page=1),
        PageMarker(line_index=15, source_page=5),
    ]

    result = extract_toc(md, markers)
    titles = [e.title for e in result]
    pages = [e.source_page for e in result]
    assert "Chapter 1" in titles
    assert "Chapter 2" in titles
    assert "Chapter 3" in titles
    # The Contents block said Chapter 1 is on 5, not 1 (despite the inline
    # heading). The explicit Contents block wins per spec.
    ch1 = next(e for e in result if e.title == "Chapter 1")
    assert ch1.source_page == 5
    assert 42 in pages
    assert 100 in pages
    # None of the inline-heading subsections should bleed in (they'd show up
    # with different source pages if heading-fallback was used).
    assert not any(e.title == "Some Subsection" for e in result)
