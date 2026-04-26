"""Unit tests for `features.past_exam_ingest` (spec §9 Phase 3).

No LLM call involved — this is pure persistence. Each test creates a fresh
SQLite temp DB and seeds a Corpus before calling the feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from data import db
from data.models import Artifact, ArtifactType, Corpus, CreatedBy
from features.past_exam_ingest import (
    PastExamIngestRequest,
    ingest_past_exam,
)


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


def _seed_corpus() -> str:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        return corpus.id


def test_ingest_past_exam_persists_two_artifacts(temp_env: None) -> None:
    """With both exam + memo text: two artifact rows, linked by
    `content["tied_to_past_exam"]`."""
    corpus_id = _seed_corpus()
    engine = db.get_engine()

    with Session(engine) as session:
        req = PastExamIngestRequest(
            corpus_id=corpus_id,
            exam_markdown="# 2023 Property Exam\n\nPart II: Harriet conveys...",
            grader_memo_markdown="Memo: common errors included hedging and...",
            source_paths=["/uploads/pollack_2023_exam.md", "/uploads/pollack_2023_memo.md"],
            year=2023,
            professor_name="Pollack",
        )
        result = ingest_past_exam(session, req)

    assert result.past_exam_artifact_id
    assert result.grader_memo_artifact_id

    with Session(engine) as session:
        artifacts = list(session.exec(select(Artifact)).all())
        by_id = {a.id: a for a in artifacts}
        exam = by_id[result.past_exam_artifact_id]
        memo = by_id[result.grader_memo_artifact_id]

        assert exam.type is ArtifactType.PAST_EXAM
        assert memo.type is ArtifactType.GRADER_MEMO
        assert exam.content["markdown"].startswith("# 2023 Property Exam")
        assert exam.content["year"] == 2023
        assert exam.content["professor_name"] == "Pollack"
        assert memo.content["tied_to_past_exam"] == exam.id
        # Provenance plumbing: both carry the original upload paths.
        assert "/uploads/pollack_2023_exam.md" in exam.content["source_paths"]
        # User-uploaded → created_by=USER, no cost, no prompt, no cache_key.
        assert exam.created_by is CreatedBy.USER
        assert memo.created_by is CreatedBy.USER
        assert exam.prompt_template == ""
        assert memo.prompt_template == ""
        assert exam.llm_model == ""
        assert int(exam.cost_usd) == 0
        assert int(memo.cost_usd) == 0
        assert exam.cache_key == ""
        # Memo's parent_artifact_id chains back to the exam.
        assert memo.parent_artifact_id == exam.id


def test_ingest_past_exam_without_memo_persists_only_exam(temp_env: None) -> None:
    """Memo is optional: omitting it gives a single past_exam artifact and
    `grader_memo_artifact_id` is None."""
    corpus_id = _seed_corpus()
    engine = db.get_engine()

    with Session(engine) as session:
        result = ingest_past_exam(
            session,
            PastExamIngestRequest(
                corpus_id=corpus_id,
                exam_markdown="# 2025 Exam only",
                grader_memo_markdown=None,
                source_paths=[],
                year=2025,
            ),
        )

    assert result.past_exam_artifact_id
    assert result.grader_memo_artifact_id is None

    with Session(engine) as session:
        artifacts = list(session.exec(select(Artifact)).all())
        assert len(artifacts) == 1
        (exam,) = artifacts
        assert exam.type is ArtifactType.PAST_EXAM
        assert exam.content["year"] == 2025
