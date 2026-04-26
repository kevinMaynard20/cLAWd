"""Unit tests for features/mc_questions.py (spec §5.12)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from data import db
from data.models import (
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    Page,
)
from features.mc_questions import (
    MCQuestionsError,
    MCQuestionsRequest,
    generate_mc_questions,
)
from primitives import generate as generate_module


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
    def __init__(self, payload_fn):
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(
            content=[_FakeTextContent(text=self._payload_fn(len(self.calls)))],
            usage=_FakeUsage(input_tokens=1500, output_tokens=900),
        )


class _FakeClient:
    def __init__(self, payload_fn):
        self.messages = _FakeMessages(payload_fn)


def _fake_factory(payload_fn):
    def _factory(_api_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _factory


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
def seeded_inputs(temp_env: None) -> dict[str, Any]:
    """Seed corpus + book + 2 pages + 2 blocks (one narrative, one case_opinion)."""
    engine = db.get_engine()
    ids: dict[str, Any] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        book = Book(
            id="b" * 64,
            corpus_id=corpus.id,
            title="Property",
            source_pdf_path="/p.pdf",
            source_page_min=500,
            source_page_max=520,
        )
        session.add(book)
        session.commit()
        ids["book_id"] = book.id

        page = Page(
            book_id=book.id,
            source_page=510,
            batch_pdf="b.pdf",
            pdf_page_start=900,
            pdf_page_end=900,
            markdown="# 510",
            raw_text="510",
        )
        session.add(page)
        session.commit()
        session.refresh(page)
        page_id = page.id

        block1 = Block(
            page_id=page_id,
            book_id=book.id,
            order_index=0,
            type=BlockType.NARRATIVE_TEXT,
            source_page=510,
            markdown="The doctrine of takings begins with Penn Central.",
            block_metadata={},
        )
        session.add(block1)
        opinion = Block(
            page_id=page_id,
            book_id=book.id,
            order_index=1,
            type=BlockType.CASE_OPINION,
            source_page=510,
            markdown="Penn Central Transp. Co. v. New York City...",
            block_metadata={"case_name": "Penn Central"},
        )
        session.add(opinion)
        session.commit()
        session.refresh(block1)
        session.refresh(opinion)
        ids["block1_id"] = block1.id
        ids["opinion_id"] = opinion.id

    return ids


def _mc_payload() -> str:
    body = {
        "topic": "Takings",
        "questions": [
            {
                "id": "q1",
                "stem": "Which test applies for non-physical regulations?",
                "options": [
                    {"letter": "A", "text": "Loretto per-se"},
                    {"letter": "B", "text": "Penn Central balancing"},
                    {"letter": "C", "text": "Strict scrutiny"},
                    {"letter": "D", "text": "Rational basis"},
                ],
                "correct_answer": "B",
                "explanation": "Penn Central is the default for non-physical regulations.",
                "distractor_explanations": {
                    "A": "Loretto only applies to permanent physical occupations.",
                    "C": "Strict scrutiny is for fundamental rights, not takings.",
                    "D": "Rational basis is the wrong framework here.",
                },
                "doctrine_tested": "Penn Central balancing",
                "source_block_ids": [],
            }
        ],
        "sources": [],
    }
    return json.dumps(body)


def test_mc_questions_happy_path(seeded_inputs: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _mc_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = MCQuestionsRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            book_id=seeded_inputs["book_id"],
            page_start=510,
            page_end=510,
            num_questions=1,
        )
        result = generate_mc_questions(session, req)
    assert result.artifact.type is ArtifactType.MC_QUESTION_SET
    assert result.cache_hit is False
    assert result.artifact.content["topic"] == "Takings"
    assert len(result.artifact.content["questions"]) == 1
    assert result.artifact.prompt_template.startswith("mc_questions@")


def test_mc_questions_cache_hit(seeded_inputs: dict[str, Any]) -> None:
    calls = {"n": 0}

    def payload(_n):
        calls["n"] += 1
        return _mc_payload()

    generate_module.set_anthropic_client_factory(_fake_factory(payload))
    engine = db.get_engine()
    with Session(engine) as session:
        req = MCQuestionsRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            book_id=seeded_inputs["book_id"],
            page_start=510,
            page_end=510,
            num_questions=1,
        )
        first = generate_mc_questions(session, req)
    with Session(engine) as session:
        req2 = MCQuestionsRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            book_id=seeded_inputs["book_id"],
            page_start=510,
            page_end=510,
            num_questions=1,
        )
        second = generate_mc_questions(session, req2)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls["n"] == 1
    assert second.artifact.id == first.artifact.id


def test_mc_questions_missing_referenced_artifact_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    """Bad book_id → MCQuestionsError (404-worthy)."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _mc_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = MCQuestionsRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            book_id="nope",
            page_start=510,
            page_end=510,
        )
        with pytest.raises(MCQuestionsError, match="not found"):
            generate_mc_questions(session, req)


def test_mc_questions_no_blocks_in_range_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    """Empty retrieval (no blocks in the page range) → MCQuestionsError."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _mc_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = MCQuestionsRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            book_id=seeded_inputs["book_id"],
            page_start=999,
            page_end=999,
        )
        with pytest.raises(MCQuestionsError, match="No source blocks"):
            generate_mc_questions(session, req)


def test_mc_questions_402_on_budget_exceeded(
    seeded_inputs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from costs import tracker as cost_tracker
    from costs.tracker import BudgetExceededError

    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "0.01")
    cost_tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        feature="test_seed",
    )
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _mc_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = MCQuestionsRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            book_id=seeded_inputs["book_id"],
            page_start=510,
            page_end=510,
        )
        with pytest.raises(BudgetExceededError):
            generate_mc_questions(session, req)
