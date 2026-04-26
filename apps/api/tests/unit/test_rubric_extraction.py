"""Unit tests for features/rubric_extraction.py (spec §5.5 Path A step 2).

Mirrors the fake-Anthropic-client pattern from
``tests/integration/test_case_brief.py`` so tests run deterministically without
a real API key. The mock returns a rubric JSON payload that matches
``packages/schemas/rubric.json``; we seed the PastExam / GraderMemo /
ProfessorProfile rows directly (i.e., we don't depend on the sibling agent's
ingest feature) so we stay within Phase 3.4 Path A scope.
"""

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
    Corpus,
    CreatedBy,
    ProfessorProfile,
)
from features.rubric_extraction import (
    RubricExtractionError,
    RubricExtractionRequest,
    extract_rubric_from_memo,
)
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client — mirrors the surface used in generate()
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
    """Fresh DB + keyring fallback + cleared cost cap for each test.

    Mirrors test_case_brief's temp_env so the same mock-injection story works
    here: the keyring_backend's encrypted-file fallback is forced on, we write
    a fake key into it, and we restore the client factory on teardown.
    """
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
def seeded_inputs(temp_env: None) -> dict[str, str]:
    """Seed the corpus + past-exam + grader-memo artifacts the feature needs.

    Returns a dict of ids so tests can pick what they need. We go direct to
    the DB (no ingest-feature dependency — see module docstring).
    """
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
            content={
                "markdown": (
                    "Part II Q2. Alice and Bob own adjoining parcels. "
                    "Alice builds a fence that encroaches six inches onto "
                    "Bob's land. Twelve years pass in silence. Analyze."
                ),
            },
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
                    "Part II Q2. Strong answers discussed adverse possession "
                    "elements (open, notorious, continuous, hostile, "
                    "exclusive) and the statutory period in New York. "
                    "Students should have argued in the alternative that "
                    "mere encroachment without hostility might fail. Many "
                    "students hedged with 'it depends on the jurisdiction' "
                    "— that's a non-answer."
                ),
            },
        )
        session.add(grader_memo)
        session.commit()
        session.refresh(grader_memo)
        ids["grader_memo_id"] = grader_memo.id

        # Also seed a CASE_BRIEF artifact that tests can use to prove
        # wrong-type errors.
        case_brief = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={"case_name": "Unrelated v. Test"},
        )
        session.add(case_brief)
        session.commit()
        session.refresh(case_brief)
        ids["case_brief_id"] = case_brief.id

        profile = ProfessorProfile(
            corpus_id=corpus.id,
            professor_name="Pollack",
            course="Property",
            pet_peeves=[
                {
                    "name": "hedging",
                    "pattern": "uses 'it depends' without resolution",
                    "severity": "high",
                    "source": "2023 memo",
                },
            ],
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        ids["profile_id"] = profile.id

    return ids


# ---------------------------------------------------------------------------
# Valid rubric payload factory
# ---------------------------------------------------------------------------


def _rubric_payload(question_label: str = "Part II Q2") -> str:
    """Return a JSON string that validates against schemas/rubric.json."""
    body = {
        "question_label": question_label,
        "fact_summary": "Encroaching fence + 12-year silence — adverse possession.",
        "required_issues": [
            {
                "id": "ap-elements",
                "label": "Adverse possession elements",
                "weight": 0.6,
                "why_required": "Memo flags this as the core doctrinal test.",
                "source_memo_excerpt": (
                    "Strong answers discussed adverse possession elements..."
                ),
            },
            {
                "id": "statutory-period",
                "label": "NY statutory period",
                "weight": 0.4,
                "why_required": "Memo references the NY period explicitly.",
                "source_memo_excerpt": "the statutory period in New York",
            },
        ],
        "required_rules": [
            {
                "id": "ap-hostility",
                "statement": (
                    "Possession must be hostile — i.e., without the true "
                    "owner's permission."
                ),
                "tied_to_issues": ["ap-elements"],
                "must_apply_to_facts": True,
            },
        ],
        "expected_counterarguments": [
            {
                "id": "no-hostility",
                "summary": "Mere encroachment may lack hostility element.",
                "why_expected": "Memo flags 'argue in the alternative'.",
            },
        ],
        "anti_patterns": [
            {
                "name": "hedging",
                "pattern": "uses 'it depends' without resolution",
                "severity": "high",
                "source": "2023 memo",
            },
        ],
        "prompt_role": None,
        "word_limit": None,
        "sources": [],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_extract_rubric_happy_path(seeded_inputs: dict[str, str]) -> None:
    """Seed past_exam + grader_memo; mock generate; assert a RUBRIC artifact
    is persisted with the mocked content."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _rubric_payload())
    )

    engine = db.get_engine()
    with Session(engine) as session:
        req = RubricExtractionRequest(
            corpus_id=seeded_inputs["corpus_id"],
            past_exam_artifact_id=seeded_inputs["past_exam_id"],
            grader_memo_artifact_id=seeded_inputs["grader_memo_id"],
            question_label="Part II Q2",
            professor_profile_id=seeded_inputs["profile_id"],
        )
        result = extract_rubric_from_memo(session, req)

    artifact = result.rubric_artifact
    assert artifact.type is ArtifactType.RUBRIC
    assert result.cache_hit is False
    assert artifact.corpus_id == seeded_inputs["corpus_id"]
    # The mocked content round-tripped into artifact.content.
    assert artifact.content["question_label"] == "Part II Q2"
    assert len(artifact.content["required_issues"]) == 2
    assert artifact.content["required_issues"][0]["id"] == "ap-elements"
    # The template identifier was recorded.
    assert artifact.prompt_template.startswith("rubric_from_memo@")
    # Caching works — second call hits cache, no second LLM call.
    engine = db.get_engine()
    with Session(engine) as session:
        req2 = RubricExtractionRequest(
            corpus_id=seeded_inputs["corpus_id"],
            past_exam_artifact_id=seeded_inputs["past_exam_id"],
            grader_memo_artifact_id=seeded_inputs["grader_memo_id"],
            question_label="Part II Q2",
            professor_profile_id=seeded_inputs["profile_id"],
        )
        second = extract_rubric_from_memo(session, req2)
    assert second.cache_hit is True
    assert second.rubric_artifact.id == artifact.id


def test_extract_rubric_missing_past_exam_raises(
    seeded_inputs: dict[str, str],
) -> None:
    """Nonexistent past_exam_artifact_id → RubricExtractionError (→ 404)."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _rubric_payload())
    )

    engine = db.get_engine()
    with Session(engine) as session:
        req = RubricExtractionRequest(
            corpus_id=seeded_inputs["corpus_id"],
            past_exam_artifact_id="does-not-exist",
            grader_memo_artifact_id=seeded_inputs["grader_memo_id"],
            question_label="Part II Q2",
        )
        with pytest.raises(RubricExtractionError, match="past_exam.*not found"):
            extract_rubric_from_memo(session, req)


def test_extract_rubric_wrong_artifact_type_raises(
    seeded_inputs: dict[str, str],
) -> None:
    """Pass a CASE_BRIEF id in the past_exam slot → RubricExtractionError."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _rubric_payload())
    )

    engine = db.get_engine()
    with Session(engine) as session:
        req = RubricExtractionRequest(
            corpus_id=seeded_inputs["corpus_id"],
            past_exam_artifact_id=seeded_inputs["case_brief_id"],
            grader_memo_artifact_id=seeded_inputs["grader_memo_id"],
            question_label="Part II Q2",
        )
        with pytest.raises(RubricExtractionError, match="past_exam"):
            extract_rubric_from_memo(session, req)


def test_extract_rubric_without_professor_profile(
    seeded_inputs: dict[str, str],
) -> None:
    """professor_profile_id=None must work and render template with
    professor_profile=null (the {{#if}} block collapses)."""
    # Capture the rendered prompt so we can assert on it.
    rendered_prompts: list[str] = []

    @dataclass
    class _CaptureMessages:
        calls: list[dict[str, Any]]

        def create(self, **kwargs):
            self.calls.append(kwargs)
            messages = kwargs.get("messages") or []
            if messages:
                rendered_prompts.append(messages[-1].get("content", ""))
            return _FakeResponse(
                content=[_FakeTextContent(text=_rubric_payload())],
                usage=_FakeUsage(input_tokens=800, output_tokens=300),
            )

    class _CaptureClient:
        def __init__(self):
            self.messages = _CaptureMessages(calls=[])

    def _capture_factory(_api_key: str) -> _CaptureClient:
        return _CaptureClient()

    generate_module.set_anthropic_client_factory(_capture_factory)

    engine = db.get_engine()
    with Session(engine) as session:
        req = RubricExtractionRequest(
            corpus_id=seeded_inputs["corpus_id"],
            past_exam_artifact_id=seeded_inputs["past_exam_id"],
            grader_memo_artifact_id=seeded_inputs["grader_memo_id"],
            question_label="Part II Q2",
            professor_profile_id=None,
        )
        result = extract_rubric_from_memo(session, req)

    assert result.rubric_artifact.type is ArtifactType.RUBRIC
    # The {{#if professor_profile}} block in the template collapses when the
    # value is falsy — so the rendered prompt must NOT contain the Professor
    # profile section header that's inside that block.
    assert rendered_prompts, "expected the client to have been called"
    assert (
        "## Professor profile" not in rendered_prompts[0]
    ), "professor_profile section should be absent when profile_id is None"


def test_extract_rubric_missing_professor_profile_id_raises(
    seeded_inputs: dict[str, str],
) -> None:
    """If a professor_profile_id is supplied but doesn't resolve, we raise
    RubricExtractionError (→ 404) rather than silently dropping it."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _rubric_payload())
    )

    engine = db.get_engine()
    with Session(engine) as session:
        req = RubricExtractionRequest(
            corpus_id=seeded_inputs["corpus_id"],
            past_exam_artifact_id=seeded_inputs["past_exam_id"],
            grader_memo_artifact_id=seeded_inputs["grader_memo_id"],
            question_label="Part II Q2",
            professor_profile_id="does-not-exist",
        )
        with pytest.raises(RubricExtractionError, match="ProfessorProfile"):
            extract_rubric_from_memo(session, req)


def test_extract_rubric_missing_grader_memo_raises(
    seeded_inputs: dict[str, str],
) -> None:
    """Nonexistent grader_memo_artifact_id → RubricExtractionError."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _rubric_payload())
    )

    engine = db.get_engine()
    with Session(engine) as session:
        req = RubricExtractionRequest(
            corpus_id=seeded_inputs["corpus_id"],
            past_exam_artifact_id=seeded_inputs["past_exam_id"],
            grader_memo_artifact_id="does-not-exist",
            question_label="Part II Q2",
        )
        with pytest.raises(RubricExtractionError, match="grader_memo.*not found"):
            extract_rubric_from_memo(session, req)
