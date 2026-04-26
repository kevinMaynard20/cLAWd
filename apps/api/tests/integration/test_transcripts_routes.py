"""Integration tests for /transcripts routes (spec §4.1.2, §3.8/§3.9).

Same fake-Anthropic-client injection pattern as ``test_case_brief.py`` and
``test_rubric_extraction_route.py`` — attached to the feature's own
``set_client_factory`` hook, not generate()'s (the transcript-cleanup call
bypasses generate()).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db
from data.models import Block, BlockType, Book, Corpus, Page
from features import transcript_ingest

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors the generate()-test pattern)
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
    model: str = "claude-haiku-4-5"
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, payload_fn):
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._payload_fn(len(self.calls))
        return _FakeResponse(
            content=[_FakeTextContent(text=payload)],
            usage=_FakeUsage(input_tokens=500, output_tokens=300),
        )


class _FakeClient:
    def __init__(self, payload_fn):
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
    transcript_ingest.set_client_factory(None)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


@pytest.fixture
def seeded_corpus(temp_env: None) -> str:
    """One corpus with one Shelley case_opinion block so known_case_names
    has something useful for the fuzzy-resolver safety net."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        corpus_id = corpus.id

        book = Book(
            id="a" * 64,
            corpus_id=corpus_id,
            title="Property Casebook",
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=2,
        )
        session.add(book)
        session.commit()

        page = Page(
            book_id=book.id,
            source_page=1,
            batch_pdf="b.pdf",
            pdf_page_start=0,
            pdf_page_end=1,
            markdown="# p1",
            raw_text="p1",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        block = Block(
            page_id=page.id,
            book_id=book.id,
            order_index=0,
            type=BlockType.CASE_OPINION,
            source_page=1,
            markdown="Shelley opinion",
            block_metadata={"case_name": "Shelley v. Kraemer"},
        )
        session.add(block)
        session.commit()

    return corpus_id


# ---------------------------------------------------------------------------
# Cleanup payload helper
# ---------------------------------------------------------------------------


def _cleanup_payload(cleaned_text: str = "Segment A. Segment B. Segment C.") -> str:
    n = len(cleaned_text)
    third = max(1, n // 3)
    return json.dumps(
        {
            "cleaned_text": cleaned_text,
            "segments": [
                {
                    "start_char": 0,
                    "end_char": third,
                    "speaker": "professor",
                    "content": cleaned_text[:third],
                    "mentioned_cases": ["Shelley v. Kraemer"],
                    "mentioned_rules": [],
                    "mentioned_concepts": [],
                    "sentiment_flags": [],
                },
                {
                    "start_char": third,
                    "end_char": 2 * third,
                    "speaker": "student",
                    "content": cleaned_text[third : 2 * third],
                    "mentioned_cases": [],
                    "mentioned_rules": [],
                    "mentioned_concepts": [],
                    "sentiment_flags": [],
                },
                {
                    "start_char": 2 * third,
                    "end_char": n,
                    "speaker": "professor",
                    "content": cleaned_text[2 * third :],
                    "mentioned_cases": [],
                    "mentioned_rules": [],
                    "mentioned_concepts": [],
                    "sentiment_flags": [],
                },
            ],
            "unresolved_mentions": [],
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_ingest_transcript_happy_path(
    client: TestClient, seeded_corpus: str
) -> None:
    """POST /transcripts with valid raw_text → 200 + transcript_id +
    mentioned_cases."""
    transcript_ingest.set_client_factory(
        _fake_factory(lambda _n: _cleanup_payload())
    )

    r = client.post(
        "/transcripts",
        json={
            "corpus_id": seeded_corpus,
            "raw_text": "raw gemini text",
            "topic": "Takings",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    assert body["segment_count"] == 3
    assert "Shelley v. Kraemer" in body["mentioned_cases"]
    assert isinstance(body["transcript_id"], str)
    assert len(body["transcript_id"]) == 64  # SHA-256 hex


def test_route_get_transcript(
    client: TestClient, seeded_corpus: str
) -> None:
    """GET /transcripts/{id} returns the full transcript + segments."""
    transcript_ingest.set_client_factory(
        _fake_factory(lambda _n: _cleanup_payload())
    )

    r1 = client.post(
        "/transcripts",
        json={"corpus_id": seeded_corpus, "raw_text": "some transcript text"},
    )
    assert r1.status_code == 200
    tid = r1.json()["transcript_id"]

    r2 = client.get(f"/transcripts/{tid}")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["id"] == tid
    assert body["corpus_id"] == seeded_corpus
    assert body["source_type"] == "text"
    assert len(body["segments"]) == 3
    # Segment ordering by order_index.
    assert [s["order_index"] for s in body["segments"]] == [0, 1, 2]


def test_route_list_transcripts_by_corpus(
    client: TestClient, seeded_corpus: str
) -> None:
    """GET /transcripts?corpus_id=... returns summary rows for that corpus."""
    transcript_ingest.set_client_factory(
        _fake_factory(lambda _n: _cleanup_payload())
    )

    # Ingest two distinct transcripts.
    client.post(
        "/transcripts",
        json={
            "corpus_id": seeded_corpus,
            "raw_text": "lecture one",
            "topic": "A",
        },
    )
    client.post(
        "/transcripts",
        json={
            "corpus_id": seeded_corpus,
            "raw_text": "lecture two",
            "topic": "B",
        },
    )

    r = client.get(f"/transcripts?corpus_id={seeded_corpus}")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    topics = {item["topic"] for item in items}
    assert topics == {"A", "B"}
    # Summary shape — no full text.
    for item in items:
        assert "raw_text" not in item
        assert "segments" not in item


def test_route_get_unknown_transcript_404(
    client: TestClient, seeded_corpus: str
) -> None:
    """Sanity check: requesting an unknown transcript returns 404."""
    r = client.get("/transcripts/deadbeef")
    assert r.status_code == 404


def test_route_list_unknown_corpus_404(client: TestClient) -> None:
    """Listing transcripts for a corpus that doesn't exist → 404."""
    r = client.get("/transcripts?corpus_id=no-such-corpus")
    assert r.status_code == 404
