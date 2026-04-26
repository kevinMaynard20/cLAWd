"""Unit tests for features/irac_grading.py (spec §5.5).

Mocks the Anthropic client via `set_anthropic_client_factory`. Asserts:

- happy path persists a Grade artifact linked to a PRACTICE_ANSWER,
- second call with identical inputs returns the cached Grade,
- verifier stub is tolerated (NotImplementedError → coverage_passed=True),
- answer with multiple Pollack anti-patterns surfaces ALL detected patterns,
- budget-exceeded raises BudgetExceededError.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlmodel import Session, select

from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Corpus,
    CreatedBy,
    Credentials,
)
from features.irac_grading import (
    IracGradeRequest,
    grade_irac_answer,
)
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client (same pattern as test_case_brief.py)
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
            usage=_FakeUsage(input_tokens=1800, output_tokens=900),
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
        lambda: Credentials(anthropic_api_key=SecretStr("sk-ant-test-FAKEKEY-1234567890-LAST")),
    )


@pytest.fixture
def seeded_rubric(temp_db: None) -> tuple[str, str]:
    """Persist a Corpus and a RUBRIC artifact; return (corpus_id, rubric_id)."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        rubric_content: dict[str, Any] = {
            "question_label": "Covenants Hypo",
            "fact_summary": "A subdivision restriction is challenged.",
            "required_issues": [
                {
                    "id": "covenant-runs",
                    "label": "Does the covenant run with the land?",
                    "weight": 0.5,
                    "why_required": "Core doctrinal question in the hypo.",
                    "source_memo_excerpt": None,
                },
                {
                    "id": "changed-conditions",
                    "label": "Changed-conditions defense?",
                    "weight": 0.5,
                    "why_required": "Prompt raises a neighborhood-change fact.",
                    "source_memo_excerpt": None,
                },
            ],
            "required_rules": [
                {
                    "id": "covenant-rule",
                    "statement": (
                        "A real covenant runs with the land when intent, privity, "
                        "and touch-and-concern are satisfied."
                    ),
                    "tied_to_issues": ["covenant-runs"],
                    "must_apply_to_facts": True,
                }
            ],
            "expected_counterarguments": [
                {
                    "id": "defense-changed-conditions",
                    "summary": "The neighborhood has changed materially.",
                    "why_expected": "Fact pattern raises it.",
                }
            ],
            "anti_patterns": [
                {
                    "name": "hedge_without_resolution",
                    "pattern": "it depends on the jurisdiction",
                    "severity": "high",
                    "source": "Pollack 2023 memo p.2",
                },
            ],
            "prompt_role": "law clerk memo",
            "word_limit": None,
            "sources": [],
        }
        rubric = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.RUBRIC,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content=rubric_content,
            prompt_template="rubric_from_memo@1.0.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0"),
            cache_key="",
        )
        session.add(rubric)
        session.commit()
        session.refresh(rubric)
        return corpus.id, rubric.id


def _grade_payload(
    rubric_id: str,
    *,
    letter_grade: str = "B",
    overall_score: float = 83.0,
) -> str:
    body = {
        "overall_score": overall_score,
        "letter_grade": letter_grade,
        "per_rubric_scores": [
            {
                "rubric_item_id": "covenant-runs",
                "rubric_item_kind": "required_issue",
                "points_earned": 0.4,
                "points_possible": 0.5,
                "justification": "Identified the issue but application was thin.",
            },
            {
                "rubric_item_id": "changed-conditions",
                "rubric_item_kind": "required_issue",
                "points_earned": 0.3,
                "points_possible": 0.5,
                "justification": "Missed the 'internal AND external' requirement.",
            },
            {
                "rubric_item_id": "covenant-rule",
                "rubric_item_kind": "required_rule",
                "points_earned": 1.0,
                "points_possible": 1.0,
                "justification": "Rule stated accurately.",
            },
            {
                "rubric_item_id": "defense-changed-conditions",
                "rubric_item_kind": "expected_counterargument",
                "points_earned": 0.5,
                "points_possible": 1.0,
                "justification": "Acknowledged but didn't develop.",
            },
        ],
        "pattern_flags": [],
        "strengths": ["Clear organization."],
        "gaps": ["Did not develop changed-conditions counterargument."],
        "what_would_have_earned_more_points": (
            "Apply the privity elements to the Sarah-Grantor chain explicitly."
        ),
        "sample_paragraph": "A rewritten paragraph that applies the elements ...",
        "rubric_id": rubric_id,
        "sources": [rubric_id],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_irac_grade_happy_path(
    seeded_rubric: tuple[str, str],
    fake_credentials: None,
) -> None:
    corpus_id, rubric_id = seeded_rubric
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _grade_payload(rubric_id))
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = grade_irac_answer(
            session,
            IracGradeRequest(
                corpus_id=corpus_id,
                rubric_artifact_id=rubric_id,
                answer_markdown=(
                    "The covenant runs with the land because intent, privity, "
                    "and touch-and-concern are satisfied. Here, the grantor "
                    "expressed intent in the deed."
                ),
                question_label="Covenants Hypo",
            ),
        )

    assert result.grade_artifact.type == ArtifactType.GRADE
    assert result.grade_artifact.content["letter_grade"] == "B"
    assert result.grade_artifact.content["rubric_id"] == rubric_id
    # PRACTICE_ANSWER was persisted and linked via parent_artifact_id.
    assert result.grade_artifact.parent_artifact_id is not None

    # Verify the PRACTICE_ANSWER row exists and matches.
    with Session(engine) as session:
        pa = session.exec(
            select(Artifact).where(
                Artifact.id == result.grade_artifact.parent_artifact_id
            )
        ).one()
        assert pa.type == ArtifactType.PRACTICE_ANSWER
        assert pa.content["word_count"] > 0
        assert "covenant runs" in pa.content["markdown"]

    # verify(rubric_coverage) is stubbed — the feature tolerates it.
    assert result.rubric_coverage_passed is True
    assert result.cache_hit is False


def test_irac_grade_cache_hit(
    seeded_rubric: tuple[str, str],
    fake_credentials: None,
) -> None:
    corpus_id, rubric_id = seeded_rubric
    calls = {"n": 0}

    def _payload(_call_n: int) -> str:
        calls["n"] += 1
        return _grade_payload(rubric_id)

    generate_module.set_anthropic_client_factory(_fake_factory(_payload))

    engine = db.get_engine()
    answer = "Same answer for cache key stability."
    with Session(engine) as session:
        r1 = grade_irac_answer(
            session,
            IracGradeRequest(
                corpus_id=corpus_id,
                rubric_artifact_id=rubric_id,
                answer_markdown=answer,
            ),
        )
    with Session(engine) as session:
        r2 = grade_irac_answer(
            session,
            IracGradeRequest(
                corpus_id=corpus_id,
                rubric_artifact_id=rubric_id,
                answer_markdown=answer,
            ),
        )
    assert r1.cache_hit is False
    assert r2.cache_hit is True
    assert calls["n"] == 1  # Anthropic only called once


def test_irac_grade_tolerates_verifier_stub(
    seeded_rubric: tuple[str, str],
    fake_credentials: None,
) -> None:
    """The rubric_coverage verifier stub raises NotImplementedError — the
    feature swallows it and returns rubric_coverage_passed=True with a log
    note, per the spec's tolerant design for Phase 3."""
    corpus_id, rubric_id = seeded_rubric
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _grade_payload(rubric_id))
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = grade_irac_answer(
            session,
            IracGradeRequest(
                corpus_id=corpus_id,
                rubric_artifact_id=rubric_id,
                answer_markdown="Answer under the verifier stub.",
            ),
        )
    # Stubbed verifier should not populate warnings or flip the flag.
    assert result.rubric_coverage_passed is True
    assert result.rubric_coverage_warnings == []


def test_irac_grade_pollack_antipatterns(
    seeded_rubric: tuple[str, str],
    fake_credentials: None,
) -> None:
    """§6.1 L2: answer contains 'clearly', 'it depends on the jurisdiction',
    and a mismatched future-interest. Assert all three detected patterns
    surface AND the letter grade stays in the B-range set (our mock pins it
    to a deliberately-B-range score)."""
    corpus_id, rubric_id = seeded_rubric
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _grade_payload(rubric_id, letter_grade="B-", overall_score=80.0))
    )

    answer = (
        "It depends on the jurisdiction whether the covenant is enforceable. "
        "Clearly, the grantor intended the restriction. "
        "The grantee holds a contingent remainder and a vested remainder "
        "subject to open."
    )
    engine = db.get_engine()
    with Session(engine) as session:
        result = grade_irac_answer(
            session,
            IracGradeRequest(
                corpus_id=corpus_id,
                rubric_artifact_id=rubric_id,
                answer_markdown=answer,
            ),
        )

    names = {p.name for p in result.detected_patterns}
    assert "hedge_without_resolution" in names
    assert "clearly_as_argument_substitution" in names
    assert "mismatched_future_interests" in names

    letter = result.grade_artifact.content["letter_grade"]
    assert letter in {"B-", "C+", "C", "C-", "D", "F"}


def test_irac_grade_402_on_budget_exceeded(
    seeded_rubric: tuple[str, str],
    fake_credentials: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from costs import tracker
    from costs.tracker import BudgetExceededError

    corpus_id, rubric_id = seeded_rubric
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "0.01")
    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        feature="test_seed",
    )

    engine = db.get_engine()
    with Session(engine) as session:
        with pytest.raises(BudgetExceededError):
            grade_irac_answer(
                session,
                IracGradeRequest(
                    corpus_id=corpus_id,
                    rubric_artifact_id=rubric_id,
                    answer_markdown="Over budget.",
                ),
            )
