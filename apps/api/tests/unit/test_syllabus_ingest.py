"""Unit tests for features/syllabus_ingest.py (spec §4.1.4)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from data import db
from data.models import Book, Corpus, Syllabus, SyllabusEntry
from features import syllabus_ingest as feature

# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _Response:
    content: list[_Text]
    usage: _Usage
    model: str = "claude-sonnet-4-6"


class _FakeMessages:
    def __init__(self, payload_fn):
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(
            content=[_Text(text=self._payload_fn(len(self.calls)))],
            usage=_Usage(input_tokens=500, output_tokens=800),
        )


class _FakeClient:
    def __init__(self, payload_fn):
        self.messages = _FakeMessages(payload_fn)


def _factory(payload_fn):
    def _f(_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _f


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "c.enc"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()
    from costs import tracker
    from credentials import keyring_backend

    tracker.reset_session_id()
    keyring_backend.store_anthropic_key("sk-ant-test-FAKEKEY-1234567890-LAST")
    yield
    feature.set_anthropic_client_factory(None)
    db.reset_engine()


@pytest.fixture
def seeded_corpus_and_book(temp_env: None) -> tuple[str, str]:
    """Returns (corpus_id, book_id)."""
    engine = db.get_engine()
    with Session(engine) as session:
        c = Corpus(name="Property", course="Property")
        session.add(c)
        session.commit()
        session.refresh(c)
        book = Book(
            id="b" * 64,
            corpus_id=c.id,
            title="Property Casebook",
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=1000,
        )
        session.add(book)
        session.commit()
        return c.id, book.id


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def _syllabus_payload(
    *,
    title: str = "Property — Pollack — Spring 2026 Syllabus",
    entries: list[dict] | None = None,
) -> str:
    if entries is None:
        entries = [
            {
                "code": "PROP-C5",
                "title": "Easements I",
                "page_ranges": [[498, 521]],
                "cases_assigned": ["Willard v. First Church"],
                "topic_tags": ["easements", "creation"],
            },
            {
                "code": "PROP-C6",
                "title": "Easements II",
                "page_ranges": [[522, 550]],
                "cases_assigned": [],
                "topic_tags": ["easements", "termination"],
            },
        ]
    return json.dumps({"title": title, "entries": entries})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ingest_syllabus_happy_path(seeded_corpus_and_book: tuple[str, str]) -> None:
    corpus_id, book_id = seeded_corpus_and_book
    feature.set_anthropic_client_factory(_factory(lambda _n: _syllabus_payload()))
    with Session(db.get_engine()) as session:
        result = feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id=corpus_id,
                syllabus_markdown="<syllabus text>",
                book_id=book_id,
            ),
        )
    assert result.syllabus.title.startswith("Property")
    assert len(result.entries) == 2
    assert result.entries[0].code == "PROP-C5"
    assert result.entries[0].page_ranges == [[498, 521]]
    assert result.discrepancies == []


def test_ingest_syllabus_detects_page_range_discrepancies(
    seeded_corpus_and_book: tuple[str, str],
) -> None:
    corpus_id, book_id = seeded_corpus_and_book
    # Book covers 1..1000; syllabus claims pp 1200–1250 for PROP-C7
    bad_entries = [
        {
            "code": "PROP-C7",
            "title": "Beyond the book",
            "page_ranges": [[1200, 1250]],
            "cases_assigned": [],
            "topic_tags": [],
        },
    ]
    feature.set_anthropic_client_factory(
        _factory(lambda _n: _syllabus_payload(entries=bad_entries))
    )
    with Session(db.get_engine()) as session:
        result = feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id=corpus_id,
                syllabus_markdown="<syllabus>",
                book_id=book_id,
            ),
        )
    assert len(result.discrepancies) == 1
    d = result.discrepancies[0]
    assert d.code == "PROP-C7"
    assert d.page_range == (1200, 1250)
    assert "batches" in d.message


def test_ingest_syllabus_without_book_id_skips_validation(
    seeded_corpus_and_book: tuple[str, str],
) -> None:
    corpus_id, _ = seeded_corpus_and_book
    feature.set_anthropic_client_factory(_factory(lambda _n: _syllabus_payload()))
    with Session(db.get_engine()) as session:
        result = feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id=corpus_id,
                syllabus_markdown="<syllabus>",
                book_id=None,
            ),
        )
    assert result.discrepancies == []


def test_ingest_syllabus_persists_entries(
    seeded_corpus_and_book: tuple[str, str],
) -> None:
    corpus_id, book_id = seeded_corpus_and_book
    feature.set_anthropic_client_factory(_factory(lambda _n: _syllabus_payload()))
    with Session(db.get_engine()) as session:
        feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id=corpus_id,
                syllabus_markdown="<syllabus>",
                book_id=book_id,
            ),
        )

    with Session(db.get_engine()) as session:
        syllabus_rows = session.exec(select(Syllabus)).all()
        entry_rows = session.exec(select(SyllabusEntry)).all()
        assert len(syllabus_rows) == 1
        assert len(entry_rows) == 2
        codes = {e.code for e in entry_rows}
        assert codes == {"PROP-C5", "PROP-C6"}


def test_ingest_syllabus_missing_corpus_raises(temp_env: None) -> None:
    feature.set_anthropic_client_factory(_factory(lambda _n: _syllabus_payload()))
    with (
        Session(db.get_engine()) as session,
        pytest.raises(feature.SyllabusIngestError, match="Corpus"),
    ):
        feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id="nonexistent",
                syllabus_markdown="<syllabus>",
            ),
        )


def test_ingest_syllabus_missing_book_raises(
    seeded_corpus_and_book: tuple[str, str],
) -> None:
    corpus_id, _ = seeded_corpus_and_book
    feature.set_anthropic_client_factory(_factory(lambda _n: _syllabus_payload()))
    with (
        Session(db.get_engine()) as session,
        pytest.raises(feature.SyllabusIngestError, match="Book"),
    ):
        feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id=corpus_id,
                syllabus_markdown="<syllabus>",
                book_id="nonexistent-book",
            ),
        )


def test_ingest_syllabus_emits_cost_event(
    seeded_corpus_and_book: tuple[str, str],
) -> None:
    corpus_id, book_id = seeded_corpus_and_book
    feature.set_anthropic_client_factory(_factory(lambda _n: _syllabus_payload()))
    with Session(db.get_engine()) as session:
        feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id=corpus_id,
                syllabus_markdown="<syllabus>",
                book_id=book_id,
            ),
        )
    from costs import tracker

    events = tracker.recent_events(feature="syllabus_extraction")
    assert len(events) == 1
    assert events[0].model.startswith("claude-sonnet")


def test_ingest_syllabus_malformed_llm_output_raises(
    seeded_corpus_and_book: tuple[str, str],
) -> None:
    """LLM returns schema-invalid JSON → feature error with actionable message."""
    corpus_id, book_id = seeded_corpus_and_book
    bad = json.dumps({"title": "ok", "entries": [{"code": "X"}]})  # missing required fields
    feature.set_anthropic_client_factory(_factory(lambda _n: bad))
    with (
        Session(db.get_engine()) as session,
        pytest.raises(feature.SyllabusIngestError, match="schema"),
    ):
        feature.ingest_syllabus(
            session,
            feature.SyllabusIngestRequest(
                corpus_id=corpus_id,
                syllabus_markdown="<syllabus>",
                book_id=book_id,
            ),
        )
