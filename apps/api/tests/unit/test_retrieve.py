"""Unit tests for primitives/retrieve.py (spec §4.2).

Covers PageRangeQuery and CaseReferenceQuery happy paths + edge cases, plus
the stub behavior for AssignmentCodeQuery and SemanticQuery.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from data import db
from data.models import Block, BlockType, Book, Corpus, Page
from primitives.retrieve import (
    AssignmentCodeQuery,
    CaseReferenceQuery,
    PageRangeQuery,
    SemanticQuery,
    retrieve,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


@pytest.fixture
def seeded_book(temp_db: None) -> str:
    """Create a book with 10 source pages, each with varied blocks.

    Layout:
      page 518: [case_header Shelley, case_opinion Shelley]
      page 519: [narrative] (continuation)
      page 520: [numbered_note 1 (Shelley), numbered_note 2 (Shelley)]
      page 521: [numbered_note 3 (Shelley)]
      page 522: [case_header River Heights, case_opinion River Heights]
      page 523: [numbered_note 1 (River Heights), numbered_note 2 (River Heights)]
      page 524: [case_header, case_opinion Bensch]
      page 525: [header "Notes"]
      page 526: [narrative]
      page 527: [numbered_note 1 (Bensch)]
    """
    engine = db.get_engine()
    book_hash = "b" * 64
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        book = Book(
            id=book_hash,
            corpus_id=corpus.id,
            title="Property",
            source_pdf_path="/p.pdf",
            source_page_min=518,
            source_page_max=527,
        )
        session.add(book)
        session.commit()

        def page(source_page: int) -> Page:
            p = Page(
                book_id=book.id,
                source_page=source_page,
                batch_pdf="b.pdf",
                pdf_page_start=source_page * 2,
                pdf_page_end=source_page * 2 + 1,
                markdown=f"# page {source_page}",
                raw_text=f"page {source_page}",
            )
            session.add(p)
            session.commit()
            session.refresh(p)
            return p

        def block(
            page_id: str,
            source_page: int,
            order: int,
            type_: BlockType,
            markdown: str,
            meta: dict | None = None,
        ) -> Block:
            b = Block(
                page_id=page_id,
                book_id=book.id,
                order_index=order,
                type=type_,
                source_page=source_page,
                markdown=markdown,
                block_metadata=meta or {},
            )
            session.add(b)
            session.commit()
            return b

        p518 = page(518)
        block(p518.id, 518, 0, BlockType.CASE_HEADER, "**Shelley v. Kraemer**", {"case_name": "Shelley v. Kraemer"})
        block(p518.id, 518, 1, BlockType.CASE_OPINION, "opinion text…", {
            "case_name": "Shelley v. Kraemer",
            "court": "SCOTUS",
            "year": 1948,
            "citation": "334 U.S. 1",
        })

        p519 = page(519)
        block(p519.id, 519, 0, BlockType.NARRATIVE_TEXT, "continuation")

        p520 = page(520)
        block(p520.id, 520, 0, BlockType.NUMBERED_NOTE, "1. Shelley note one", {"number": 1})
        block(p520.id, 520, 1, BlockType.NUMBERED_NOTE, "2. Shelley note two", {"number": 2})

        p521 = page(521)
        block(p521.id, 521, 0, BlockType.NUMBERED_NOTE, "3. Shelley note three", {"number": 3})

        p522 = page(522)
        block(p522.id, 522, 0, BlockType.CASE_HEADER, "**River Heights Associates L.P. v. Batten**", {
            "case_name": "River Heights Associates L.P. v. Batten",
        })
        block(p522.id, 522, 1, BlockType.CASE_OPINION, "river heights opinion", {
            "case_name": "River Heights Associates L.P. v. Batten",
        })

        p523 = page(523)
        block(p523.id, 523, 0, BlockType.NUMBERED_NOTE, "1. RH note one", {"number": 1})
        block(p523.id, 523, 1, BlockType.NUMBERED_NOTE, "2. RH note two", {"number": 2})

        p524 = page(524)
        block(p524.id, 524, 0, BlockType.CASE_HEADER, "**Bensch v. Metropolitan**", {"case_name": "Bensch v. Metropolitan"})
        block(p524.id, 524, 1, BlockType.CASE_OPINION, "bensch opinion", {"case_name": "Bensch v. Metropolitan"})

        p525 = page(525)
        block(p525.id, 525, 0, BlockType.HEADER, "## Notes")

        p526 = page(526)
        block(p526.id, 526, 0, BlockType.NARRATIVE_TEXT, "more narrative")

        p527 = page(527)
        block(p527.id, 527, 0, BlockType.NUMBERED_NOTE, "1. Bensch note one", {"number": 1})

        session.commit()

    return book_hash


# ---------------------------------------------------------------------------
# PageRangeQuery
# ---------------------------------------------------------------------------


def test_retrieve_page_range_inclusive(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(session, PageRangeQuery(book_id=seeded_book, start=520, end=521))
    assert [p.source_page for p in result.pages] == [520, 521]
    assert all(520 <= b.source_page <= 521 for b in result.blocks)
    assert len(result.blocks) == 3  # 2 on 520 + 1 on 521
    assert result.notes == []


def test_retrieve_page_range_single_page(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(session, PageRangeQuery(book_id=seeded_book, start=518, end=518))
    assert [p.source_page for p in result.pages] == [518]
    assert len(result.blocks) == 2  # case_header + case_opinion
    assert result.blocks[0].type is BlockType.CASE_HEADER
    assert result.blocks[1].type is BlockType.CASE_OPINION


def test_retrieve_page_range_blocks_in_reading_order(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(session, PageRangeQuery(book_id=seeded_book, start=518, end=522))
    order = [(b.source_page, b.order_index) for b in result.blocks]
    assert order == sorted(order)  # monotonic by (page, order_index)


def test_retrieve_page_range_out_of_book_returns_empty_with_note(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(session, PageRangeQuery(book_id=seeded_book, start=9000, end=9010))
    assert result.empty
    assert result.pages == []
    assert result.blocks == []
    assert len(result.notes) == 1
    assert "9000" in result.notes[0]


def test_retrieve_page_range_partial_clamps_and_warns(seeded_book: str) -> None:
    """Ask for 515–520 in a book starting at 518 — returns 518–520 plus a note
    that the requested start was clamped."""
    with Session(db.get_engine()) as session:
        result = retrieve(session, PageRangeQuery(book_id=seeded_book, start=515, end=520))
    assert [p.source_page for p in result.pages] == [518, 519, 520]
    assert any("515" in n and "518" in n for n in result.notes)


def test_retrieve_page_range_invalid_raises() -> None:
    with pytest.raises(ValueError, match="start .* > end"):
        PageRangeQuery(book_id="x", start=10, end=5)


def test_retrieve_page_range_wrong_book_empty(temp_db: None) -> None:
    """An unseeded book id returns empty; doesn't crash."""
    with Session(db.get_engine()) as session:
        result = retrieve(session, PageRangeQuery(book_id="z" * 64, start=1, end=10))
    assert result.empty


# ---------------------------------------------------------------------------
# CaseReferenceQuery
# ---------------------------------------------------------------------------


def test_retrieve_case_reference_returns_opinion_and_trailing_notes(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name="Shelley v. Kraemer", book_id=seeded_book),
        )
    # First block is the matching opinion
    assert result.blocks[0].type is BlockType.CASE_OPINION
    assert result.blocks[0].block_metadata["case_name"] == "Shelley v. Kraemer"
    # Then notes from pages 519..521 (bounded by the next case on p522)
    trailing_types = [b.type for b in result.blocks[1:]]
    assert BlockType.NUMBERED_NOTE in trailing_types
    # Must NOT cross into River Heights material
    assert all(b.source_page < 522 for b in result.blocks)


def test_retrieve_case_reference_case_insensitive(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name="shelley V kraemer", book_id=seeded_book),
        )
    assert not result.empty
    assert result.blocks[0].block_metadata["case_name"] == "Shelley v. Kraemer"


def test_retrieve_case_reference_normalizes_vs_vs_v(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name="Shelley vs. Kraemer", book_id=seeded_book),
        )
    assert not result.empty
    assert result.blocks[0].block_metadata["case_name"] == "Shelley v. Kraemer"


def test_retrieve_case_reference_no_match_returns_empty_with_note(seeded_book: str) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name="Marbury v. Madison", book_id=seeded_book),
        )
    assert result.empty
    assert len(result.notes) == 1
    assert "Marbury" in result.notes[0]


def test_retrieve_case_reference_last_case_trails_to_end_of_book(seeded_book: str) -> None:
    """Bensch is the last case — its trailing range should extend to page 527."""
    with Session(db.get_engine()) as session:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name="Bensch v. Metropolitan", book_id=seeded_book),
        )
    # Opinion is on 524; trailing pages include 525, 526, 527.
    max_page = max(b.source_page for b in result.blocks)
    assert max_page == 527


def test_retrieve_case_reference_cross_book_without_book_filter(seeded_book: str) -> None:
    """With book_id=None, the search is unbounded (a single-book fixture proves
    it still finds its own book's case)."""
    with Session(db.get_engine()) as session:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name="Shelley v. Kraemer"),
        )
    assert not result.empty


def test_retrieve_case_reference_trailing_contains_foreign_type_flagged(seeded_book: str) -> None:
    """Bensch's trailing range includes a `header` block — the retriever should
    surface this as a note so the UI knows trailing material isn't pure notes."""
    with Session(db.get_engine()) as session:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name="Bensch v. Metropolitan", book_id=seeded_book),
        )
    assert any("header" in n for n in result.notes)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def test_retrieve_assignment_code_no_syllabus_empty(temp_db: None) -> None:
    """Phase 4.5: AssignmentCodeQuery now resolves against Syllabus rows. When
    no syllabus is ingested, we still return an empty-with-note result (not
    an exception) so callers can surface the missing-syllabus hint."""
    with Session(db.get_engine()) as session:
        result = retrieve(session, AssignmentCodeQuery(corpus_id="c1", code="PROP-C5"))
    assert result.empty
    assert any("syllabus" in n.lower() for n in result.notes)


def test_retrieve_assignment_code_resolves_to_page_range(seeded_book: str) -> None:
    """End-to-end: seed a Syllabus + SyllabusEntry with a page range inside
    the seeded book; query by code returns the matching Pages/Blocks."""
    from data.models import Syllabus, SyllabusEntry

    with Session(db.get_engine()) as session:
        # Find the corpus that owns the seeded book.
        from data.models import Book

        book = session.exec(select(Book).where(Book.id == seeded_book)).one()
        corpus_id = book.corpus_id

        syl = Syllabus(corpus_id=corpus_id, title="Property Syllabus")
        session.add(syl)
        session.commit()
        session.refresh(syl)
        entry = SyllabusEntry(
            syllabus_id=syl.id,
            code="PROP-C5",
            title="Shelley + Notes",
            page_ranges=[[518, 520]],
            cases_assigned=["Shelley v. Kraemer"],
            topic_tags=["covenants"],
        )
        session.add(entry)
        session.commit()

    with Session(db.get_engine()) as session:
        result = retrieve(
            session, AssignmentCodeQuery(corpus_id=corpus_id, code="PROP-C5")
        )
    assert not result.empty
    assert "PROP-C5" in result.query_description
    # Should include pages 518, 519, 520.
    source_pages = {p.source_page for p in result.pages}
    assert {518, 519, 520}.issubset(source_pages)


def test_retrieve_assignment_code_unknown_code_empty(seeded_book: str) -> None:
    from data.models import Book, Syllabus

    with Session(db.get_engine()) as session:
        book = session.exec(select(Book).where(Book.id == seeded_book)).one()
        corpus_id = book.corpus_id
        syl = Syllabus(corpus_id=corpus_id, title="Property Syllabus")
        session.add(syl)
        session.commit()

    with Session(db.get_engine()) as session:
        result = retrieve(
            session, AssignmentCodeQuery(corpus_id=corpus_id, code="NONEXISTENT")
        )
    assert result.empty
    assert any("NONEXISTENT" in n for n in result.notes)


def test_retrieve_semantic_stub(temp_db: None) -> None:
    with Session(db.get_engine()) as session:
        result = retrieve(session, SemanticQuery(corpus_id="c1", text="state action"))
    assert result.empty
    assert any("Phase 1" in n or "voyage" in n.lower() for n in result.notes)


def test_retrieve_unknown_query_type_raises(temp_db: None) -> None:
    with Session(db.get_engine()) as session:
        with pytest.raises(TypeError, match="Unknown query type"):
            retrieve(session, "just a string")  # type: ignore[arg-type]
