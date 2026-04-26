"""Unit tests for `features.professor_profile` (spec §5.13).

Uses `set_anthropic_client_factory` to inject a deterministic fake client —
same pattern as `test_case_brief.py`. Tests cover:

- Build → creates both ProfessorProfile row and Artifact row.
- Second build → upsert, not duplicate row.
- Schema-validated edits (good + bad).
- `updated_at` bumps on update.
- Load by (corpus, name) and by corpus only.
- Pollack seed is idempotent and matches Appendix A shape.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from data import db
from data.models import Artifact, ArtifactType, Corpus, CreatedBy, ProfessorProfile
from features.past_exam_ingest import PastExamIngestRequest, ingest_past_exam
from features.professor_profile import (
    APPENDIX_A_POLLACK_PROFILE,
    ProfileBuildRequest,
    build_profile_from_memos,
    load_profile_for_corpus,
    seed_pollack_profile,
    update_profile,
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
        payload = self._payload_fn(len(self.calls))
        return _FakeResponse(
            content=[_FakeTextContent(text=payload)],
            usage=_FakeUsage(input_tokens=2000, output_tokens=1200),
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


def _seed_corpus() -> str:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property — Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        return corpus.id


def _ingest_memos(corpus_id: str) -> list[str]:
    """Create one past_exam + one grader_memo and return their ids."""
    engine = db.get_engine()
    with Session(engine) as session:
        result = ingest_past_exam(
            session,
            PastExamIngestRequest(
                corpus_id=corpus_id,
                exam_markdown="# 2023 Property Exam\n\nPart II: ...",
                grader_memo_markdown=(
                    "Common errors: hedging, using 'clearly', misnaming future interests."
                ),
                source_paths=["/uploads/pollack_2023_exam.md"],
                year=2023,
                professor_name="Pollack",
            ),
        )
    return [result.past_exam_artifact_id, result.grader_memo_artifact_id or ""]


# ---------------------------------------------------------------------------
# Mock extraction payload
# ---------------------------------------------------------------------------


def _mock_profile_payload(**overrides: Any) -> str:
    """Return a valid JSON payload that matches schemas/professor_profile.json.

    Tests can override the handful of fields they care about (pet peeve names,
    school, etc.) without hand-building the full dict."""
    payload: dict[str, Any] = {
        "professor_name": overrides.get("professor_name", "Pollack"),
        "course": overrides.get("course", "Property"),
        "school": overrides.get("school", "Benjamin N. Cardozo School of Law"),
        "exam_format": {
            "duration_hours": 5.0,
            "word_limit": 4000,
            "open_book": False,
            "structure": [
                {"part": "I", "weight": 10, "type": "multiple_choice", "count": 10},
                {"part": "II-IV", "weight": 30, "type": "issue_spotter_essay"},
            ],
            "prompt_conventions": [
                "Ends with ambiguity closer.",
            ],
        },
        "pet_peeves": overrides.get(
            "pet_peeves",
            [
                {
                    "name": "hedge_without_resolution",
                    "pattern": "'It depends on jurisdiction'",
                    "severity": "high",
                    "quote": None,
                    "source": "2023 memo p.2",
                },
                {
                    "name": "clearly_as_argument_substitution",
                    "pattern": "using 'clearly' to avoid arguing",
                    "severity": "high",
                    "quote": None,
                    "source": "2024 memo p.4",
                },
            ],
        ),
        "favored_framings": overrides.get(
            "favored_framings", ["Numerus clausus — closed menu"]
        ),
        "stable_traps": overrides.get(
            "stable_traps",
            [
                {
                    "name": "deed_language_FSSEL_vs_FSD",
                    "desc": "Durational language → FSSEL, not FSD.",
                    "source": None,
                }
            ],
        ),
        "voice_conventions": overrides.get(
            "voice_conventions",
            [
                {
                    "name": "prompt_role_varies",
                    "desc": "Role varies: clerk memo vs advocate.",
                }
            ],
        ),
        "commonly_tested": overrides.get(
            "commonly_tested", ["RAP", "Takings", "Covenants"]
        ),
        "source_artifact_paths": overrides.get(
            "source_artifact_paths", ["/uploads/pollack_2023_exam.md"]
        ),
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_profile_creates_professor_profile_row(temp_env: None) -> None:
    """After build: one ProfessorProfile row AND one Artifact(type=professor_profile)."""
    corpus_id = _seed_corpus()
    memo_ids = _ingest_memos(corpus_id)

    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _mock_profile_payload())
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = build_profile_from_memos(
            session,
            ProfileBuildRequest(
                corpus_id=corpus_id,
                professor_name="Pollack",
                course="Property",
                school="Benjamin N. Cardozo School of Law",
                memo_artifact_ids=memo_ids,
            ),
        )

    assert result.profile.professor_name == "Pollack"
    assert result.profile.pet_peeves[0]["name"] == "hedge_without_resolution"
    assert result.cache_hit is False

    with Session(engine) as session:
        profile_rows = list(session.exec(select(ProfessorProfile)).all())
        assert len(profile_rows) == 1
        assert profile_rows[0].course == "Property"

        # An Artifact row was created by the generate primitive.
        artifact_rows = list(
            session.exec(
                select(Artifact).where(Artifact.type == ArtifactType.PROFESSOR_PROFILE)
            ).all()
        )
        assert len(artifact_rows) == 1
        assert artifact_rows[0].content["pet_peeves"][0]["name"] == "hedge_without_resolution"
        # Artifact was created by the system (LLM extraction), not the user.
        assert artifact_rows[0].created_by is CreatedBy.SYSTEM


def test_build_profile_upserts_on_second_call(temp_env: None) -> None:
    """Same (corpus, name) → second call updates the existing row; no
    IntegrityError from the unique index."""
    corpus_id = _seed_corpus()
    memo_ids = _ingest_memos(corpus_id)

    call_count = {"n": 0}

    def payload_fn(_call_n: int) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _mock_profile_payload(commonly_tested=["RAP"])
        return _mock_profile_payload(
            commonly_tested=["RAP", "Takings", "Covenants", "Zoning"]
        )

    generate_module.set_anthropic_client_factory(_fake_factory(payload_fn))

    engine = db.get_engine()
    with Session(engine) as session:
        first = build_profile_from_memos(
            session,
            ProfileBuildRequest(
                corpus_id=corpus_id,
                professor_name="Pollack",
                course="Property",
                memo_artifact_ids=memo_ids,
            ),
        )
        first_id = first.profile.id
        first_created_at = first.profile.created_at

    # Second call: different payload → force_regenerate so we don't hit the
    # cache. Provide via the same memos but tweak syllabus to bust cache.
    with Session(engine) as session:
        second = build_profile_from_memos(
            session,
            ProfileBuildRequest(
                corpus_id=corpus_id,
                professor_name="Pollack",
                course="Property",
                memo_artifact_ids=memo_ids,
                syllabus_markdown="new syllabus text",  # different → new cache key
            ),
        )

    assert second.profile.id == first_id  # same row id
    assert second.profile.created_at == first_created_at  # created_at preserved
    assert "Zoning" in second.profile.commonly_tested
    # Only one ProfessorProfile row exists.
    with Session(engine) as session:
        rows = list(session.exec(select(ProfessorProfile)).all())
        assert len(rows) == 1


def test_update_profile_validates_schema(temp_env: None) -> None:
    """Malformed edits → ValueError (bad severity value)."""
    corpus_id = _seed_corpus()
    engine = db.get_engine()
    with Session(engine) as session:
        profile = seed_pollack_profile(session, corpus_id)

    bad = {
        "pet_peeves": [
            {
                "name": "x",
                "pattern": "y",
                "severity": "GIGANTIC",  # not in enum
                "source": "z",
            }
        ]
    }

    with Session(engine) as session:
        with pytest.raises(ValueError):
            update_profile(session, profile.id, bad)


def test_update_profile_rejects_unknown_field(temp_env: None) -> None:
    """Attempting to edit a protected field (e.g., corpus_id) is a ValueError."""
    corpus_id = _seed_corpus()
    engine = db.get_engine()
    with Session(engine) as session:
        profile = seed_pollack_profile(session, corpus_id)

    with Session(engine) as session:
        with pytest.raises(ValueError):
            update_profile(session, profile.id, {"corpus_id": "somethingelse"})


def test_update_profile_bumps_updated_at(temp_env: None) -> None:
    corpus_id = _seed_corpus()
    engine = db.get_engine()
    with Session(engine) as session:
        profile = seed_pollack_profile(session, corpus_id)
        profile_id = profile.id
        created_at = profile.created_at

    # Nudge the clock forward so updated_at is strictly greater.
    time.sleep(0.01)

    with Session(engine) as session:
        updated = update_profile(
            session,
            profile_id,
            {"commonly_tested": ["RAP", "Recording Acts", "Takings"]},
        )

    assert updated.updated_at > created_at
    assert "Recording Acts" in updated.commonly_tested


def test_load_profile_for_corpus(temp_env: None) -> None:
    """Returns the profile; None when absent; single profile when no name given."""
    corpus_id = _seed_corpus()
    engine = db.get_engine()

    # Absent → None
    with Session(engine) as session:
        assert load_profile_for_corpus(session, corpus_id, "Pollack") is None
        assert load_profile_for_corpus(session, corpus_id) is None

    # Seed one profile
    with Session(engine) as session:
        seed_pollack_profile(session, corpus_id)

    # Exact match
    with Session(engine) as session:
        p = load_profile_for_corpus(session, corpus_id, "Pollack")
        assert p is not None
        assert p.professor_name == "Pollack"

    # No name, single profile → returns it
    with Session(engine) as session:
        p = load_profile_for_corpus(session, corpus_id)
        assert p is not None
        assert p.professor_name == "Pollack"

    # Wrong name → None
    with Session(engine) as session:
        assert load_profile_for_corpus(session, corpus_id, "Martinez") is None


def test_seed_pollack_profile_idempotent(temp_env: None) -> None:
    corpus_id = _seed_corpus()
    engine = db.get_engine()

    with Session(engine) as session:
        first = seed_pollack_profile(session, corpus_id)
    with Session(engine) as session:
        second = seed_pollack_profile(session, corpus_id)

    assert first.id == second.id

    with Session(engine) as session:
        rows = list(
            session.exec(
                select(ProfessorProfile).where(
                    ProfessorProfile.corpus_id == corpus_id
                )
            ).all()
        )
        assert len(rows) == 1


def test_seed_pollack_has_appendix_a_shape(temp_env: None) -> None:
    """Seeded profile carries the Appendix A signature items."""
    corpus_id = _seed_corpus()
    engine = db.get_engine()
    with Session(engine) as session:
        profile = seed_pollack_profile(session, corpus_id)

    peeve_names = {p["name"] for p in profile.pet_peeves}
    assert "hedge_without_resolution" in peeve_names
    assert "clearly_as_argument_substitution" in peeve_names
    assert "mismatched_future_interests" in peeve_names

    # commonly_tested includes Takings (the Loretto/Lucas/Penn Central bullet).
    assert any("Takings" in item for item in profile.commonly_tested)

    # favored_framings mentions numerus clausus.
    assert any("numerus clausus" in f.lower() for f in profile.favored_framings)

    # Module-level constant also matches the seeded row's content.
    assert APPENDIX_A_POLLACK_PROFILE["professor_name"] == "Pollack"
