"""Unit tests for features/attack_sheet.py (spec §5.9)."""

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
from features.attack_sheet import (
    AttackSheetError,
    AttackSheetRequest,
    generate_attack_sheet,
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
            usage=_FakeUsage(input_tokens=900, output_tokens=400),
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
def seeded_inputs(temp_env: None) -> dict[str, Any]:
    """Seed corpus + 2 CASE_BRIEF artifacts + 1 CASE_BRIEF wrong-type alias
    + 1 ProfessorProfile."""
    engine = db.get_engine()
    ids: dict[str, Any] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
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
                "case_name": "Loretto v. Teleprompter",
                "year": 1982,
                "holding": {"text": "Permanent physical occupation is per-se taking."},
                "rule": {"text": "Permanent physical invasion → per-se taking."},
                "reasoning": [{"text": "No matter the public benefit."}],
            },
        )
        session.add(brief1)
        brief2 = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "case_name": "Penn Central v. New York",
                "year": 1978,
                "holding": {"text": "Multi-factor balancing for regulatory takings."},
                "rule": {"text": "Penn Central balancing test."},
                "reasoning": [{"text": "Diminution + investment + character."}],
            },
        )
        session.add(brief2)

        # Wrong-type artifact for negative tests.
        rubric = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.RUBRIC,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={"question_label": "X"},
        )
        session.add(rubric)

        session.commit()
        session.refresh(brief1)
        session.refresh(brief2)
        session.refresh(rubric)
        ids["brief1_id"] = brief1.id
        ids["brief2_id"] = brief2.id
        ids["rubric_id"] = rubric.id

        profile = ProfessorProfile(
            corpus_id=corpus.id,
            professor_name="Pollack",
            course="Property",
            stable_traps=[
                {"name": "fsd_vs_fssel", "desc": "Confusing FSD and FSSEL"}
            ],
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        ids["profile_id"] = profile.id

    return ids


# ---------------------------------------------------------------------------
# Valid attack-sheet payload
# ---------------------------------------------------------------------------


def _attack_sheet_payload() -> str:
    body = {
        "topic": "Takings",
        "issue_spotting_triggers": [
            {"trigger": "permanent physical occupation", "points_to": "Loretto per-se"},
            {"trigger": "regulation cuts use", "points_to": "Penn Central balancing"},
            {"trigger": "denial of all economic use", "points_to": "Lucas categorical"},
            {"trigger": "exaction conditioned on permit", "points_to": "Nollan/Dolan"},
            {"trigger": "physical entry by govt", "points_to": "Loretto / per-se"},
        ],
        "decision_tree": {
            "root": "Is there a physical invasion?",
            "yes": {"per_se": "Loretto"},
            "no": {"check_balancing": "Penn Central"},
        },
        "controlling_cases": [
            {"case_name": "Loretto", "year": 1982, "one_line_holding": "Per-se taking."},
            {
                "case_name": "Penn Central",
                "year": 1978,
                "one_line_holding": "Balancing test.",
            },
        ],
        "rules_with_elements": [
            {
                "rule": "Loretto per-se",
                "elements": ["permanent", "physical", "occupation"],
            }
        ],
        "exceptions": ["temporary invasions"],
        "majority_minority_splits": [],
        "common_traps": ["confusing FSD and FSSEL"],
        "one_line_summaries": [
            "Permanent physical = per-se",
            "Else Penn Central balancing",
            "Lucas if denied all use",
        ],
        "sources": [],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_attack_sheet_happy_path(seeded_inputs: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _attack_sheet_payload())
    )

    engine = db.get_engine()
    with Session(engine) as session:
        req = AttackSheetRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            case_brief_artifact_ids=[
                seeded_inputs["brief1_id"],
                seeded_inputs["brief2_id"],
            ],
            professor_profile_id=seeded_inputs["profile_id"],
        )
        result = generate_attack_sheet(session, req)

    assert result.artifact.type is ArtifactType.ATTACK_SHEET
    assert result.cache_hit is False
    assert result.artifact.corpus_id == seeded_inputs["corpus_id"]
    assert result.artifact.content["topic"] == "Takings"
    assert len(result.artifact.content["issue_spotting_triggers"]) == 5
    assert result.artifact.prompt_template.startswith("attack_sheet@")


def test_attack_sheet_cache_hit(seeded_inputs: dict[str, Any]) -> None:
    calls = {"n": 0}

    def payload(_call_n: int) -> str:
        calls["n"] += 1
        return _attack_sheet_payload()

    generate_module.set_anthropic_client_factory(_fake_factory(payload))

    engine = db.get_engine()
    with Session(engine) as session:
        req = AttackSheetRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            case_brief_artifact_ids=[
                seeded_inputs["brief1_id"],
                seeded_inputs["brief2_id"],
            ],
        )
        first = generate_attack_sheet(session, req)
    with Session(engine) as session:
        req2 = AttackSheetRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            case_brief_artifact_ids=[
                seeded_inputs["brief1_id"],
                seeded_inputs["brief2_id"],
            ],
        )
        second = generate_attack_sheet(session, req2)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls["n"] == 1
    assert second.artifact.id == first.artifact.id


def test_attack_sheet_missing_referenced_artifact_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _attack_sheet_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = AttackSheetRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            case_brief_artifact_ids=["does-not-exist"],
        )
        with pytest.raises(AttackSheetError, match="not found"):
            generate_attack_sheet(session, req)


def test_attack_sheet_wrong_artifact_type_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _attack_sheet_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = AttackSheetRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            case_brief_artifact_ids=[seeded_inputs["rubric_id"]],
        )
        with pytest.raises(AttackSheetError, match="rubric"):
            generate_attack_sheet(session, req)


def test_attack_sheet_402_on_budget_exceeded(
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
        _fake_factory(lambda _n: _attack_sheet_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = AttackSheetRequest(
            corpus_id=seeded_inputs["corpus_id"],
            topic="Takings",
            case_brief_artifact_ids=[seeded_inputs["brief1_id"]],
        )
        with pytest.raises(BudgetExceededError):
            generate_attack_sheet(session, req)
