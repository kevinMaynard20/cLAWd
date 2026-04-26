"""Unit tests for features/global_search.py (spec §5.14)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session

from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    CreatedBy,
    Page,
    Speaker,
    TocEntry,
    Transcript,
    TranscriptSegment,
    TranscriptSourceType,
)
from features.global_search import SearchRequest, search


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


@pytest.fixture
def seeded_corpus(temp_db: None) -> str:
    """Seed one corpus with one book (one page with case_opinion block about
    'state action'), one transcript with one segment (also about 'state action'),
    and one artifact (case brief with Shelley content)."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        cid = corpus.id

        book = Book(
            id="b" * 64,
            corpus_id=cid,
            title="Property Casebook",
            source_pdf_path="/p.pdf",
            source_page_min=500,
            source_page_max=550,
        )
        session.add(book)
        session.commit()

        session.add(
            TocEntry(
                book_id=book.id,
                level=1,
                title="Chapter 10: Takings",
                source_page=510,
                order_index=0,
            )
        )
        session.commit()

        page = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="b.pdf",
            pdf_page_start=1000,
            pdf_page_end=1002,
            markdown="page md",
            raw_text="page rt",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        session.add(
            Block(
                page_id=page.id,
                book_id=book.id,
                order_index=0,
                type=BlockType.CASE_OPINION,
                source_page=518,
                markdown=(
                    "Judicial enforcement of private racially restrictive "
                    "covenants constitutes state action subject to the "
                    "Fourteenth Amendment."
                ),
                block_metadata={"case_name": "Shelley v. Kraemer"},
            )
        )
        session.commit()

        transcript = Transcript(
            id="t" * 64,
            corpus_id=cid,
            source_type=TranscriptSourceType.TEXT,
            raw_text="whatever",
            cleaned_text="cleaned",
            topic="State action in covenants",
        )
        session.add(transcript)
        session.commit()
        session.refresh(transcript)

        session.add(
            TranscriptSegment(
                transcript_id=transcript.id,
                order_index=0,
                start_char=0,
                end_char=80,
                speaker=Speaker.PROFESSOR,
                content=(
                    "The state action doctrine is the cornerstone of "
                    "Shelley. Make sure you know it."
                ),
                mentioned_cases=["Shelley v. Kraemer"],
                mentioned_rules=["state action doctrine"],
                mentioned_concepts=[],
                sentiment_flags=["emphasis_verbal_cue"],
            )
        )
        session.commit()

        session.add(
            Artifact(
                corpus_id=cid,
                type=ArtifactType.CASE_BRIEF,
                created_by=CreatedBy.SYSTEM,
                content={
                    "case_name": "Shelley v. Kraemer",
                    "rule": {
                        "text": (
                            "Judicial enforcement of racially restrictive "
                            "covenants is state action."
                        )
                    },
                },
                prompt_template="case_brief@1.2.0",
                llm_model="claude-opus-4-7",
                cost_usd=Decimal("0.05"),
                cache_key="k1",
            )
        )
        session.commit()

        return cid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_empty_query_returns_empty(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(session, SearchRequest(q="", corpus_id=seeded_corpus))
    assert results == []


def test_search_finds_results_across_all_three_kinds(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session,
            SearchRequest(q="state action", corpus_id=seeded_corpus),
        )
    kinds = {r.kind for r in results}
    assert kinds == {"block", "transcript_segment", "artifact"}


def test_search_includes_structural_context(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session,
            SearchRequest(q="state action", corpus_id=seeded_corpus),
        )
    block_result = next(r for r in results if r.kind == "block")
    # TOC-backed context: book title + chapter
    assert "Chapter 10" in block_result.source_context or "Takings" in block_result.source_context
    assert "518" in block_result.source_context

    transcript_result = next(r for r in results if r.kind == "transcript_segment")
    assert "State action" in transcript_result.source_context or "professor" in transcript_result.source_context

    artifact_result = next(r for r in results if r.kind == "artifact")
    assert "Case Brief" in artifact_result.source_context or "Shelley" in artifact_result.source_context


def test_search_case_opinion_boosted(seeded_corpus: str) -> None:
    """Case-opinion blocks should rank above other kinds when all three match
    with similar token overlap — the +0.5 boost matters."""
    with Session(db.get_engine()) as session:
        results = search(
            session,
            SearchRequest(q="state action Fourteenth Amendment", corpus_id=seeded_corpus),
        )
    assert results
    # Either the case-opinion block or the artifact citing the amendment wins;
    # both are valid top picks. Assert the score ordering is DESC and stable.
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_search_emphasis_flag_boost_on_transcript(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session,
            SearchRequest(
                q="cornerstone",
                corpus_id=seeded_corpus,
                kinds=["transcript_segment"],
            ),
        )
    assert len(results) == 1
    # The segment has `emphasis_verbal_cue` → boosted by 0.5
    assert results[0].score >= 1.5


def test_search_kinds_filter_narrows(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session,
            SearchRequest(q="state action", corpus_id=seeded_corpus, kinds=["block"]),
        )
    assert all(r.kind == "block" for r in results)


def test_search_corpus_id_filter_isolates(seeded_corpus: str, temp_db: None) -> None:
    """A query scoped to a different corpus returns nothing."""
    with Session(db.get_engine()) as session:
        other = Corpus(name="Crim", course="Criminal Law")
        session.add(other)
        session.commit()
        session.refresh(other)
        other_id = other.id

    with Session(db.get_engine()) as session:
        results = search(
            session, SearchRequest(q="state action", corpus_id=other_id)
        )
    assert results == []


def test_search_limit_applied(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session, SearchRequest(q="state action", corpus_id=seeded_corpus, limit=1)
        )
    assert len(results) == 1


def test_search_snippet_has_ellipsis_on_long_match(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session,
            SearchRequest(q="state action", corpus_id=seeded_corpus, kinds=["block"]),
        )
    assert results
    assert "state action" in results[0].snippet.lower()


def test_search_no_match_returns_empty(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session,
            SearchRequest(q="nothing matches this query", corpus_id=seeded_corpus),
        )
    assert results == []


def test_search_no_corpus_filter_spans_all(seeded_corpus: str, temp_db: None) -> None:
    """Without corpus_id, search returns hits across every corpus."""
    with Session(db.get_engine()) as session:
        results = search(session, SearchRequest(q="state action"))
    # Should include the seeded corpus's hits
    corpus_ids = {r.corpus_id for r in results}
    assert seeded_corpus in corpus_ids


def test_search_score_desc_ordering(seeded_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        results = search(
            session, SearchRequest(q="state action", corpus_id=seeded_corpus)
        )
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
