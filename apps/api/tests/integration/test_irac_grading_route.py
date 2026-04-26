"""End-to-end IRAC grading via the FastAPI route (spec §5.5, §6.1 L3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db
from data.models import Artifact, ArtifactType, Corpus, CreatedBy
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake client — same pattern as test_case_brief.py
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
            usage=_FakeUsage(input_tokens=1200, output_tokens=800),
        )


class _FakeClient:
    def __init__(self, payload_fn):
        self.messages = _FakeMessages(payload_fn)


def _factory(payload_fn):
    def _f(_api_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _f


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


@pytest.fixture
def seeded_rubric(temp_env: None) -> tuple[str, str]:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        rubric = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.RUBRIC,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "question_label": "Covenants Hypo",
                "fact_summary": "Subdivision covenant.",
                "required_issues": [
                    {
                        "id": "covenant-runs",
                        "label": "Covenant runs with the land?",
                        "weight": 1.0,
                        "why_required": "Core question.",
                        "source_memo_excerpt": None,
                    },
                ],
                "required_rules": [],
                "expected_counterarguments": [],
                "anti_patterns": [],
                "prompt_role": "law clerk memo",
                "word_limit": None,
                "sources": [],
            },
            prompt_template="rubric_from_memo@1.0.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0"),
            cache_key="",
        )
        session.add(rubric)
        session.commit()
        session.refresh(rubric)
        return corpus.id, rubric.id


def _grade_payload(rubric_id: str, letter_grade: str = "B") -> str:
    body = {
        "overall_score": 82.0,
        "letter_grade": letter_grade,
        "per_rubric_scores": [
            {
                "rubric_item_id": "covenant-runs",
                "rubric_item_kind": "required_issue",
                "points_earned": 0.8,
                "points_possible": 1.0,
                "justification": "Good IRAC structure.",
            }
        ],
        "pattern_flags": [],
        "strengths": ["Organization."],
        "gaps": ["Counterargument undeveloped."],
        "what_would_have_earned_more_points": "Apply elements to specific facts.",
        "sample_paragraph": "A rewritten A-level paragraph ...",
        "rubric_id": rubric_id,
        "sources": [rubric_id],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_irac_grade_happy_path_via_route(
    client: TestClient,
    seeded_rubric: tuple[str, str],
) -> None:
    corpus_id, rubric_id = seeded_rubric
    generate_module.set_anthropic_client_factory(_factory(lambda _n: _grade_payload(rubric_id)))

    r = client.post(
        "/features/irac-grade",
        json={
            "corpus_id": corpus_id,
            "rubric_artifact_id": rubric_id,
            "answer_markdown": (
                "The covenant runs with the land because the grantor intended "
                "the restriction and there is horizontal privity."
            ),
            "question_label": "Covenants Hypo",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    assert body["grade_artifact"]["type"] == "grade"
    assert body["grade_artifact"]["content"]["letter_grade"] == "B"
    assert body["rubric_coverage_passed"] is True


def test_irac_grade_surfaces_detected_patterns_via_route(
    client: TestClient,
    seeded_rubric: tuple[str, str],
) -> None:
    corpus_id, rubric_id = seeded_rubric
    generate_module.set_anthropic_client_factory(_factory(lambda _n: _grade_payload(rubric_id, letter_grade="B-")))

    r = client.post(
        "/features/irac-grade",
        json={
            "corpus_id": corpus_id,
            "rubric_artifact_id": rubric_id,
            "answer_markdown": (
                "It depends on the jurisdiction whether the covenant is "
                "enforceable. Clearly, the intent is there."
            ),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    names = {p["name"] for p in body["detected_patterns"]}
    assert "hedge_without_resolution" in names
    assert "clearly_as_argument_substitution" in names


def test_irac_grade_404_on_missing_rubric(
    client: TestClient,
    seeded_rubric: tuple[str, str],
) -> None:
    corpus_id, _ = seeded_rubric
    generate_module.set_anthropic_client_factory(
        _factory(lambda _n: _grade_payload("bogus-rubric-id"))
    )
    r = client.post(
        "/features/irac-grade",
        json={
            "corpus_id": corpus_id,
            "rubric_artifact_id": "nonexistent-rubric-id",
            "answer_markdown": "Any answer.",
        },
    )
    assert r.status_code == 404


def test_irac_grade_402_on_budget_exceeded(
    client: TestClient,
    seeded_rubric: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_id, rubric_id = seeded_rubric
    from costs import tracker

    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "0.01")
    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        feature="test_seed",
    )
    generate_module.set_anthropic_client_factory(_factory(lambda _n: _grade_payload(rubric_id)))

    r = client.post(
        "/features/irac-grade",
        json={
            "corpus_id": corpus_id,
            "rubric_artifact_id": rubric_id,
            "answer_markdown": "Over budget.",
        },
    )
    assert r.status_code == 402
