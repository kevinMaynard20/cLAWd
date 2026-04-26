"""Unit tests for features/what_if.py (spec §5.10)."""

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
)
from features.what_if import (
    WHAT_IF_KIND,
    WhatIfError,
    WhatIfRequest,
    generate_what_if_variations,
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
            usage=_FakeUsage(input_tokens=600, output_tokens=300),
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

        brief = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={
                "case_name": "Loretto v. Teleprompter",
                "facts": [{"text": "Cable on roof."}],
                "holding": {"text": "Per-se taking."},
                "rule": {"text": "Permanent physical occupation."},
            },
        )
        session.add(brief)

        wrong = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.RUBRIC,
            created_by=CreatedBy.SYSTEM,
            sources=[],
            content={"question_label": "x"},
        )
        session.add(wrong)
        session.commit()
        session.refresh(brief)
        session.refresh(wrong)
        ids["brief_id"] = brief.id
        ids["wrong_id"] = wrong.id

    return ids


def _what_if_payload() -> str:
    body = {
        "case_name": "Loretto v. Teleprompter",
        "variations": [
            {
                "id": "v1",
                "fact_changed": "Cable removable on demand.",
                "consequence": "Outcome flips — temp invasion is PC balancing.",
                "doctrinal_reason": "Permanence element fails.",
                "tests_understanding_of": "permanent vs temporary invasion",
            },
            {
                "id": "v2",
                "fact_changed": "Cable owned by city.",
                "consequence": "Government action triggers Fifth Amendment differently.",
                "doctrinal_reason": "Public-use analysis applies directly.",
                "tests_understanding_of": "private vs public actor",
            },
            {
                "id": "v3",
                "fact_changed": "No physical contact, only EM signal.",
                "consequence": "No per-se rule; PC balancing or nuisance.",
                "doctrinal_reason": "No physical occupation element.",
                "tests_understanding_of": "physical vs intangible invasion",
            },
        ],
        "sources": [],
    }
    return json.dumps(body)


def test_what_if_happy_path(seeded_inputs: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _what_if_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = WhatIfRequest(
            corpus_id=seeded_inputs["corpus_id"],
            case_brief_artifact_id=seeded_inputs["brief_id"],
            num_variations=3,
        )
        result = generate_what_if_variations(session, req)
    assert result.artifact.type is ArtifactType.SYNTHESIS
    assert result.cache_hit is False
    # The sub-discriminator must be stamped onto content.
    assert result.artifact.content.get("kind") == WHAT_IF_KIND
    assert result.artifact.content["case_name"].startswith("Loretto")
    assert len(result.artifact.content["variations"]) == 3
    assert result.artifact.parent_artifact_id == seeded_inputs["brief_id"]
    assert result.artifact.prompt_template.startswith("what_if_variations@")


def test_what_if_cache_hit(seeded_inputs: dict[str, Any]) -> None:
    calls = {"n": 0}

    def payload(_n):
        calls["n"] += 1
        return _what_if_payload()

    generate_module.set_anthropic_client_factory(_fake_factory(payload))
    engine = db.get_engine()
    with Session(engine) as session:
        req = WhatIfRequest(
            corpus_id=seeded_inputs["corpus_id"],
            case_brief_artifact_id=seeded_inputs["brief_id"],
            num_variations=3,
        )
        first = generate_what_if_variations(session, req)
    with Session(engine) as session:
        req2 = WhatIfRequest(
            corpus_id=seeded_inputs["corpus_id"],
            case_brief_artifact_id=seeded_inputs["brief_id"],
            num_variations=3,
        )
        second = generate_what_if_variations(session, req2)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls["n"] == 1
    assert second.artifact.id == first.artifact.id
    # Cache-hit artifact still carries the discriminator.
    assert second.artifact.content.get("kind") == WHAT_IF_KIND


def test_what_if_missing_referenced_artifact_raises(
    seeded_inputs: dict[str, Any],
) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _what_if_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = WhatIfRequest(
            corpus_id=seeded_inputs["corpus_id"],
            case_brief_artifact_id="nope",
        )
        with pytest.raises(WhatIfError, match="not found"):
            generate_what_if_variations(session, req)


def test_what_if_wrong_type_raises(seeded_inputs: dict[str, Any]) -> None:
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _what_if_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = WhatIfRequest(
            corpus_id=seeded_inputs["corpus_id"],
            case_brief_artifact_id=seeded_inputs["wrong_id"],
        )
        with pytest.raises(WhatIfError, match="rubric"):
            generate_what_if_variations(session, req)


def test_what_if_402_on_budget_exceeded(
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
        _fake_factory(lambda _n: _what_if_payload())
    )
    engine = db.get_engine()
    with Session(engine) as session:
        req = WhatIfRequest(
            corpus_id=seeded_inputs["corpus_id"],
            case_brief_artifact_id=seeded_inputs["brief_id"],
        )
        with pytest.raises(BudgetExceededError):
            generate_what_if_variations(session, req)
