"""Integration tests for the flashcards routes (spec §5.3).

Mocks Anthropic via ``set_anthropic_client_factory`` and exercises:

- POST /features/flashcards: 200 happy path with persisted artifact +
  seeded reviews.
- GET /flashcards/due: corpus filter; another corpus's cards never leak.
- POST /flashcards/review: round-trip through SM-2 returns expected
  schedule on remember (interval=1) and forget (interval=1, reps=0).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from data import db
from data.models import (
    Block,
    BlockType,
    Book,
    Corpus,
    FlashcardReview,
    Page,
)
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors test_emphasis_route.py pattern)
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


def _fake_factory(payload_fn):
    def _factory(_api_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "creds.enc"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()

    from costs import tracker
    from credentials import keyring_backend

    tracker.reset_session_id()
    keyring_backend.store_anthropic_key("sk-ant-test-FAKEKEY-1234567890-LAST")

    yield
    generate_module.set_anthropic_client_factory(None)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


def _seed_book(corpus_name: str, book_id_seed: str) -> dict[str, str]:
    """Helper: seed a corpus + book + a couple of blocks. Returns ids."""
    engine = db.get_engine()
    ids: dict[str, str] = {}
    with Session(engine) as session:
        corpus = Corpus(name=corpus_name, course=corpus_name)
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        book = Book(
            id=book_id_seed * 64,
            corpus_id=corpus.id,
            title=corpus_name,
            source_pdf_path=f"/{corpus_name}.pdf",
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
            pdf_page_start=1,
            pdf_page_end=2,
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
                markdown="Loretto: per-se taking for permanent physical occupation.",
                block_metadata={},
            ),
            Block(
                page_id=page.id,
                book_id=book.id,
                order_index=1,
                type=BlockType.NARRATIVE_TEXT,
                source_page=518,
                markdown="Penn Central: three-factor balancing test.",
                block_metadata={},
            ),
        ]
        for b in blocks:
            session.add(b)
        session.commit()
        for b in blocks:
            session.refresh(b)
        ids["block_id_a"] = blocks[0].id
        ids["block_id_b"] = blocks[1].id
    return ids


@pytest.fixture
def seeded_book(temp_env: None) -> dict[str, str]:
    return _seed_book("Property", "a")


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
                "front": "Rule from Loretto?",
                "back": "Permanent physical occupation is a per-se taking.",
                "source_block_ids": [block_ids[0]],
            },
            {
                "id": "penn_central_test",
                "kind": "test_for",
                "front": "Test under Penn Central?",
                "back": "Three-factor balancing.",
                "source_block_ids": [block_ids[1]],
            },
        ],
        "sources": list(block_ids),
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_generate_flashcards_happy_path(
    client: TestClient, seeded_book: dict[str, str]
) -> None:
    """200 with persisted FLASHCARD_SET artifact + reviews seeded."""
    block_ids = [seeded_book["block_id_a"], seeded_book["block_id_b"]]
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _flashcards_payload(block_ids))
    )

    r = client.post(
        "/features/flashcards",
        json={
            "corpus_id": seeded_book["corpus_id"],
            "topic": "Regulatory takings",
            "book_id": seeded_book["book_id"],
            "page_start": 518,
            "page_end": 518,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    art = body["artifact"]
    assert art["type"] == "flashcard_set"
    assert len(art["content"]["cards"]) == 2

    # Verify FlashcardReview rows landed in the DB.
    engine = db.get_engine()
    with Session(engine) as session:
        rows = session.exec(
            select(FlashcardReview).where(
                FlashcardReview.flashcard_set_id == art["id"]
            )
        ).all()
        assert len(rows) == 2


def test_route_due_cards_filter_by_corpus(
    client: TestClient, seeded_book: dict[str, str]
) -> None:
    """A second corpus's cards never appear in the first corpus's queue."""
    # First corpus's set.
    block_ids_a = [seeded_book["block_id_a"], seeded_book["block_id_b"]]
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _flashcards_payload(block_ids_a))
    )
    r1 = client.post(
        "/features/flashcards",
        json={
            "corpus_id": seeded_book["corpus_id"],
            "topic": "Regulatory takings",
            "book_id": seeded_book["book_id"],
            "page_start": 518,
            "page_end": 518,
        },
    )
    assert r1.status_code == 200, r1.text
    set_a_id = r1.json()["artifact"]["id"]

    # Second corpus with its own data + set.
    seeded_b = _seed_book("Civ Pro", "c")
    block_ids_b = [seeded_b["block_id_a"], seeded_b["block_id_b"]]
    # Use a distinct payload (different card ids) so the cache key differs.
    payload_b = json.dumps(
        {
            "topic": "Pleadings",
            "cards": [
                {
                    "id": "rule_8_short_plain",
                    "kind": "rule",
                    "front": "Rule 8 standard?",
                    "back": "Short and plain statement.",
                    "source_block_ids": [block_ids_b[0]],
                },
            ],
            "sources": [block_ids_b[0]],
        }
    )
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: payload_b)
    )
    r2 = client.post(
        "/features/flashcards",
        json={
            "corpus_id": seeded_b["corpus_id"],
            "topic": "Pleadings",
            "book_id": seeded_b["book_id"],
            "page_start": 518,
            "page_end": 518,
        },
    )
    assert r2.status_code == 200, r2.text

    # Default seed has due_at=now, so both should be due immediately.
    # Query A's queue — should include only Property cards.
    r_due = client.get(
        "/flashcards/due",
        params={"corpus_id": seeded_book["corpus_id"]},
    )
    assert r_due.status_code == 200
    cards = r_due.json()
    assert len(cards) == 2
    set_ids = {c["set_id"] for c in cards}
    assert set_ids == {set_a_id}
    card_ids = {c["card_id"] for c in cards}
    assert card_ids == {"loretto_per_se_rule", "penn_central_test"}

    # Query B's queue — should include only Civ Pro card.
    r_due_b = client.get(
        "/flashcards/due",
        params={"corpus_id": seeded_b["corpus_id"]},
    )
    assert r_due_b.status_code == 200
    cards_b = r_due_b.json()
    assert len(cards_b) == 1
    assert cards_b[0]["card_id"] == "rule_8_short_plain"


def test_route_record_review_round_trips_sm2(
    client: TestClient, seeded_book: dict[str, str]
) -> None:
    """POST /flashcards/review applies SM-2 and returns the schedule."""
    block_ids = [seeded_book["block_id_a"], seeded_book["block_id_b"]]
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _flashcards_payload(block_ids))
    )

    r = client.post(
        "/features/flashcards",
        json={
            "corpus_id": seeded_book["corpus_id"],
            "topic": "Regulatory takings",
            "book_id": seeded_book["book_id"],
            "page_start": 518,
            "page_end": 518,
        },
    )
    assert r.status_code == 200, r.text
    set_id = r.json()["artifact"]["id"]

    # First review: grade 4. Expect reps=1, interval=1.
    r1 = client.post(
        "/flashcards/review",
        json={
            "set_id": set_id,
            "card_id": "loretto_per_se_rule",
            "grade": 4,
        },
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["repetitions"] == 1
    assert body1["interval_days"] == 1
    assert body1["last_grade"] == 4
    assert body1["due_at"] is not None

    # Second review: grade 4 again. Expect reps=2, interval=6.
    r2 = client.post(
        "/flashcards/review",
        json={
            "set_id": set_id,
            "card_id": "loretto_per_se_rule",
            "grade": 4,
        },
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["repetitions"] == 2
    assert body2["interval_days"] == 6

    # Forget on a different card: grade 1 from a fresh seed → reps=0,
    # interval=1, last_grade=1.
    r3 = client.post(
        "/flashcards/review",
        json={
            "set_id": set_id,
            "card_id": "penn_central_test",
            "grade": 1,
        },
    )
    assert r3.status_code == 200, r3.text
    body3 = r3.json()
    assert body3["repetitions"] == 0
    assert body3["interval_days"] == 1
    assert body3["last_grade"] == 1
