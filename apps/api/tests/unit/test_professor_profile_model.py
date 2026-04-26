"""Unit tests for ProfessorProfile SQLModel (spec §3.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from data import db
from data.models import Corpus, ProfessorProfile


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


def test_professor_profile_roundtrip(temp_db: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property — Pollack — Spring 2026", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        profile = ProfessorProfile(
            corpus_id=corpus.id,
            professor_name="Pollack",
            course="Property",
            school="Benjamin N. Cardozo School of Law",
            exam_format={
                "duration_hours": 5,
                "word_limit": 4000,
                "open_book": False,
                "structure": [
                    {"part": "I", "weight": 10, "type": "multiple_choice", "count": 10},
                    {"part": "II-IV", "weight": 30, "type": "issue_spotter_essay"},
                ],
            },
            pet_peeves=[
                {
                    "name": "hedge_without_resolution",
                    "pattern": "'It depends on the jurisdiction'",
                    "severity": "high",
                    "quote": "'Well, Client, it all depends on the facts' is not the kind of analysis that anyone will pay you very much to provide.",
                    "source": "2023 memo p.2",
                },
            ],
            favored_framings=["Numerus clausus — the menu of estates is closed"],
            stable_traps=[
                {
                    "name": "deed_language_FSSEL_vs_FSD",
                    "desc": "Durational language → FSSEL, not FSD.",
                },
            ],
            commonly_tested=["RAP", "Recording acts", "Covenants"],
            source_artifact_paths=["storage/artifacts/pollack_2023_memo.md"],
        )
        session.add(profile)
        session.commit()

        loaded = session.exec(select(ProfessorProfile)).one()
        assert loaded.professor_name == "Pollack"
        assert loaded.exam_format["duration_hours"] == 5
        assert loaded.pet_peeves[0]["name"] == "hedge_without_resolution"
        assert loaded.stable_traps[0]["name"] == "deed_language_FSSEL_vs_FSD"
        assert "RAP" in loaded.commonly_tested
        # created_at and updated_at are both factory-set at insert time with
        # separate `datetime.now()` calls, so they may differ by microseconds;
        # just verify both are populated.
        assert loaded.created_at is not None
        assert loaded.updated_at is not None


def test_professor_profile_unique_per_corpus_and_name(temp_db: None) -> None:
    """Spec: one profile per (corpus, professor_name). A duplicate raises."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="c", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        session.add(ProfessorProfile(corpus_id=corpus.id, professor_name="Pollack", course="Property"))
        session.commit()

        session.add(ProfessorProfile(corpus_id=corpus.id, professor_name="Pollack", course="Property"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_professor_profile_two_professors_same_corpus_ok(temp_db: None) -> None:
    """A corpus may reference multiple profiles when a course has co-teachers."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="c", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        session.add(
            ProfessorProfile(corpus_id=corpus.id, professor_name="Pollack", course="Property")
        )
        session.add(
            ProfessorProfile(corpus_id=corpus.id, professor_name="Martinez", course="Property")
        )
        session.commit()

        count = len(session.exec(select(ProfessorProfile)).all())
        assert count == 2
