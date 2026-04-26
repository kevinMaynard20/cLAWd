"""Integration tests for the Phase 5 static-output feature routes:

POST /features/attack-sheet      (§5.9)
POST /features/synthesis         (§5.8)
POST /features/what-if           (§5.10)
POST /features/outline           (§5.11)
POST /features/mc-questions      (§5.12)

Same fake-Anthropic-client injection pattern as the rest of the integration
suite.
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
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    CreatedBy,
    Page,
    TocEntry,
)
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client
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
    def __init__(self, payload_fn):
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(
            content=[_FakeTextContent(text=self._payload_fn(len(self.calls)))],
            usage=_FakeUsage(input_tokens=1000, output_tokens=500),
        )


class _FakeClient:
    def __init__(self, payload_fn):
        self.messages = _FakeMessages(payload_fn)


def _fake_factory(payload_fn):
    def _factory(_api_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _factory


# ---------------------------------------------------------------------------
# Common fixtures
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


@pytest.fixture
def big_seed(temp_env: None) -> dict[str, Any]:
    """Seed the union of fixtures all five route tests need."""
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
            source_page_min=100,
            source_page_max=600,
        )
        session.add(book)
        session.commit()
        ids["book_id"] = book.id

        for idx, (lvl, title, p) in enumerate(
            [(1, "Estates", 100), (1, "Takings", 500)]
        ):
            session.add(
                TocEntry(
                    book_id=book.id,
                    level=lvl,
                    title=title,
                    source_page=p,
                    order_index=idx,
                )
            )

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

        block = Block(
            page_id=page_id,
            book_id=book.id,
            order_index=0,
            type=BlockType.NARRATIVE_TEXT,
            source_page=510,
            markdown="Penn Central is the default test for non-physical takings.",
            block_metadata={},
        )
        session.add(block)
        session.commit()
        session.refresh(block)
        ids["block_id"] = block.id

        # Two case briefs.
        b1 = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "case_name": "Loretto",
                "year": 1982,
                "court": "U.S.",
                "rule": {"text": "Per-se."},
                "holding": {"text": "Per-se."},
                "facts": [{"text": "cable on roof"}],
            },
        )
        b2 = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "case_name": "Penn Central",
                "year": 1978,
                "court": "U.S.",
                "rule": {"text": "Penn Central balancing."},
                "holding": {"text": "Balance."},
                "facts": [{"text": "grand central"}],
            },
        )
        session.add(b1)
        session.add(b2)
        session.commit()
        session.refresh(b1)
        session.refresh(b2)
        ids["brief1_id"] = b1.id
        ids["brief2_id"] = b2.id

    return ids


# ---------------------------------------------------------------------------
# Per-feature payload factories matching the schemas
# ---------------------------------------------------------------------------


def _attack_sheet_payload() -> str:
    return json.dumps(
        {
            "topic": "Takings",
            "issue_spotting_triggers": [
                {"trigger": "x1", "points_to": "y1"},
                {"trigger": "x2", "points_to": "y2"},
                {"trigger": "x3", "points_to": "y3"},
                {"trigger": "x4", "points_to": "y4"},
                {"trigger": "x5", "points_to": "y5"},
            ],
            "decision_tree": {"q": "physical?"},
            "controlling_cases": [
                {"case_name": "Loretto", "year": 1982, "one_line_holding": "per-se"},
                {"case_name": "Penn Central", "year": 1978, "one_line_holding": "PC"},
            ],
            "rules_with_elements": [
                {"rule": "Loretto", "elements": ["permanent", "physical"]}
            ],
            "exceptions": [],
            "majority_minority_splits": [],
            "common_traps": [],
            "one_line_summaries": ["Permanent = per-se", "Else PC", "Lucas all-out"],
            "sources": [],
        }
    )


def _synthesis_payload() -> str:
    return json.dumps(
        {
            "doctrinal_area": "Takings",
            "cases": [
                {"case_name": "Loretto", "year": 1982, "court": "U.S."},
                {"case_name": "Penn Central", "year": 1978, "court": "U.S."},
            ],
            "timeline": [
                {"year": 1978, "event": "PC", "case_name": "Penn Central"},
                {"year": 1982, "event": "Loretto carve-out", "case_name": "Loretto"},
            ],
            "categorical_rules": [
                {"rule": "Permanent = per-se", "from_case": "Loretto"}
            ],
            "balancing_tests": [
                {
                    "test": "Penn Central",
                    "factors": ["impact", "investment", "character"],
                    "from_case": "Penn Central",
                }
            ],
            "relationships": [
                {
                    "description": "Loretto carved per-se rule out of PC.",
                    "case_a": "Loretto",
                    "case_b": "Penn Central",
                },
                {
                    "description": "Lucas extended categorical rules.",
                    "case_a": None,
                    "case_b": None,
                },
            ],
            "modern_synthesis": "PC + per-se rules.",
            "exam_framework": "1) physical 2) categorical 3) PC.",
            "visual_diagram": None,
            "sources": [],
        }
    )


def _what_if_payload() -> str:
    return json.dumps(
        {
            "case_name": "Loretto",
            "variations": [
                {
                    "id": "v1",
                    "fact_changed": "Cable removable.",
                    "consequence": "PC balancing applies.",
                    "doctrinal_reason": "Permanence fails.",
                    "tests_understanding_of": "permanent vs temp",
                },
                {
                    "id": "v2",
                    "fact_changed": "City-owned cable.",
                    "consequence": "Direct govt action.",
                    "doctrinal_reason": "Public-use shifts.",
                    "tests_understanding_of": "actor identity",
                },
                {
                    "id": "v3",
                    "fact_changed": "EM signal only.",
                    "consequence": "No physical occupation.",
                    "doctrinal_reason": "No physical element.",
                    "tests_understanding_of": "physical vs intangible",
                },
            ],
            "sources": [],
        }
    )


def _outline_payload() -> str:
    return json.dumps(
        {
            "course": "Property",
            "topics": [
                {
                    "title": "Estates",
                    "level": 1,
                    "toc_source_page": 100,
                    "rule_statements": [],
                    "controlling_cases": [],
                    "policy_rationales": [],
                    "exam_traps": [],
                    "cross_references": [],
                },
                {
                    "title": "Takings",
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
    )


def _mc_payload() -> str:
    return json.dumps(
        {
            "topic": "Takings",
            "questions": [
                {
                    "id": "q1",
                    "stem": "Which test applies to non-physical regulations?",
                    "options": [
                        {"letter": "A", "text": "Loretto per-se"},
                        {"letter": "B", "text": "Penn Central"},
                        {"letter": "C", "text": "Strict scrutiny"},
                        {"letter": "D", "text": "Rational basis"},
                    ],
                    "correct_answer": "B",
                    "explanation": "Penn Central is the default.",
                    "distractor_explanations": {
                        "A": "Only physical occupations.",
                        "C": "Wrong tier.",
                        "D": "Wrong tier.",
                    },
                    "doctrine_tested": "Penn Central balancing",
                    "source_block_ids": [],
                }
            ],
            "sources": [],
        }
    )


# ---------------------------------------------------------------------------
# Tests — one happy-path + 1 error per route
# ---------------------------------------------------------------------------


def test_route_attack_sheet_happy_path(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _attack_sheet_payload())
    )
    r = client.post(
        "/features/attack-sheet",
        json={
            "corpus_id": big_seed["corpus_id"],
            "topic": "Takings",
            "case_brief_artifact_ids": [big_seed["brief1_id"], big_seed["brief2_id"]],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    assert body["artifact"]["type"] == "attack_sheet"
    assert body["artifact"]["content"]["topic"] == "Takings"


def test_route_attack_sheet_404(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _attack_sheet_payload())
    )
    r = client.post(
        "/features/attack-sheet",
        json={
            "corpus_id": big_seed["corpus_id"],
            "topic": "Takings",
            "case_brief_artifact_ids": ["does-not-exist"],
        },
    )
    assert r.status_code == 404


def test_route_synthesis_happy_path(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _synthesis_payload())
    )
    r = client.post(
        "/features/synthesis",
        json={
            "corpus_id": big_seed["corpus_id"],
            "doctrinal_area": "Takings",
            "case_brief_artifact_ids": [big_seed["brief1_id"], big_seed["brief2_id"]],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact"]["type"] == "synthesis"
    assert body["artifact"]["content"]["doctrinal_area"] == "Takings"
    assert body["cache_hit"] is False


def test_route_synthesis_404(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _synthesis_payload())
    )
    r = client.post(
        "/features/synthesis",
        json={
            "corpus_id": big_seed["corpus_id"],
            "doctrinal_area": "Takings",
            "case_brief_artifact_ids": ["nope"],
        },
    )
    assert r.status_code == 404


def test_route_what_if_happy_path(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _what_if_payload())
    )
    r = client.post(
        "/features/what-if",
        json={
            "corpus_id": big_seed["corpus_id"],
            "case_brief_artifact_id": big_seed["brief1_id"],
            "num_variations": 3,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact"]["type"] == "synthesis"
    # Sub-discriminator
    assert body["artifact"]["content"]["kind"] == "what_if_variations"
    assert len(body["artifact"]["content"]["variations"]) == 3


def test_route_what_if_404(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _what_if_payload())
    )
    r = client.post(
        "/features/what-if",
        json={
            "corpus_id": big_seed["corpus_id"],
            "case_brief_artifact_id": "does-not-exist",
        },
    )
    assert r.status_code == 404


def test_route_outline_happy_path(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _outline_payload())
    )
    r = client.post(
        "/features/outline",
        json={
            "corpus_id": big_seed["corpus_id"],
            "course": "Property",
            "book_id": big_seed["book_id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact"]["type"] == "outline"
    assert body["input_artifact_count"] == 2  # 2 briefs, 0 flashcards


def test_route_outline_404(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _outline_payload())
    )
    r = client.post(
        "/features/outline",
        json={
            "corpus_id": big_seed["corpus_id"],
            "course": "Property",
            "book_id": "missing",
        },
    )
    assert r.status_code == 404


def test_route_mc_questions_happy_path(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _mc_payload())
    )
    r = client.post(
        "/features/mc-questions",
        json={
            "corpus_id": big_seed["corpus_id"],
            "topic": "Takings",
            "book_id": big_seed["book_id"],
            "page_start": 510,
            "page_end": 510,
            "num_questions": 1,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact"]["type"] == "mc_question_set"
    assert body["artifact"]["content"]["topic"] == "Takings"


def test_route_mc_questions_404(client: TestClient, big_seed: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _mc_payload())
    )
    r = client.post(
        "/features/mc-questions",
        json={
            "corpus_id": big_seed["corpus_id"],
            "topic": "Takings",
            "book_id": "nope",
            "page_start": 510,
            "page_end": 510,
        },
    )
    assert r.status_code == 404


def test_route_503_on_generate_error(client: TestClient, big_seed: dict[str, Any]) -> None:
    """One representative route — invalid JSON exhausts retry budget → 503."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: "not-json")
    )
    r = client.post(
        "/features/synthesis",
        json={
            "corpus_id": big_seed["corpus_id"],
            "doctrinal_area": "Takings",
            "case_brief_artifact_ids": [big_seed["brief1_id"]],
        },
    )
    assert r.status_code == 503


def test_route_402_on_budget_exceeded(
    client: TestClient,
    big_seed: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Representative route — over-cap → 402."""
    from costs import tracker as cost_tracker

    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "0.01")
    cost_tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        feature="test_seed",
    )

    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _attack_sheet_payload())
    )
    r = client.post(
        "/features/attack-sheet",
        json={
            "corpus_id": big_seed["corpus_id"],
            "topic": "Takings",
            "case_brief_artifact_ids": [big_seed["brief1_id"]],
        },
    )
    assert r.status_code == 402
