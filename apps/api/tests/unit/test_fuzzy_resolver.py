"""Unit tests for ``primitives.fuzzy_resolver`` (spec §4.3.4).

Anchors on the three real user-reported Gemini deformations:

- "Shelly B Kramer"          → "Shelley v. Kraemer"
- "Pen Central"              → "Penn Central Transportation Co. v. New York City"
- "River Heights v Daton"    → "River Heights Associates L.P. v. Batten"

These three cases are the threshold calibration data — see the resolver's
``_composite_score`` / ``fuzzy_threshold=82.0`` comments for details.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from data import db
from data.models import Block, BlockType, Book, Corpus, Page
from primitives.fuzzy_resolver import (
    load_known_case_names_for_corpus,
    resolve_case_names,
)

# ---------------------------------------------------------------------------
# Canonical-name fixtures (shared across tests)
# ---------------------------------------------------------------------------


SHELLEY = "Shelley v. Kraemer"
PENN_CENTRAL = "Penn Central Transportation Co. v. New York City"
RIVER_HEIGHTS = "River Heights Associates L.P. v. Batten"

ALL_KNOWN = [SHELLEY, PENN_CENTRAL, RIVER_HEIGHTS]


# ---------------------------------------------------------------------------
# The three real-user deformations
# ---------------------------------------------------------------------------


def test_resolve_shelley_gemini_mishearing() -> None:
    """Gemini transcribes "v." as a capital "B" and drops a letter from both
    parties. The resolver must catch this via the normalized-separator +
    rapidfuzz path."""
    text = "the court in Shelly B Kramer held that state action applies."

    result = resolve_case_names(text, ALL_KNOWN)

    assert len(result.resolved) == 1
    candidate = result.resolved[0]
    assert candidate.matched_canonical == SHELLEY
    assert candidate.raw == "Shelly B Kramer"
    assert candidate.method == "fuzzy"
    assert candidate.score >= 82.0
    assert result.unresolved == []


def test_resolve_pen_central_truncation() -> None:
    """Gemini elides the Corporate suffix entirely — "Pen Central's
    three-factor test" has no " v. " at all, so the resolver has to match via
    the capitalized-phrase extractor, not the v-shape extractor."""
    text = "Pen Central's three-factor test applies here."

    result = resolve_case_names(text, ALL_KNOWN)

    assert any(c.matched_canonical == PENN_CENTRAL for c in result.resolved)
    match = next(c for c in result.resolved if c.matched_canonical == PENN_CENTRAL)
    assert match.raw == "Pen Central"
    assert match.score >= 82.0


def test_resolve_river_heights_wrong_party() -> None:
    """Gemini loses the suffix + mangles the second party. The composite
    score is what lets this resolve correctly — WRatio alone ties the
    candidate against Penn Central's surface-level similarity."""
    text = "The issue in River Heights v Daton came up next."

    result = resolve_case_names(text, ALL_KNOWN)

    assert len(result.resolved) >= 1
    match = next(
        (c for c in result.resolved if c.matched_canonical == RIVER_HEIGHTS), None
    )
    assert match is not None, f"expected {RIVER_HEIGHTS!r} in resolved"
    assert match.raw == "River Heights v Daton"
    assert match.method == "fuzzy"
    assert match.score >= 82.0


# ---------------------------------------------------------------------------
# Method-tag behavior
# ---------------------------------------------------------------------------


def test_exact_match_method_tag() -> None:
    """When the raw spelling equals the canonical byte-for-byte, the method
    tag is ``exact``. This lets the UI show a "high-confidence" badge and
    skip the manual-review prompt."""
    text = "As seen in Shelley v. Kraemer, state action applies."

    result = resolve_case_names(text, ALL_KNOWN)

    assert len(result.resolved) == 1
    assert result.resolved[0].method == "exact"
    assert result.resolved[0].matched_canonical == SHELLEY
    assert result.resolved[0].score >= 99.0  # near-perfect score


def test_normalized_match_handles_vs_and_vee() -> None:
    """Both "Shelley vs. Kraemer" and "Shelley vee Kraemer" normalize to the
    canonical "Shelley v. Kraemer", so they should resolve with the
    ``normalized`` method tag (not ``fuzzy``)."""
    for variant in ("Shelley vs. Kraemer", "Shelley vee Kraemer"):
        text = f"In {variant}, the court held..."
        result = resolve_case_names(text, ALL_KNOWN)

        assert len(result.resolved) == 1, f"variant {variant!r} failed"
        c = result.resolved[0]
        assert c.matched_canonical == SHELLEY
        assert c.method == "normalized", f"{variant!r} was {c.method!r}, expected 'normalized'"


# ---------------------------------------------------------------------------
# Unresolved / edge cases
# ---------------------------------------------------------------------------


def test_unresolved_when_below_threshold() -> None:
    """A candidate that doesn't look like any known case should land in
    ``unresolved`` rather than being forced to resolve to the highest-
    scoring (but still wrong) canonical."""
    text = "the court in Some Bogus v. Made Up Case did something."

    result = resolve_case_names(text, ALL_KNOWN)

    assert result.resolved == []
    assert "Some Bogus v. Made Up Case" in result.unresolved


def test_empty_corpus_returns_empty_unresolved_only() -> None:
    """With no known case names, every candidate is unresolved.

    This is the path taken when transcript ingest runs against a brand-new
    corpus that hasn't had any books ingested yet — the corpus has no
    canonical names, so every candidate is surfaced for later manual review
    once the books land.
    """
    text = "the court in Shelly B Kramer held ... also Pen Central's test applies."

    result = resolve_case_names(text, [])

    assert result.resolved == []
    # Both distinct raw mentions should appear in unresolved.
    assert "Shelly B Kramer" in result.unresolved
    assert "Pen Central" in result.unresolved


def test_duplicates_deduped() -> None:
    """Two mentions of the same case name should collapse to one
    ``CaseNameCandidate`` in the ``resolved`` list — downstream storage
    fields like ``Transcript.mentioned_cases`` should not contain duplicates.
    """
    text = "Shelly B Kramer is discussed. Later we come back to Shelly B Kramer."

    result = resolve_case_names(text, ALL_KNOWN)

    # Only one entry for Shelley even though "Shelly B Kramer" appears twice.
    matches = [c for c in result.resolved if c.matched_canonical == SHELLEY]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Corpus-name loader
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated SQLite per test, same pattern as test_models / test_ingest."""
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield tmp_path
    db.reset_engine()


def _seed_corpus_with_blocks(
    case_names_present: list[str | None],
) -> str:
    """Seed one corpus + one book + one page + N blocks.

    ``case_names_present`` is a list whose i-th element is either a case-name
    string (block gets that name in block_metadata), or ``None`` (block is a
    non-case block with no case_name). Returns the corpus_id.
    """
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        book = Book(
            id="d" * 64,
            corpus_id=corpus.id,
            title="Property Casebook",
            source_pdf_path="/tmp/book.pdf",
            source_page_min=1,
            source_page_max=100,
        )
        session.add(book)
        session.commit()

        page = Page(
            book_id=book.id,
            source_page=1,
            batch_pdf="b.pdf",
            pdf_page_start=0,
            pdf_page_end=2,
            markdown="page 1",
            raw_text="page 1",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        for i, name in enumerate(case_names_present):
            block_type = BlockType.CASE_OPINION if name else BlockType.NARRATIVE_TEXT
            metadata: dict = {}
            if name is not None:
                metadata["case_name"] = name
            block = Block(
                page_id=page.id,
                book_id=book.id,
                order_index=i,
                type=block_type,
                source_page=1,
                markdown=f"Block {i}",
                block_metadata=metadata,
            )
            session.add(block)
        session.commit()

        return corpus.id


def test_load_known_case_names_from_corpus(temp_db: Path) -> None:
    """Loader pulls case_name metadata from CASE_OPINION / CASE_HEADER blocks
    only, and ignores blocks without a case_name. Seeded with 3 blocks (one
    with a case_name, two without) → the returned list contains exactly the
    one case_name.
    """
    corpus_id = _seed_corpus_with_blocks([SHELLEY, None, None])

    engine = db.get_engine()
    with Session(engine) as session:
        names = load_known_case_names_for_corpus(session, corpus_id)

    assert names == [SHELLEY]


def test_load_known_case_names_dedupes_and_sorts(temp_db: Path) -> None:
    """Two blocks referencing the same case collapse to one name; the output
    is sorted for stable downstream testing."""
    corpus_id = _seed_corpus_with_blocks([
        PENN_CENTRAL, SHELLEY, PENN_CENTRAL, RIVER_HEIGHTS,
    ])

    engine = db.get_engine()
    with Session(engine) as session:
        names = load_known_case_names_for_corpus(session, corpus_id)

    assert names == sorted({SHELLEY, PENN_CENTRAL, RIVER_HEIGHTS})
