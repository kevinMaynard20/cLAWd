"""Tests for features/corpus_export.py (spec §6.3)."""

from __future__ import annotations

import io
import json
import tarfile
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
from features.corpus_export import (
    EXPORT_SCHEMA_VERSION,
    CorpusExportError,
    export_corpus,
    export_filename,
)


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


@pytest.fixture
def populated_corpus(temp_db: None) -> str:
    """Seed a small but realistic corpus: corpus + book + page + 2 blocks +
    1 artifact + 1 cost event."""
    engine = db.get_engine()
    with Session(engine) as session:
        c = Corpus(name="Property — Pollack", course="Property")
        session.add(c)
        session.commit()
        session.refresh(c)
        cid = c.id

        book = Book(
            id="b" * 64,
            corpus_id=cid,
            title="Property Casebook",
            edition="9th",
            authors=["Dukeminier", "Krier"],
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=1500,
        )
        session.add(book)
        session.commit()

        page = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="b.pdf",
            pdf_page_start=1000,
            pdf_page_end=1002,
            markdown="# page 518",
            raw_text="page 518",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        session.add(
            Block(
                page_id=page.id,
                book_id=book.id,
                order_index=0,
                type=BlockType.CASE_OPINION,
                source_page=518,
                markdown="opinion text",
                block_metadata={"case_name": "Shelley v. Kraemer"},
            )
        )
        session.add(
            Block(
                page_id=page.id,
                book_id=book.id,
                order_index=1,
                type=BlockType.NUMBERED_NOTE,
                source_page=518,
                markdown="1. Note",
                block_metadata={"number": 1},
            )
        )
        session.commit()

        artifact = Artifact(
            corpus_id=cid,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            content={"case_name": "Shelley v. Kraemer"},
            sources=[{"kind": "block", "id": "x"}],
            prompt_template="case_brief@1.2.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0.0525"),
            cache_key="cache-1",
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)

        session.add(
            CostEvent(
                session_id="s1",
                model="claude-opus-4-7",
                provider=Provider.ANTHROPIC,
                input_tokens=1200,
                output_tokens=450,
                input_cost_usd=Decimal("0.018"),
                output_cost_usd=Decimal("0.03375"),
                total_cost_usd=Decimal("0.05175"),
                feature="case_brief",
                artifact_id=artifact.id,
            )
        )
        session.commit()

        return cid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_unknown_corpus_raises(temp_db: None) -> None:
    with (
        Session(db.get_engine()) as session,
        pytest.raises(CorpusExportError, match="not found"),
    ):
        export_corpus(session, "nonexistent")


def test_export_returns_valid_gzip_tar(populated_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        archive = export_corpus(session, populated_corpus)
    assert isinstance(archive, bytes)
    assert len(archive) > 100
    # gzip magic
    assert archive[:2] == b"\x1f\x8b"
    # Tar inside gzip should open cleanly
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = set(tar.getnames())
    expected = {
        "manifest.json",
        "corpus.json",
        "books.jsonl",
        "pages.jsonl",
        "blocks.jsonl",
        "toc_entries.jsonl",
        "artifacts.jsonl",
        "transcripts.jsonl",
        "transcript_segments.jsonl",
        "emphasis_items.jsonl",
        "syllabi.jsonl",
        "syllabus_entries.jsonl",
        "professor_profiles.jsonl",
        "flashcard_reviews.jsonl",
        "cost_events.jsonl",
    }
    assert expected.issubset(names)


def test_export_manifest_has_schema_version_and_counts(populated_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        archive = export_corpus(session, populated_corpus)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None
        manifest = json.loads(manifest_member.read())
    assert manifest["schema_version"] == EXPORT_SCHEMA_VERSION
    assert manifest["corpus_id"] == populated_corpus
    counts = manifest["table_counts"]
    assert counts["corpus"] == 1
    assert counts["books"] == 1
    assert counts["pages"] == 1
    assert counts["blocks"] == 2
    assert counts["artifacts"] == 1
    assert counts["cost_events"] == 1


def test_export_blocks_jsonl_has_block_metadata(populated_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        archive = export_corpus(session, populated_corpus)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        blocks_member = tar.extractfile("blocks.jsonl")
        assert blocks_member is not None
        lines = blocks_member.read().decode("utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    types = {p["type"] for p in parsed}
    assert types == {"case_opinion", "numbered_note"}
    # Decimal coercion landed
    case_brief = next(p for p in parsed if p["type"] == "case_opinion")
    assert case_brief["block_metadata"]["case_name"] == "Shelley v. Kraemer"


def test_export_artifacts_decimal_serialized_as_string(populated_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        archive = export_corpus(session, populated_corpus)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile("artifacts.jsonl")
        assert member is not None
        line = member.read().decode("utf-8").strip()
    artifact_data = json.loads(line)
    # Decimal → str so the JSON is portable across languages. SQLAlchemy
    # Numeric(20,10) zero-pads the right side; compare numerically.
    assert isinstance(artifact_data["cost_usd"], str)
    assert Decimal(artifact_data["cost_usd"]) == Decimal("0.0525")


def test_export_cost_events_filtered_to_corpus(populated_corpus: str, temp_db: None) -> None:
    """A cost event whose artifact_id is None or belongs to a different
    corpus must NOT appear in the export."""
    with Session(db.get_engine()) as session:
        # Add a totally unrelated cost event (no artifact_id) — should be excluded.
        session.add(
            CostEvent(
                session_id="other",
                model="claude-haiku-4-5",
                provider=Provider.ANTHROPIC,
                input_tokens=10,
                output_tokens=10,
                total_cost_usd=Decimal("0.001"),
                feature="unrelated_feature",
                artifact_id=None,
            )
        )
        session.commit()
    with Session(db.get_engine()) as session:
        archive = export_corpus(session, populated_corpus)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile("cost_events.jsonl")
        assert member is not None
        text = member.read().decode("utf-8").strip()
    lines = text.splitlines() if text else []
    # Only the corpus's own event lands in the archive.
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["feature"] == "case_brief"


def test_export_filename_format(populated_corpus: str) -> None:
    with Session(db.get_engine()) as session:
        from sqlmodel import select

        corpus = session.exec(
            select(Corpus).where(Corpus.id == populated_corpus)
        ).one()
    fname = export_filename(corpus)
    assert fname.startswith("corpus_")
    assert fname.endswith(".tar.gz")
    assert "Property" in fname
