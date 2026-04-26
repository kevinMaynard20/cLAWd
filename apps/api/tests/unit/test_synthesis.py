"""Unit tests for features/synthesis.py (spec §5.8)."""

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
from features.synthesis import (
    SynthesisError,
    SynthesisRequest,
    generate_synthesis,
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
            usage=_FakeUsage(input_tokens=900, output_tokens=400),
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
    engine = db.get_engine()
    ids: dict[str, Any] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        brief1 = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "case_name": "Loretto",
                "year": 1982,
                "court": "U.S.",
                "holding": {"text": "Per-se."},
                "rule": {"text": "Permanent occupation."},
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
                "year": 1978,
                "court": "U.S.",
                "holding": {"text": "Balance."},
                "rule": {"text": "Penn Central balancing."},
            },
        )
        session.add(brief2)
        wrong = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.HYPO,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={"hypo": {"prompt": "x"}},
        )
        session.add(wrong)
        session.commit()
        session.refresh(brief1)
        session.refresh(brief2)
        session.refresh(wrong)
        ids["brief1_id"] = brief1.id
        ids["brief2_id"] = brief2.id
        ids["wrong_id"] = wrong.id

        profile = ProfessorProfile(
            corpus_id=corpus.id,
            professor_name="Pollack",
            course="Property",
            favored_framings=["follow the property"],
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        ids["profile_id"] = profile.id

    return ids


def _synthesis_payload() -> str:
    body = {
        "doctrinal_area": "Takings",
        "cases": [
            {"case_name": "Loretto", "year": 1982, "court": "U.S."},
            {"case_name": "Penn Central", "year": 1978, "court": "U.S."},
        ],
        "timeline": [
            {"year": 1978, "event": "PC test established", "case_name": "Penn Central"},
            {"year": 1982, "event": "Per-se carve-out", "case_name": "Loretto"},
        ],
        "categorical_rules": [
            {"rule": "Permanent physical = per-se", "from_case": "Loretto"}
        ],
        "balancing_tests": [
            {
                "test": "Penn Central",
                "factors": ["economic impact", "investment-backed", "character"],
                "from_case": "Penn Central",
            }
        ],
        "relationships": [
            {
                "description": "Loretto carved a per-se rule out of PC's balancing.",
                "case_a": "Loretto",
                "case_b": "Penn Central",
            },
            {
                "description": "PC remains the default for non-physical regulations.",
                "case_a": "Penn Central",
                "case_b": None,
            },
        ],
        "modern_synthesis": "Per-se rules sit alongside PC balancing.",
        "exam_framework": "Step 1 ask physical; Step 2 ask categorical; Step 3 PC.",
        "visual_diagram": None,
        "sources": [],
    }
    return json.dumps(body)


def test_synthesis_happy_path(seeded_inputs: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _synthesis_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = SynthesisRequest(
            corpus_id=seeded_inputs["corpus_id"],
            doctrinal_area="Takings",
            case_brief_artifact_ids=[
                seeded_inputs["brief1_id"],
                seeded_inputs["brief2_id"],
            ],
            professor_profile_id=seeded_inputs["profile_id"],
        )
        result = generate_synthesis(session, req)
    assert result.artifact.type is ArtifactType.SYNTHESIS
    assert result.cache_hit is False
    assert result.artifact.content["doctrinal_area"] == "Takings"
    assert len(result.artifact.content["timeline"]) == 2
    assert result.artifact.prompt_template.startswith("doctrinal_synthesis@")


def test_synthesis_cache_hit(seeded_inputs: dict[str, Any]) -> None:
    calls = {"n": 0}

    def payload(_n):
        calls["n"] += 1
        return _synthesis_payload()

    generate_module.set_anthropic_client_factory(_fake_factory(payload))
    engine = db.get_engine()
    with Session(engine) as session:
        req = SynthesisRequest(
            corpus_id=seeded_inputs["corpus_id"],
            doctrinal_area="Takings",
            case_brief_artifact_ids=[seeded_inputs["brief1_id"]],
        )
        first = generate_synthesis(session, req)
    with Session(engine) as session:
        req2 = SynthesisRequest(
            corpus_id=seeded_inputs["corpus_id"],
            doctrinal_area="Takings",
            case_brief_artifact_ids=[seeded_inputs["brief1_id"]],
        )
        second = generate_synthesis(session, req2)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls["n"] == 1
    assert second.artifact.id == first.artifact.id


def test_synthesis_missing_referenced_artifact_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _synthesis_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = SynthesisRequest(
            corpus_id=seeded_inputs["corpus_id"],
            doctrinal_area="Takings",
            case_brief_artifact_ids=["nope"],
        )
        with pytest.raises(SynthesisError, match="not found"):
            generate_synthesis(session, req)


def test_synthesis_wrong_type_raises(seeded_inputs: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _synthesis_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = SynthesisRequest(
            corpus_id=seeded_inputs["corpus_id"],
            doctrinal_area="Takings",
            case_brief_artifact_ids=[seeded_inputs["wrong_id"]],
        )
        with pytest.raises(SynthesisError, match="hypo"):
            generate_synthesis(session, req)


def test_synthesis_402_on_budget_exceeded(
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
        _fake_factory(lambda _n: _synthesis_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = SynthesisRequest(
            corpus_id=seeded_inputs["corpus_id"],
            doctrinal_area="X",
            case_brief_artifact_ids=[seeded_inputs["brief1_id"]],
        )
        with pytest.raises(BudgetExceededError):
            generate_synthesis(session, req)
