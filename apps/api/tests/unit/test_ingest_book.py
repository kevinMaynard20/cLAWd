"""Unit tests for the full `ingest_book` orchestration (spec §4.1.1 1–8).

Marker is never actually invoked — `_run_marker_impl` is patched to return a
small fixture so the tests exercise step 3 (stitching) through step 8
(persistence) without the heavy dependency.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from data import db
from data.models import Block, Book, Corpus, Page, TocEntry
from primitives import marker_runner
from primitives.ingest import ingest_book


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh SQLite per test, plus a redirected Marker cache dir.

    Mirrors the pattern from `test_models.py` so ingestion tests share
    fixture shape with the data-model tests.
    """
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LAWSCHOOL_MARKER_CACHE_DIR", str(tmp_path / "marker_cache"))
    db.reset_engine()
    db.init_schema()
    yield tmp_path
    db.reset_engine()


def _fake_pdf(dir_: Path, name: str, bytes_: bytes) -> Path:
    p = dir_ / name
    p.write_bytes(bytes_)
    return p


def _fixture_markdown() -> str:
    """A 10-source-page synthetic fixture with bare-numeric markers and a
    single case header. Designed so `extract_page_markers_from_markdown`
    finds at least 3 pages and `segment_page_markdown` emits at least one
    non-narrative block per populated page."""
    return "\n".join(
        [
            "# Introduction",
            "",
            "Opening remarks for the fixture book.",
            "",
            "1",  # page 1 marker
            "",
            "Narrative for page 1. The rule applies when X, Y, and Z.",
            "",
            "2",  # page 2 marker
            "",
            "Smith v. Jones",
            "",
            "Supreme Court of Missouri, 1948",
            "",
            "The court held that the defendant owed a duty.",
            "",
            "3",  # page 3 marker
            "",
            "Notes and Questions",
            "",
            "1. Consider whether the rule would apply if X were false.",
        ]
    )


def _install_fake_marker(monkeypatch: pytest.MonkeyPatch, markdown: str) -> None:
    def fake_impl(
        pdf_path: Path, *, use_llm: bool, extract_images: bool
    ) -> marker_runner.MarkerResult:
        return marker_runner.MarkerResult(
            markdown=markdown,
            pdf_page_count=3,
            pdf_page_offsets=[0, max(1, len(markdown) // 3), max(2, (len(markdown) * 2) // 3)],
        )

    monkeypatch.setattr(marker_runner, "_run_marker_impl", fake_impl)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_ingest_book_happy_path(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_marker(monkeypatch, _fixture_markdown())
    pdf = _fake_pdf(temp_db, "book.pdf", b"%PDF-HAPPY\n")

    book = ingest_book(
        [pdf],
        corpus_id=None,
        title="Fixture Book",
        authors=["Test Author"],
    )

    # Book id is a SHA-256 hex (64 chars, lowercase hex).
    assert isinstance(book.id, str)
    assert len(book.id) == 64
    assert all(c in "0123456789abcdef" for c in book.id)

    with Session(db.get_engine()) as session:
        pages = session.exec(select(Page).where(Page.book_id == book.id)).all()
        assert len(pages) >= 3  # Three page markers = three pages
        blocks = session.exec(select(Block).where(Block.book_id == book.id)).all()
        assert len(blocks) > 0

        # The Book's source_page_min/max reflect the markers we extracted.
        loaded_book = session.get(Book, book.id)
        assert loaded_book is not None
        assert loaded_book.source_page_min == 1
        assert loaded_book.source_page_max == 3


def test_ingest_book_dedup(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_marker(monkeypatch, _fixture_markdown())
    pdf = _fake_pdf(temp_db, "book.pdf", b"%PDF-DEDUP\n")

    # First call: full pipeline runs.
    first = ingest_book([pdf], corpus_id=None, title="Dedup Book")

    # Now patch _run_marker_impl to raise if called — the second ingest must
    # hit the early-exit dedup branch and never touch Marker.
    def boom(*_a: object, **_k: object) -> marker_runner.MarkerResult:
        raise AssertionError("Marker should not be called on dedup hit")

    monkeypatch.setattr(marker_runner, "_run_marker_impl", boom)

    second = ingest_book([pdf], corpus_id=None, title="Dedup Book 2")
    assert second.id == first.id

    with Session(db.get_engine()) as session:
        books = session.exec(select(Book)).all()
        assert len([b for b in books if b.id == first.id]) == 1


def test_ingest_book_persists_toc_when_present(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixture with a `# Part I\\n\\n1\\n\\n## Chapter 1\\n\\n2` shape should
    yield persisted TocEntry rows after ingestion."""
    md = "\n".join(
        [
            "# Part I",
            "",
            "1",
            "",
            "## Chapter 1",
            "",
            "2",
            "",
            "### Section A",
        ]
    )
    _install_fake_marker(monkeypatch, md)
    pdf = _fake_pdf(temp_db, "toc.pdf", b"%PDF-TOC\n")

    book = ingest_book([pdf], corpus_id=None, title="TOC Book")

    with Session(db.get_engine()) as session:
        toc_entries = session.exec(
            select(TocEntry).where(TocEntry.book_id == book.id)
        ).all()
        assert len(toc_entries) >= 2
        titles = {e.title for e in toc_entries}
        assert "Part I" in titles
        assert "Chapter 1" in titles


def test_ingest_book_progress_callback(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_marker(monkeypatch, _fixture_markdown())
    pdf = _fake_pdf(temp_db, "book.pdf", b"%PDF-PROG\n")

    seen: list[str] = []

    def on_progress(step: str, _current: int, _total: int) -> None:
        seen.append(step)

    ingest_book(
        [pdf],
        corpus_id=None,
        title="Progress Book",
        on_progress=on_progress,
    )

    expected_steps = {
        "hashing",
        "marker",
        "stitching",
        "page_markers",
        "blocks",
        "toc",
        "persisting",
    }
    assert expected_steps.issubset(set(seen)), f"missing: {expected_steps - set(seen)}"


# ---------------------------------------------------------------------------
# Corpus handling
# ---------------------------------------------------------------------------


def test_ingest_book_missing_corpus_raises(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_marker(monkeypatch, _fixture_markdown())
    pdf = _fake_pdf(temp_db, "book.pdf", b"%PDF-MISSCORP\n")

    with pytest.raises(ValueError) as exc_info:
        ingest_book(
            [pdf],
            corpus_id="doesnotexist",
            title="t",
        )
    assert "corpus_id" in str(exc_info.value).lower() or "not found" in str(exc_info.value).lower()


def test_ingest_book_creates_corpus_when_none(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_marker(monkeypatch, _fixture_markdown())
    pdf = _fake_pdf(temp_db, "book.pdf", b"%PDF-AUTOCORP\n")

    book = ingest_book(
        [pdf],
        corpus_id=None,
        title="Auto-Corpus Book",
    )

    with Session(db.get_engine()) as session:
        corpus = session.get(Corpus, book.corpus_id)
        assert corpus is not None
        # The auto-created corpus takes the book's title as its name.
        assert corpus.name == "Auto-Corpus Book"
