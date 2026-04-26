"""Unit tests for features/flashcards.py (spec §5.3).

Mocks Anthropic via ``set_anthropic_client_factory`` and asserts:

- a FLASHCARD_SET artifact is persisted on happy path,
- FlashcardReview rows are seeded for every card,
- the cache hit on a second call returns the same artifact id without
  creating new review rows,
- ``due_cards`` filters by due_at correctly,
- ``record_review`` round-trips through SM-2 and updates the row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlmodel import Session, select

from data import db
from data.models import (
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    Credentials,
    FlashcardReview,
    Page,
)
from features.flashcards import (
    FlashcardGenerateRequest,
    due_cards,
    generate_flashcards,
    record_review,
)
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors test_hypo.py / test_case_brief.py)
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeTextContent:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list[_FakeTextContent]
    usage: _FakeUsage
    model: str = "claude-opus-4-7"
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, payload_fn) -> None:
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(
            content=[_FakeTextContent(text=self._payload_fn(len(self.calls)))],
            usage=_FakeUsage(input_tokens=2400, output_tokens=900),
        )


class _FakeClient:
    def __init__(self, payload_fn) -> None:
        self.messages = _FakeMessages(payload_fn)


def _factory(payload_fn):
    def _f(_api_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _f


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "creds.enc"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()

    from costs import tracker

    tracker.reset_session_id()

    yield
    generate_module.set_anthropic_client_factory(None)
    db.reset_engine()


@pytest.fixture
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        generate_module,
        "load_credentials",
        lambda: Credentials(
            anthropic_api_key=SecretStr("sk-ant-test-FAKEKEY-1234567890-LAST")
        ),
    )


@pytest.fixture
def seeded_book(temp_db: None) -> dict[str, str]:
    """Seed a corpus + book + a couple of pages and blocks so PageRange
    retrieval has something to return."""
    engine = db.get_engine()
    ids: dict[str, str] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        book = Book(
            id="b" * 64,
            corpus_id=corpus.id,
            title="Property",
            source_pdf_path="/p.pdf",
            source_page_min=518,
            source_page_max=520,
        )
        session.add(book)
        session.commit()
        ids["book_id"] = book.id

        page = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="b.pdf",
            pdf_page_start=1000,
            pdf_page_end=1001,
            markdown="# page 518",
            raw_text="page 518",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        blocks = [
            Block(
                page_id=page.id,
                book_id=book.id,
                order_index=0,
                type=BlockType.NARRATIVE_TEXT,
                source_page=518,
                markdown="Loretto established a per-se taking for permanent physical occupation.",
                block_metadata={},
            ),
            Block(
                page_id=page.id,
                book_id=book.id,
                order_index=1,
                type=BlockType.NARRATIVE_TEXT,
                source_page=518,
                markdown="Penn Central announced a three-factor balancing test for regulatory takings.",
                block_metadata={},
            ),
        ]
        for b in blocks:
            session.add(b)
        session.commit()
        for b in blocks:
            session.refresh(b)
        ids["block_ids"] = ",".join([b.id for b in blocks])

    return ids


# ---------------------------------------------------------------------------
# Payload factory matching schemas/flashcards.json
# ---------------------------------------------------------------------------


def _flashcards_payload(block_ids: list[str]) -> str:
    body = {
        "topic": "Regulatory takings",
        "cards": [
            {
                "id": "loretto_per_se_rule",
                "kind": "rule",
                "front": "What is the rule from Loretto?",
                "back": "Permanent physical occupation is a per-se taking.",
                "source_block_ids": [block_ids[0]],
            },
            {
                "id": "penn_central_test",
                "kind": "test_for",
                "front": "What is the test for regulatory takings under Penn Central?",
                "back": "Three-factor balancing: economic impact, investment-backed expectations, character of the action.",
                "source_block_ids": [block_ids[1]],
            },
            {
                "id": "loretto_vs_penn_central",
                "kind": "compare_contrast",
                "front": "Distinguish Loretto from Penn Central.",
                "back": "Loretto is a per-se rule for physical occupation; Penn Central balances factors for non-occupation regs.",
                "source_block_ids": block_ids,
            },
        ],
        "sources": list(block_ids),
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_generate_flashcards_happy_path(
    seeded_book: dict[str, str],
    fake_credentials: None,
) -> None:
    """Artifact persisted, FlashcardReview rows seeded for every card."""
    block_ids = seeded_book["block_ids"].split(",")
    generate_module.set_anthropic_client_factory(
        _factory(lambda _n: _flashcards_payload(block_ids))
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = generate_flashcards(
            session,
            FlashcardGenerateRequest(
                corpus_id=seeded_book["corpus_id"],
                topic="Regulatory takings",
                book_id=seeded_book["book_id"],
                page_start=518,
                page_end=518,
            ),
        )

    assert result.artifact.type == ArtifactType.FLASHCARD_SET
    assert result.cache_hit is False
    assert len(result.artifact.content["cards"]) == 3

    # FlashcardReview rows seeded.
    with Session(engine) as session:
        rows = session.exec(
            select(FlashcardReview).where(
                FlashcardReview.flashcard_set_id == result.artifact.id
            )
        ).all()
        assert len(rows) == 3
        # Default seed state.
        for row in rows:
            assert row.repetitions == 0
            assert row.interval_days == 0
            assert row.ease_factor == 2.5
            assert row.last_grade is None
            assert row.due_at is not None
            assert row.corpus_id == seeded_book["corpus_id"]
        # All card_ids accounted for.
        seeded_ids = {r.card_id for r in rows}
        assert seeded_ids == {
            "loretto_per_se_rule",
            "penn_central_test",
            "loretto_vs_penn_central",
        }


def test_generate_flashcards_cache_hit(
    seeded_book: dict[str, str],
    fake_credentials: None,
) -> None:
    """Second call returns the same artifact id and creates no new reviews."""
    block_ids = seeded_book["block_ids"].split(",")
    call_count = {"n": 0}

    def payload(_call_n: int) -> str:
        call_count["n"] += 1
        return _flashcards_payload(block_ids)

    generate_module.set_anthropic_client_factory(_factory(payload))

    engine = db.get_engine()
    with Session(engine) as session:
        first = generate_flashcards(
            session,
            FlashcardGenerateRequest(
                corpus_id=seeded_book["corpus_id"],
                topic="Regulatory takings",
                book_id=seeded_book["book_id"],
                page_start=518,
                page_end=518,
            ),
        )

    with Session(engine) as session:
        second = generate_flashcards(
            session,
            FlashcardGenerateRequest(
                corpus_id=seeded_book["corpus_id"],
                topic="Regulatory takings",
                book_id=seeded_book["book_id"],
                page_start=518,
                page_end=518,
            ),
        )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.artifact.id == first.artifact.id
    assert call_count["n"] == 1  # only one Anthropic call

    # No new review rows on cache hit.
    with Session(engine) as session:
        rows = session.exec(
            select(FlashcardReview).where(
                FlashcardReview.flashcard_set_id == first.artifact.id
            )
        ).all()
        assert len(rows) == 3


def test_due_cards_returns_only_past_due(
    seeded_book: dict[str, str],
    fake_credentials: None,
) -> None:
    """Seed reviews with various due_at; due_cards(now=t) returns only past-due."""
    block_ids = seeded_book["block_ids"].split(",")
    generate_module.set_anthropic_client_factory(
        _factory(lambda _n: _flashcards_payload(block_ids))
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = generate_flashcards(
            session,
            FlashcardGenerateRequest(
                corpus_id=seeded_book["corpus_id"],
                topic="Regulatory takings",
                book_id=seeded_book["book_id"],
                page_start=518,
                page_end=518,
            ),
        )
    set_id = result.artifact.id

    now = datetime.now(tz=UTC)
    yesterday = now - timedelta(days=1)
    tomorrow = now + timedelta(days=1)

    # Reset all to known-past, then push one review into the future.
    with Session(engine) as session:
        rows = list(
            session.exec(
                select(FlashcardReview).where(
                    FlashcardReview.flashcard_set_id == set_id
                )
            ).all()
        )
        # Deterministic ordering for the assertions below.
        rows.sort(key=lambda r: r.card_id)
        rows[0].due_at = yesterday  # past — should appear
        rows[1].due_at = now - timedelta(seconds=1)  # past — should appear
        rows[2].due_at = tomorrow  # future — should NOT appear
        for r in rows:
            session.add(r)
        session.commit()

    with Session(engine) as session:
        due = due_cards(session, corpus_id=seeded_book["corpus_id"], now=now)

    assert len(due) == 2
    # Oldest due first.
    assert due[0]["due_at"] <= due[1]["due_at"]
    returned_ids = {entry["card_id"] for entry in due}
    assert "loretto_per_se_rule" in returned_ids or "loretto_vs_penn_central" in returned_ids
    # Each entry carries the card payload.
    for entry in due:
        card = entry["card"]
        assert "front" in card
        assert "back" in card
        assert card["id"] == entry["card_id"]


def test_record_review_remembers_updates_interval(
    seeded_book: dict[str, str],
    fake_credentials: None,
) -> None:
    """grade=4 then grade=4 again → interval transitions 1 → 6."""
    block_ids = seeded_book["block_ids"].split(",")
    generate_module.set_anthropic_client_factory(
        _factory(lambda _n: _flashcards_payload(block_ids))
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = generate_flashcards(
            session,
            FlashcardGenerateRequest(
                corpus_id=seeded_book["corpus_id"],
                topic="Regulatory takings",
                book_id=seeded_book["book_id"],
                page_start=518,
                page_end=518,
            ),
        )
    set_id = result.artifact.id

    # First review: grade 4 (remember). Should set interval=1, reps=1.
    with Session(engine) as session:
        before = datetime.now(tz=UTC).replace(tzinfo=None)
        row = record_review(session, set_id=set_id, card_id="loretto_per_se_rule", grade=4)
        after = datetime.now(tz=UTC).replace(tzinfo=None)

    assert row.repetitions == 1
    assert row.interval_days == 1
    assert row.last_grade == 4
    assert row.due_at is not None
    # +1 day since last_reviewed_at, which we just set to ~now. SQLite
    # round-trips drop tzinfo so we compare the naive forms.
    due_naive = row.due_at.replace(tzinfo=None) if row.due_at.tzinfo else row.due_at
    assert due_naive - timedelta(days=1) >= before - timedelta(seconds=1)
    assert due_naive - timedelta(days=1) <= after + timedelta(seconds=1)

    # Second review: grade 4 again. Should set interval=6, reps=2.
    with Session(engine) as session:
        row2 = record_review(session, set_id=set_id, card_id="loretto_per_se_rule", grade=4)
    assert row2.repetitions == 2
    assert row2.interval_days == 6


def test_record_review_forgets_resets(
    seeded_book: dict[str, str],
    fake_credentials: None,
) -> None:
    """Remember (grade 4 → reps=1) then forget (grade 1 → reps=0, interval=1)."""
    block_ids = seeded_book["block_ids"].split(",")
    generate_module.set_anthropic_client_factory(
        _factory(lambda _n: _flashcards_payload(block_ids))
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = generate_flashcards(
            session,
            FlashcardGenerateRequest(
                corpus_id=seeded_book["corpus_id"],
                topic="Regulatory takings",
                book_id=seeded_book["book_id"],
                page_start=518,
                page_end=518,
            ),
        )
    set_id = result.artifact.id

    with Session(engine) as session:
        row1 = record_review(session, set_id=set_id, card_id="penn_central_test", grade=4)
    assert row1.repetitions == 1

    # Now forget.
    with Session(engine) as session:
        row2 = record_review(session, set_id=set_id, card_id="penn_central_test", grade=1)
    assert row2.repetitions == 0
    assert row2.interval_days == 1
    assert row2.last_grade == 1
