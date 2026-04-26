"""Unit tests for primitives/ingest.py source-page-marker extraction.

Spec refs: §4.1.1 (ingestion pipeline, step 4 + algorithm description),
§2.3 (why source page numbers matter), §6.1 L1 `test_extract_source_page_markers`.

The full fixture-corpus test (`tests/fixtures/book/`, the 10-source-page slice
of the user's real Property casebook) lands in Phase 1.8. These tests pin the
algorithm against synthetic inputs that exercise every documented edge case.
"""

from __future__ import annotations

from primitives.ingest import (
    NumericLineCandidate,
    PageMarker,
    extract_page_markers_from_markdown,
    extract_source_page_markers,
    find_numeric_line_candidates,
)

# ---------------------------------------------------------------------------
# find_numeric_line_candidates
# ---------------------------------------------------------------------------


def test_find_candidates_empty_markdown() -> None:
    assert find_numeric_line_candidates("") == []


def test_find_candidates_only_bare_numerics() -> None:
    md = "1\n2\n3\n"
    result = find_numeric_line_candidates(md)
    assert [(c.line_index, c.value) for c in result] == [(0, 1), (1, 2), (2, 3)]


def test_find_candidates_rejects_numbers_embedded_in_prose() -> None:
    md = (
        "A chapter begins here.\n"
        "There were 3 defendants.\n"  # not bare — has surrounding prose
        "1\n"  # this is bare and kept
        "Another line.\n"
    )
    assert [(c.line_index, c.value) for c in find_numeric_line_candidates(md)] == [(2, 1)]


def test_find_candidates_accepts_surrounding_whitespace() -> None:
    md = "   518   \n  \n"
    assert [(c.line_index, c.value) for c in find_numeric_line_candidates(md)] == [(0, 518)]


def test_find_candidates_rejects_signed_and_decimal() -> None:
    md = "-5\n3.14\n+7\n"
    assert find_numeric_line_candidates(md) == []


def test_find_candidates_rejects_roman_numerals() -> None:
    """Front-matter handled separately (spec §3.3); extractor only sees digits."""
    md = "iv\nv\nvi\n"
    assert find_numeric_line_candidates(md) == []


# ---------------------------------------------------------------------------
# extract_source_page_markers — happy paths
# ---------------------------------------------------------------------------


def test_extract_simple_monotonic_sequence() -> None:
    candidates = [
        NumericLineCandidate(10, 1),
        NumericLineCandidate(20, 2),
        NumericLineCandidate(30, 3),
        NumericLineCandidate(40, 4),
    ]
    result = extract_source_page_markers(candidates)
    assert result == [
        PageMarker(10, 1),
        PageMarker(20, 2),
        PageMarker(30, 3),
        PageMarker(40, 4),
    ]


def test_extract_starts_at_value_two_when_one_missing() -> None:
    """Some books have no page "1" printed. Start value 2 is allowed."""
    candidates = [
        NumericLineCandidate(5, 2),
        NumericLineCandidate(15, 3),
        NumericLineCandidate(25, 4),
    ]
    result = extract_source_page_markers(candidates)
    assert [m.source_page for m in result] == [2, 3, 4]


def test_extract_tolerates_single_missing_marker() -> None:
    """Spec §4.1.1: consecutive diffs of 1 OR 2 are allowed (one missing marker).

    The Property casebook batch-1 fixture has exactly this case — a Part-I
    divider page with no printed number causes a 1→3 gap that the algorithm
    must accept.
    """
    candidates = [
        NumericLineCandidate(5, 1),
        # (page 2 missing — divider page)
        NumericLineCandidate(20, 3),
        NumericLineCandidate(30, 4),
    ]
    result = extract_source_page_markers(candidates)
    assert [m.source_page for m in result] == [1, 3, 4]


def test_extract_rejects_two_missing_markers_in_a_row() -> None:
    """Diff=3 is too big. The algorithm should NOT chain 1→4 together.

    And because 4 itself is past `max_start_value=3`, it can't start its own
    chain either — so the best valid chain is just [1] (length 1).
    """
    candidates = [
        NumericLineCandidate(5, 1),
        NumericLineCandidate(20, 4),
        NumericLineCandidate(30, 5),
        NumericLineCandidate(40, 6),
    ]
    result = extract_source_page_markers(candidates)
    assert [m.source_page for m in result] == [1]


def test_extract_prefers_longer_valid_chain_over_orphan_start() -> None:
    """When a valid early start has no extensions but a later relaxed start
    yields a longer chain (with `max_start_value` loosened), the latter wins.
    This exercises the tie-break between candidate starts.
    """
    # Relax start to 5 so both starts are valid; compare chains.
    candidates = [
        NumericLineCandidate(5, 1),    # lone start — no valid successors
        NumericLineCandidate(15, 20),  # noise
        NumericLineCandidate(25, 5),   # start of longer chain (under relaxed max_start)
        NumericLineCandidate(35, 6),
        NumericLineCandidate(45, 7),
    ]
    result = extract_source_page_markers(candidates, max_start_value=5)
    assert [m.source_page for m in result] == [5, 6, 7]


def test_extract_rejects_unreachable_tail_without_valid_start() -> None:
    """If candidates don't start at 1, 2, or 3, return an empty chain."""
    candidates = [
        NumericLineCandidate(10, 100),
        NumericLineCandidate(20, 101),
        NumericLineCandidate(30, 102),
    ]
    result = extract_source_page_markers(candidates)
    assert result == []


def test_extract_filters_footnote_noise() -> None:
    """The key scenario. Footnote numbers appear intermixed with page markers
    but are non-monotonic — the algorithm must skip them."""
    candidates = [
        NumericLineCandidate(5, 47),   # footnote
        NumericLineCandidate(10, 1),   # page 1
        NumericLineCandidate(15, 23),  # footnote
        NumericLineCandidate(20, 2),   # page 2
        NumericLineCandidate(25, 12),  # footnote
        NumericLineCandidate(30, 3),   # page 3
        NumericLineCandidate(35, 8),   # footnote
        NumericLineCandidate(40, 4),   # page 4
    ]
    result = extract_source_page_markers(candidates)
    assert [m.source_page for m in result] == [1, 2, 3, 4]
    assert [m.line_index for m in result] == [10, 20, 30, 40]


def test_extract_duplicates_rejected_as_non_strictly_increasing() -> None:
    candidates = [
        NumericLineCandidate(10, 1),
        NumericLineCandidate(20, 2),
        NumericLineCandidate(25, 2),  # accidental duplicate — reject
        NumericLineCandidate(30, 3),
    ]
    result = extract_source_page_markers(candidates)
    assert [m.source_page for m in result] == [1, 2, 3]
    # And specifically the second "2" at line 25 must not appear.
    assert PageMarker(25, 2) not in result


def test_extract_empty_input_returns_empty() -> None:
    assert extract_source_page_markers([]) == []


def test_extract_handles_large_page_numbers() -> None:
    """Real casebooks go to ~1500 pages. Make sure there's no start-value
    mismatch when chains begin deep in the book (the user ingests a book from
    page 1, though, so this is really about robustness)."""
    candidates = [
        NumericLineCandidate(10, 1),
        NumericLineCandidate(20, 2),
        NumericLineCandidate(30, 3),
        NumericLineCandidate(40, 4),
        NumericLineCandidate(50, 1500),  # noise much further ahead
    ]
    result = extract_source_page_markers(candidates)
    # The long valid chain 1,2,3,4 wins; the stranded 1500 is ignored.
    assert [m.source_page for m in result] == [1, 2, 3, 4]


def test_extract_custom_max_gap() -> None:
    """When callers know their book has bigger gaps, they can relax.

    At default `max_gap=2`, 1→4 is diff=3 (rejected) AND 4 itself can't start
    (value>max_start_value=3), so only the lone `1` survives.
    Loosening `max_gap=3` lets 1→4→5 chain.
    """
    candidates = [
        NumericLineCandidate(10, 1),
        NumericLineCandidate(20, 4),  # diff 3 from 1
        NumericLineCandidate(30, 5),
    ]
    # Default max_gap=2: only the lone start chains.
    assert [m.source_page for m in extract_source_page_markers(candidates)] == [1]
    # Loosened to 3: entire chain accepted.
    custom = extract_source_page_markers(candidates, max_gap=3)
    assert [m.source_page for m in custom] == [1, 4, 5]


def test_extract_custom_max_start() -> None:
    candidates = [NumericLineCandidate(10, 5), NumericLineCandidate(20, 6)]
    # Default max_start_value=3 rejects 5 as a start.
    assert extract_source_page_markers(candidates) == []
    # Relaxed to 5 — chain accepted.
    result = extract_source_page_markers(candidates, max_start_value=5)
    assert [m.source_page for m in result] == [5, 6]


# ---------------------------------------------------------------------------
# extract_page_markers_from_markdown — integration of the two above
# ---------------------------------------------------------------------------


def test_full_markdown_extraction_with_mixed_content() -> None:
    """A realistic-ish markdown blob: prose, footnotes, page markers."""
    markdown = "\n".join(
        [
            "# Chapter 1",
            "",
            "Opening text of chapter. See note.¹",
            "",
            "1",  # line 4 — page 1 marker
            "",
            "More narrative here. Smith sued Jones.",
            "",
            "Smith reasoned that 3 factors applied.",  # prose, not bare
            "",
            "2",  # line 10 — page 2 marker
            "",
            "---",
            "",
            "**Notes and Questions**",
            "",
            "1.  What is the rule?",  # numbered note, not bare (has period + text)
            "",
            "47",  # line 18 — bare footnote number, filtered
            "",
            "3",  # line 20 — page 3 marker
        ]
    )
    result = extract_page_markers_from_markdown(markdown)
    assert [m.source_page for m in result] == [1, 2, 3]
    assert [m.line_index for m in result] == [4, 10, 20]


def test_full_markdown_no_markers_returns_empty() -> None:
    markdown = "# Just a header\n\nSome prose here.\n"
    assert extract_page_markers_from_markdown(markdown) == []
