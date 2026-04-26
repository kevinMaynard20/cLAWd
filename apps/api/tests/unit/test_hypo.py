"""Unit tests for features/hypo.py (spec §5.5 Path B).

Mocks generate() via the Anthropic client factory and asserts a HYPO
artifact with both the hypo prompt and the embedded rubric is persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlmodel import Session

from data import db
from data.models import ArtifactType, Corpus, Credentials
from features.hypo import HypoRequest, generate_hypo
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake client (mirrors test_case_brief.py / test_irac_grading.py)
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
            usage=_FakeUsage(input_tokens=2100, output_tokens=1300),
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
def seeded_corpus(temp_db: None) -> str:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        return corpus.id


def _hypo_payload() -> str:
    body = {
        "hypo": {
            "prompt": (
                "Owner A conveys Blackacre to B 'so long as used for a farm'. "
                "B sells to C, who builds a factory. Analyze the ensuing "
                "interests and the takings-clause implications."
            ),
            "role": "law clerk memo",
            "word_limit": 1200,
            "fact_pattern_summary": "Durational conveyance, factory built.",
        },
        "rubric": {
            "question_label": "takings_hypo_3",
            "fact_summary": "Durational conveyance, factory built.",
            "required_issues": [
                {
                    "id": "fsd-vs-fssel",
                    "label": "FSD vs FSSEL",
                    "weight": 0.5,
                    "why_required": "Durational language at the center of the hypo.",
                    "source_memo_excerpt": None,
                },
                {
                    "id": "regulatory-takings",
                    "label": "Regulatory takings framework",
                    "weight": 0.5,
                    "why_required": "Factory-vs-farming raises Penn Central.",
                    "source_memo_excerpt": None,
                },
            ],
            "required_rules": [
                {
                    "id": "fsd-rule",
                    "statement": "Durational language creates an FSD.",
                    "tied_to_issues": ["fsd-vs-fssel"],
                    "must_apply_to_facts": True,
                }
            ],
            "expected_counterarguments": [
                {
                    "id": "fssel-alt",
                    "summary": "Could be read as FSSEL to a third party.",
                    "why_expected": "Pollack 2023 deed-language trap.",
                }
            ],
            "anti_patterns": [
                {
                    "name": "hedge_without_resolution",
                    "pattern": "it depends on the jurisdiction",
                    "severity": "high",
                    "source": "Pollack profile",
                }
            ],
            "prompt_role": "law clerk memo",
            "word_limit": 1200,
            "sources": [],
        },
        "topics_covered": ["defeasible_fees", "regulatory_takings"],
        "sources": [],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hypo_generates_and_persists(
    seeded_corpus: str,
    fake_credentials: None,
) -> None:
    generate_module.set_anthropic_client_factory(_factory(lambda _n: _hypo_payload()))
    engine = db.get_engine()
    with Session(engine) as session:
        result = generate_hypo(
            session,
            HypoRequest(
                corpus_id=seeded_corpus,
                topics_to_cover=["defeasible_fees", "regulatory_takings"],
            ),
        )
    assert result.hypo_artifact.type == ArtifactType.HYPO
    assert result.cache_hit is False


def test_hypo_content_has_both_hypo_and_rubric(
    seeded_corpus: str,
    fake_credentials: None,
) -> None:
    generate_module.set_anthropic_client_factory(_factory(lambda _n: _hypo_payload()))
    engine = db.get_engine()
    with Session(engine) as session:
        result = generate_hypo(
            session,
            HypoRequest(
                corpus_id=seeded_corpus,
                topics_to_cover=["defeasible_fees"],
            ),
        )
    content = result.hypo_artifact.content
    assert "hypo" in content
    assert "rubric" in content
    assert content["hypo"]["prompt"].startswith("Owner A")
    assert len(content["rubric"]["required_issues"]) >= 1
    assert content["rubric"]["required_issues"][0]["id"]
