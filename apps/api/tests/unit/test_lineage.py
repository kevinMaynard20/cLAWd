"""Tests for features/lineage.py (spec §7.4)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session

from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    CostEvent,
    CreatedBy,
    Page,
    Provider,
)
from features.lineage import LineageError, build_lineage


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


@pytest.fixture
def chain_seed(temp_db: None) -> dict[str, str]:
    """Build a 3-step lineage: rubric → grade → re-grade.
    Returns dict with corpus_id, rubric_id, grade_id, regrade_id, block_id."""
    engine = db.get_engine()
    with Session(engine) as session:
        c = Corpus(name="Property", course="Property")
        session.add(c)
        session.commit()
        session.refresh(c)
        cid = c.id

        book = Book(
            id="b" * 64,
            corpus_id=cid,
            title="t",
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=10,
        )
        session.add(book)
        session.commit()
        page = Page(
            book_id=book.id,
            source_page=1,
            batch_pdf="b.pdf",
            pdf_page_start=1,
            pdf_page_end=2,
            markdown="m",
            raw_text="r",
        )
        session.add(page)
        session.commit()
        session.refresh(page)
        block = Block(
            page_id=page.id,
            book_id=book.id,
            order_index=0,
            type=BlockType.CASE_OPINION,
            source_page=1,
            markdown="opinion",
            block_metadata={"case_name": "Smith v. Jones"},
        )
        session.add(block)
        session.commit()
        session.refresh(block)

        rubric = Artifact(
            corpus_id=cid,
            type=ArtifactType.RUBRIC,
            created_by=CreatedBy.SYSTEM,
            content={"required_issues": []},
            sources=[{"kind": "block", "id": block.id}],
            prompt_template="rubric_from_memo@1.0.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0.05"),
            cache_key="rk",
        )
        session.add(rubric)
        session.commit()
        session.refresh(rubric)

        grade = Artifact(
            corpus_id=cid,
            type=ArtifactType.GRADE,
            created_by=CreatedBy.SYSTEM,
            content={"overall_score": 80},
            sources=[
                {"kind": "block", "id": block.id},
                {"kind": "block", "id": "missing-block-id"},  # intentional miss
            ],
            parent_artifact_id=rubric.id,
            prompt_template="irac_grade@1.0.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0.08"),
            cache_key="gk",
        )
        session.add(grade)
        session.commit()
        session.refresh(grade)

        regrade = Artifact(
            corpus_id=cid,
            type=ArtifactType.GRADE,
            created_by=CreatedBy.SYSTEM,
            content={"overall_score": 82},
            sources=[{"kind": "block", "id": block.id}],
            parent_artifact_id=grade.id,
            prompt_template="irac_grade@1.0.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0.08"),
            cache_key="rgk",
        )
        session.add(regrade)
        session.commit()
        session.refresh(regrade)

        # CostEvents for each step
        for art_id, cost in (
            (rubric.id, Decimal("0.05")),
            (grade.id, Decimal("0.08")),
            (regrade.id, Decimal("0.08")),
        ):
            session.add(
                CostEvent(
                    session_id="s1",
                    model="claude-opus-4-7",
                    provider=Provider.ANTHROPIC,
                    input_tokens=1000,
                    output_tokens=400,
                    total_cost_usd=cost,
                    feature="irac_grade",
                    artifact_id=art_id,
                )
            )
        session.commit()

        return {
            "corpus_id": cid,
            "rubric_id": rubric.id,
            "grade_id": grade.id,
            "regrade_id": regrade.id,
            "block_id": block.id,
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_lineage_unknown_artifact_raises(temp_db: None) -> None:
    with (
        Session(db.get_engine()) as session,
        pytest.raises(LineageError, match="not found"),
    ):
        build_lineage(session, "nonexistent")


def test_lineage_chain_root_first(chain_seed: dict) -> None:
    with Session(db.get_engine()) as session:
        report = build_lineage(session, chain_seed["regrade_id"])
    chain_ids = [n.id for n in report.chain]
    assert chain_ids == [chain_seed["rubric_id"], chain_seed["grade_id"], chain_seed["regrade_id"]]


def test_lineage_target_with_no_parent(chain_seed: dict) -> None:
    with Session(db.get_engine()) as session:
        report = build_lineage(session, chain_seed["rubric_id"])
    assert len(report.chain) == 1
    assert report.chain[0].id == chain_seed["rubric_id"]


def test_lineage_total_cost_sums_all_chain_events(chain_seed: dict) -> None:
    with Session(db.get_engine()) as session:
        report = build_lineage(session, chain_seed["regrade_id"])
    assert report.total_cost_usd == Decimal("0.21")  # 0.05 + 0.08 + 0.08


def test_lineage_events_in_timestamp_order(chain_seed: dict) -> None:
    with Session(db.get_engine()) as session:
        report = build_lineage(session, chain_seed["regrade_id"])
    timestamps = [e.timestamp for e in report.events]
    assert timestamps == sorted(timestamps)


def test_lineage_sources_summary_marks_missing(chain_seed: dict) -> None:
    """The grade artifact references one valid block id and one bogus id;
    `sources_summary` resolves the valid one and surfaces the missing id."""
    with Session(db.get_engine()) as session:
        report = build_lineage(session, chain_seed["grade_id"])
    grade_node = report.chain[-1]
    summaries = grade_node.sources_summary
    found_states = {s["id"]: s.get("found") for s in summaries}
    assert found_states[chain_seed["block_id"]] is True
    assert found_states["missing-block-id"] is False
    assert "missing-block-id" in report.missing_sources


def test_lineage_block_summary_includes_case_name(chain_seed: dict) -> None:
    with Session(db.get_engine()) as session:
        report = build_lineage(session, chain_seed["grade_id"])
    grade_node = report.chain[-1]
    block_summary = next(
        s for s in grade_node.sources_summary if s["id"] == chain_seed["block_id"]
    )
    assert block_summary["case_name"] == "Smith v. Jones"
    assert block_summary["source_page"] == 1
    assert block_summary["block_type"] == "case_opinion"


def test_lineage_cited_counts(chain_seed: dict) -> None:
    """`cited_block_count` should report citations on the TARGET only,
    not the whole chain."""
    with Session(db.get_engine()) as session:
        report = build_lineage(session, chain_seed["regrade_id"])
    # Target's sources is 1 block (the missing one was cleaned out at regrade)
    assert report.cited_block_count == 1
    assert report.cited_segment_count == 0


def test_lineage_handles_self_referential_loop(temp_db: None) -> None:
    """Defensive: an artifact whose parent_artifact_id points to itself
    must not infinite-loop."""
    engine = db.get_engine()
    with Session(engine) as session:
        c = Corpus(name="x", course="x")
        session.add(c)
        session.commit()
        session.refresh(c)
        a = Artifact(
            corpus_id=c.id,
            type=ArtifactType.CASE_BRIEF,
            content={},
            sources=[],
            prompt_template="t@1",
            llm_model="m",
            cost_usd=Decimal("0"),
            cache_key="k",
        )
        session.add(a)
        session.commit()
        session.refresh(a)
        artifact_id = a.id  # capture before session closes
        a.parent_artifact_id = artifact_id
        session.add(a)
        session.commit()

    with Session(db.get_engine()) as session:
        report = build_lineage(session, artifact_id)
    # Walk should terminate; chain should be exactly one node.
    assert len(report.chain) == 1
