"""Unit tests for features/outline.py (spec §5.11)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Book,
    Corpus,
    CreatedBy,
    TocEntry,
)
from features.outline import (
    OutlineError,
    OutlineRequest,
    generate_outline,
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
            usage=_FakeUsage(input_tokens=2000, output_tokens=1500),
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
    """Seed corpus + book + 3 toc entries + 2 case briefs + 1 flashcard set."""
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
            source_page_min=1,
            source_page_max=1000,
        )
        session.add(book)
        session.commit()
        ids["book_id"] = book.id

        for idx, (level, title, page) in enumerate(
            [
                (1, "Part I — Estates", 100),
                (2, "Defeasible Fees", 110),
                (1, "Part II — Takings", 500),
            ]
        ):
            session.add(
                TocEntry(
                    book_id=book.id,
                    level=level,
                    title=title,
                    source_page=page,
                    order_index=idx,
                )
            )

        brief1 = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "case_name": "Loretto",
                "rule": {"text": "Permanent occupation = per-se."},
            },
        )
        session.add(brief1)
        brief2 = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "case_name": "Penn Central",
                "rule": {"text": "Penn Central balancing test."},
            },
        )
        session.add(brief2)
        flash = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.FLASHCARD_SET,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={"cards": [{"q": "What is per-se?", "a": "permanent physical"}]},
        )
        session.add(flash)
        session.commit()
        session.refresh(brief1)
        session.refresh(brief2)
        session.refresh(flash)
        ids["brief1_id"] = brief1.id
        ids["brief2_id"] = brief2.id
        ids["flash_id"] = flash.id

    return ids


def _outline_payload() -> str:
    body = {
        "course": "Property",
        "topics": [
            {
                "title": "Part I — Estates",
                "level": 1,
                "toc_source_page": 100,
                "rule_statements": ["Permanent occupation = per-se."],
                "controlling_cases": [{"case_name": "Loretto"}],
                "policy_rationales": [],
                "exam_traps": [],
                "cross_references": [],
                "children": [
                    {
                        "title": "Defeasible Fees",
                        "level": 2,
                        "toc_source_page": 110,
                        "rule_statements": [],
                        "controlling_cases": [],
                        "policy_rationales": [],
                        "exam_traps": [],
                        "cross_references": [],
                    }
                ],
            },
            {
                "title": "Part II — Takings",
                "level": 1,
                "toc_source_page": 500,
                "rule_statements": ["Penn Central balancing test."],
                "controlling_cases": [{"case_name": "Penn Central"}],
                "policy_rationales": [],
                "exam_traps": [],
                "cross_references": [],
            },
        ],
        "sources": [],
    }
    return json.dumps(body)


def test_outline_happy_path(seeded_inputs: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _outline_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = OutlineRequest(
            corpus_id=seeded_inputs["corpus_id"],
            course="Property",
            book_id=seeded_inputs["book_id"],
        )
        result = generate_outline(session, req)
    assert result.artifact.type is ArtifactType.OUTLINE
    assert result.cache_hit is False
    assert result.artifact.content["course"] == "Property"
    assert len(result.artifact.content["topics"]) == 2
    # 2 case_briefs + 1 flashcard_set.
    assert result.input_artifact_count == 3
    assert result.artifact.prompt_template.startswith("outline_hierarchical@")


def test_outline_cache_hit(seeded_inputs: dict[str, Any]) -> None:
    calls = {"n": 0}

    def payload(_n):
        calls["n"] += 1
        return _outline_payload()

    generate_module.set_anthropic_client_factory(_fake_factory(payload))
    engine = db.get_engine()
    with Session(engine) as session:
        req = OutlineRequest(
            corpus_id=seeded_inputs["corpus_id"],
            course="Property",
            book_id=seeded_inputs["book_id"],
        )
        first = generate_outline(session, req)
    with Session(engine) as session:
        req2 = OutlineRequest(
            corpus_id=seeded_inputs["corpus_id"],
            course="Property",
            book_id=seeded_inputs["book_id"],
        )
        second = generate_outline(session, req2)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls["n"] == 1
    assert second.artifact.id == first.artifact.id


def test_outline_missing_referenced_artifact_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    """Bad book_id → OutlineError (404-worthy)."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _outline_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = OutlineRequest(
            corpus_id=seeded_inputs["corpus_id"],
            course="Property",
            book_id="nope",
        )
        with pytest.raises(OutlineError, match="book"):
            generate_outline(session, req)


def test_outline_book_belongs_to_other_corpus_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    """A book that exists but lives in a different corpus → OutlineError."""
    engine = db.get_engine()
    with Session(engine) as session:
        other = Corpus(name="Torts", course="Torts")
        session.add(other)
        session.commit()
        session.refresh(other)
        other_id = other.id

    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _outline_payload())
    )
    with Session(engine) as session:
        req = OutlineRequest(
            corpus_id=other_id,
            course="Torts",
            book_id=seeded_inputs["book_id"],
        )
        with pytest.raises(OutlineError, match="does not belong"):
            generate_outline(session, req)


def test_outline_402_on_budget_exceeded(
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
        _fake_factory(lambda _n: _outline_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = OutlineRequest(
            corpus_id=seeded_inputs["corpus_id"],
            course="Property",
            book_id=seeded_inputs["book_id"],
        )
        with pytest.raises(BudgetExceededError):
            generate_outline(session, req)
