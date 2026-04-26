"""Integration tests for POST /features/rubric-extract (spec §5.5 Path A).

Same fake-Anthropic-client injection pattern as ``test_case_brief.py``. We
seed ``PAST_EXAM`` + ``GRADER_MEMO`` artifacts directly so the integration
surface stays within Phase 3.4 scope and doesn't depend on the sibling
agent's ingest feature.
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
from data.models import Artifact, ArtifactType, Corpus, CreatedBy
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors anthropic.Anthropic surface used in generate)
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
        payload = self._payload_fn(len(self.calls))
        return _FakeResponse(
            content=[_FakeTextContent(text=payload)],
            usage=_FakeUsage(input_tokens=800, output_tokens=300),
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
    generate_module.set_anthropic_client_factory(None)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


@pytest.fixture
def seeded_inputs(temp_env: None) -> dict[str, str]:
    """Seed a corpus + PAST_EXAM + GRADER_MEMO the route can consume."""
    engine = db.get_engine()
    ids: dict[str, str] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        past_exam = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.PAST_EXAM,
            created_by=CreatedBy.USER,
            sources=[],
            content={"markdown": "Part II Q2. Adverse possession question."},
        )
        session.add(past_exam)
        session.commit()
        session.refresh(past_exam)
        ids["past_exam_id"] = past_exam.id

        grader_memo = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.GRADER_MEMO,
            created_by=CreatedBy.USER,
            sources=[],
            content={
                "markdown": (
                    "Strong answers discussed adverse possession elements."
                ),
            },
        )
        session.add(grader_memo)
        session.commit()
        session.refresh(grader_memo)
        ids["grader_memo_id"] = grader_memo.id

    return ids


# ---------------------------------------------------------------------------
# Valid rubric payload factory
# ---------------------------------------------------------------------------


def _rubric_payload() -> str:
    body = {
        "question_label": "Part II Q2",
        "fact_summary": "Adverse possession hypo.",
        "required_issues": [
            {
                "id": "ap-elements",
                "label": "AP elements",
                "weight": 1.0,
                "why_required": "Memo flags as core.",
                "source_memo_excerpt": "Strong answers discussed...",
            },
        ],
        "required_rules": [],
        "expected_counterarguments": [],
        "anti_patterns": [],
        "prompt_role": None,
        "word_limit": None,
        "sources": [],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_rubric_extract_happy_path(
    client: TestClient, seeded_inputs: dict[str, str]
) -> None:
    """200 with rubric artifact in body."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _rubric_payload())
    )

    r = client.post(
        "/features/rubric-extract",
        json={
            "corpus_id": seeded_inputs["corpus_id"],
            "past_exam_artifact_id": seeded_inputs["past_exam_id"],
            "grader_memo_artifact_id": seeded_inputs["grader_memo_id"],
            "question_label": "Part II Q2",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    art = body["rubric_artifact"]
    assert art["type"] == "rubric"
    assert art["corpus_id"] == seeded_inputs["corpus_id"]
    assert art["content"]["question_label"] == "Part II Q2"
    assert art["content"]["required_issues"][0]["id"] == "ap-elements"
    assert art["prompt_template"].startswith("rubric_from_memo@")


def test_route_rubric_extract_404_on_missing_exam(
    client: TestClient, seeded_inputs: dict[str, str]
) -> None:
    """Nonexistent past_exam_artifact_id → 404."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _rubric_payload())
    )
    r = client.post(
        "/features/rubric-extract",
        json={
            "corpus_id": seeded_inputs["corpus_id"],
            "past_exam_artifact_id": "does-not-exist",
            "grader_memo_artifact_id": seeded_inputs["grader_memo_id"],
            "question_label": "Part II Q2",
        },
    )
    assert r.status_code == 404
    assert "past_exam" in r.json()["detail"].lower()


def test_route_rubric_extract_400_on_missing_corpus_id(
    client: TestClient, seeded_inputs: dict[str, str]
) -> None:
    """Missing corpus_id → 400.

    Pydantic will reject an omitted required field with 422 (FastAPI's default
    validation-error status). We test both the empty-string case (our own 400
    gate in the route) and the omitted-field case to cover both shapes.
    """
    # Omitted — FastAPI returns 422.
    r = client.post(
        "/features/rubric-extract",
        json={
            "past_exam_artifact_id": seeded_inputs["past_exam_id"],
            "grader_memo_artifact_id": seeded_inputs["grader_memo_id"],
            "question_label": "Part II Q2",
        },
    )
    assert r.status_code == 422

    # Empty string — our own 400 gate inside the route.
    r2 = client.post(
        "/features/rubric-extract",
        json={
            "corpus_id": "",
            "past_exam_artifact_id": seeded_inputs["past_exam_id"],
            "grader_memo_artifact_id": seeded_inputs["grader_memo_id"],
            "question_label": "Part II Q2",
        },
    )
    assert r2.status_code == 400


def test_route_rubric_extract_503_on_generate_error(
    client: TestClient, seeded_inputs: dict[str, str]
) -> None:
    """GenerateError from the primitive → 503 at the route."""
    # Mock returns invalid JSON every attempt; after retry budget is exhausted
    # generate() raises GenerateError and the route maps to 503.
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: "not valid json at all")
    )
    r = client.post(
        "/features/rubric-extract",
        json={
            "corpus_id": seeded_inputs["corpus_id"],
            "past_exam_artifact_id": seeded_inputs["past_exam_id"],
            "grader_memo_artifact_id": seeded_inputs["grader_memo_id"],
            "question_label": "Part II Q2",
        },
    )
    assert r.status_code == 503
