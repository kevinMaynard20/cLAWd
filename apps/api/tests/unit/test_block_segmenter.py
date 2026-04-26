"""Unit tests for primitives/block_segmenter.py (spec §4.1.3).

These tests exercise the rule-based path only. Ambiguous cases are the LLM
fallback's job (Phase 2); we don't test that here. Segmentation is a pure
function over `(markdown, page_number)` so no database fixture is required.
"""

from __future__ import annotations

from data.models import BlockType
from primitives.block_segmenter import SegmentedBlock, segment_page_markdown

# ---------------------------------------------------------------------------
# Empty-page + narrative fallback
# ---------------------------------------------------------------------------


def test_empty_page_yields_single_empty_narrative() -> None:
    """Spec §4.1.3 rule 1: a page always has at least one block."""
    result = segment_page_markdown("", 500)
    assert len(result) == 1
    block = result[0]
    assert isinstance(block, SegmentedBlock)
    assert block.type is BlockType.NARRATIVE_TEXT
    assert block.markdown == ""
    assert block.order_index == 0
    assert block.source_page == 500


def test_narrative_only_page() -> None:
    md = (
        "This is the first paragraph of running narrative.\n"
        "It has two lines.\n"
        "\n"
        "This is the second paragraph.\n"
    )
    result = segment_page_markdown(md, 1)
    assert len(result) == 2
    assert all(b.type is BlockType.NARRATIVE_TEXT for b in result)
    assert result[0].markdown.startswith("This is the first paragraph")
    assert result[1].markdown == "This is the second paragraph."


# ---------------------------------------------------------------------------
# Case header + opinion
# ---------------------------------------------------------------------------


def test_case_header_and_opinion_segmentation() -> None:
    md = (
        "Smith v. Jones\n"
        "\n"
        "Supreme Court of Missouri, 1948\n"
        "\n"
        "The plaintiff brought this action in ejectment. The trial court\n"
        "held for the defendant. We reverse.\n"
        "\n"
    )
    result = segment_page_markdown(md, 101)
    assert len(result) == 2

    header, opinion = result
    assert header.type is BlockType.CASE_HEADER
    assert header.block_metadata["case_name"] == "Smith v. Jones"
    assert header.block_metadata["court"] == "Supreme Court of Missouri"
    assert header.block_metadata["year"] == 1948

    assert opinion.type is BlockType.CASE_OPINION
    assert opinion.block_metadata["case_name"] == "Smith v. Jones"
    assert opinion.block_metadata["court"] == "Supreme Court of Missouri"
    assert opinion.block_metadata["year"] == 1948
    assert "plaintiff brought this action" in opinion.markdown
    assert "Smith v. Jones" not in opinion.markdown
    assert "Supreme Court of Missouri" not in opinion.markdown


def test_case_header_shelley_with_citation() -> None:
    """Header followed by a U.S. Reports-style citation, then the opinion."""
    md = (
        "Shelley v. Kraemer\n"
        "\n"
        "334 U.S. 1\n"
        "\n"
        "This case presents for our consideration questions relating to the\n"
        "validity of court enforcement of private agreements.\n"
    )
    result = segment_page_markdown(md, 518)
    assert len(result) == 2
    header, opinion = result
    assert header.type is BlockType.CASE_HEADER
    assert header.block_metadata["case_name"] == "Shelley v. Kraemer"
    assert header.block_metadata["citation"] == "334 U.S. 1"

    assert opinion.type is BlockType.CASE_OPINION
    assert opinion.block_metadata["citation"] == "334 U.S. 1"
    assert "validity of court enforcement" in opinion.markdown


def test_case_header_with_apostrophe() -> None:
    """Both straight and curly apostrophes are acceptable in party names."""
    straight = "Smith's Farm v. Jones\n\nOpinion text here.\n"
    curly = "Smith’s Farm v. Jones\n\nOpinion text here.\n"

    for md in (straight, curly):
        result = segment_page_markdown(md, 12)
        assert len(result) == 2, f"Failed for: {md!r}"
        assert result[0].type is BlockType.CASE_HEADER
        # Metadata preserves the original apostrophe flavor.
        name = result[0].block_metadata["case_name"]
        assert name.endswith(" v. Jones")
        assert name.startswith("Smith")


def test_opinion_terminates_at_notes_and_questions_header() -> None:
    md = (
        "Smith v. Jones\n"
        "\n"
        "Supreme Court of Missouri, 1948\n"
        "\n"
        "The plaintiff brought this action in ejectment.\n"
        "\n"
        "Notes and Questions\n"
        "\n"
        "Some question prose.\n"
    )
    result = segment_page_markdown(md, 50)
    # header, opinion, notes-header, narrative
    types = [b.type for b in result]
    assert types[:2] == [BlockType.CASE_HEADER, BlockType.CASE_OPINION]
    assert BlockType.HEADER in types
    opinion = result[1]
    assert "Notes and Questions" not in opinion.markdown
    assert "plaintiff brought" in opinion.markdown


def test_opinion_terminates_at_numbered_note() -> None:
    md = (
        "Smith v. Jones\n"
        "\n"
        "Supreme Court of Missouri, 1948\n"
        "\n"
        "The plaintiff brought this action.\n"
        "\n"
        "1. The first note. Follow-up question here.\n"
    )
    result = segment_page_markdown(md, 7)
    assert len(result) == 3
    header, opinion, note = result
    assert header.type is BlockType.CASE_HEADER
    assert opinion.type is BlockType.CASE_OPINION
    assert "The first note" not in opinion.markdown

    assert note.type is BlockType.NUMBERED_NOTE
    assert note.block_metadata["number"] == 1
    assert note.block_metadata["has_problem"] is False


# ---------------------------------------------------------------------------
# Numbered note + problem flag + standalone problem
# ---------------------------------------------------------------------------


def test_numbered_note_problem_flag() -> None:
    # No blank line between the numbered item and the "Problem:" continuation —
    # they belong to the same numbered_note block. `has_problem` should flip.
    md = (
        "1. Consider the following situation. Then answer.\n"
        "Problem: Suppose a new owner receives the land.\n"
    )
    result = segment_page_markdown(md, 200)
    note_blocks = [b for b in result if b.type is BlockType.NUMBERED_NOTE]
    assert note_blocks, f"Expected a numbered_note, got types: {[b.type for b in result]}"
    assert note_blocks[0].block_metadata["number"] == 1
    assert note_blocks[0].block_metadata["has_problem"] is True


def test_numbered_note_problem_flag_inline() -> None:
    """Simpler case: a single numbered_note whose body contains 'Problem:'."""
    md = "1. Consider this rule. Problem: what if the buyer was on notice?\n"
    result = segment_page_markdown(md, 201)
    assert len(result) == 1
    note = result[0]
    assert note.type is BlockType.NUMBERED_NOTE
    assert note.block_metadata["number"] == 1
    assert note.block_metadata["has_problem"] is True


def test_standalone_problem_block() -> None:
    md = (
        "Some narrative text introducing the problem.\n"
        "\n"
        "Problem 1:\n"
        "\n"
        "Assume the following facts. Analyze whether the rule applies.\n"
    )
    result = segment_page_markdown(md, 34)
    types = [b.type for b in result]
    assert BlockType.PROBLEM in types
    problem_block = [b for b in result if b.type is BlockType.PROBLEM][0]
    assert "Problem 1" in problem_block.markdown


# ---------------------------------------------------------------------------
# Blockquote, header, figure, table
# ---------------------------------------------------------------------------


def test_blockquote_detection() -> None:
    md = "> This is a block quote.\n> It spans multiple lines.\n"
    result = segment_page_markdown(md, 9)
    assert len(result) == 1
    assert result[0].type is BlockType.BLOCK_QUOTE
    assert "block quote" in result[0].markdown


def test_header_detection_non_case() -> None:
    md = "## Section Header\n"
    result = segment_page_markdown(md, 3)
    assert len(result) == 1
    b = result[0]
    assert b.type is BlockType.HEADER
    assert b.block_metadata["level"] == 2
    assert b.block_metadata["text"] == "Section Header"


def test_figure_detection() -> None:
    md = "![a diagram of the property](https://example.com/fig.png)\n"
    result = segment_page_markdown(md, 11)
    assert len(result) == 1
    b = result[0]
    assert b.type is BlockType.FIGURE
    assert b.block_metadata["alt"] == "a diagram of the property"
    assert b.block_metadata["src"] == "https://example.com/fig.png"


def test_table_detection() -> None:
    md = (
        "| Col A | Col B | Col C |\n"
        "|-------|-------|-------|\n"
        "| 1     | 2     | 3     |\n"
        "| 4     | 5     | 6     |\n"
    )
    result = segment_page_markdown(md, 42)
    assert len(result) == 1
    t = result[0]
    assert t.type is BlockType.TABLE
    assert t.block_metadata["cols"] == 3
    # Header row + 2 data rows = 3 rows (alignment row excluded).
    assert t.block_metadata["rows"] == 3


# ---------------------------------------------------------------------------
# Footnote detection
# ---------------------------------------------------------------------------


def test_footnote_after_horizontal_rule() -> None:
    md = (
        "The court held for the defendant.\n"
        "\n"
        "---\n"
        "\n"
        "1 Text of footnote.\n"
    )
    result = segment_page_markdown(md, 77)
    types = [b.type for b in result]
    assert BlockType.FOOTNOTE in types
    fn = [b for b in result if b.type is BlockType.FOOTNOTE][0]
    assert fn.block_metadata["footnote_number"] == 1
    assert fn.block_metadata["parent_block_id"] is None
    # And the preceding narrative block is intact and doesn't absorb the fn.
    narrative = [b for b in result if b.type is BlockType.NARRATIVE_TEXT]
    assert narrative
    assert "Text of footnote" not in narrative[0].markdown


def test_footnote_without_horizontal_rule_stays_with_prior_block() -> None:
    """Without the `---` separator, a numeric-led line is ambiguous; it stays
    inside whatever block it appears in (conservative rule)."""
    md = (
        "The court held for the defendant.\n"
        "\n"
        "1 Text that looks like a footnote but has no rule above.\n"
    )
    result = segment_page_markdown(md, 78)
    assert all(b.type is not BlockType.FOOTNOTE for b in result)
    # The '1 Text' line becomes a numbered_note if `.` follows the number;
    # here it's just "1 Text" (no dot) so it falls through to narrative_text.
    combined = " ".join(b.markdown for b in result)
    assert "1 Text that looks like a footnote" in combined


# ---------------------------------------------------------------------------
# Multi-case + structural invariants
# ---------------------------------------------------------------------------


def test_multi_case_page_has_proper_boundaries() -> None:
    md = (
        "Smith v. Jones\n"
        "\n"
        "Supreme Court of Missouri, 1948\n"
        "\n"
        "The first opinion text goes here.\n"
        "\n"
        "Alpha v. Beta\n"
        "\n"
        "Supreme Court of Nevada, 1972\n"
        "\n"
        "The second opinion text goes here.\n"
    )
    result = segment_page_markdown(md, 42)
    assert len(result) == 4
    assert [b.type for b in result] == [
        BlockType.CASE_HEADER,
        BlockType.CASE_OPINION,
        BlockType.CASE_HEADER,
        BlockType.CASE_OPINION,
    ]
    assert result[0].block_metadata["case_name"] == "Smith v. Jones"
    assert result[2].block_metadata["case_name"] == "Alpha v. Beta"
    # Opinions carry their own case metadata, not the other case's.
    assert result[1].block_metadata["case_name"] == "Smith v. Jones"
    assert result[3].block_metadata["case_name"] == "Alpha v. Beta"


def test_order_index_monotonic() -> None:
    md = (
        "## A Header\n"
        "\n"
        "Some narrative.\n"
        "\n"
        "> A quote.\n"
        "\n"
        "More narrative.\n"
    )
    result = segment_page_markdown(md, 15)
    assert [b.order_index for b in result] == list(range(len(result)))


def test_source_page_propagated() -> None:
    md = "## Section\n\nSome text.\n\n> A quote.\n"
    result = segment_page_markdown(md, 873)
    assert all(b.source_page == 873 for b in result)


def test_case_header_false_positive_rejected() -> None:
    """'Smith vs. Jones' (not 'v.') should not be parsed as a case header."""
    md = "Smith vs. Jones is a local pairing but not a case.\n"
    result = segment_page_markdown(md, 1)
    assert len(result) == 1
    assert result[0].type is BlockType.NARRATIVE_TEXT
    assert "Smith vs. Jones" in result[0].markdown
