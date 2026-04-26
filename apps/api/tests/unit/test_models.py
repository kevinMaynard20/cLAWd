"""Unit tests for data/models.py (spec §3).

Scope: ensure every model instantiates, round-trips through SQLite, enforces
the enum types, and that the Credentials envelope masks keys properly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlmodel import Session, select

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
    Credentials,
    IngestionMethod,
    Page,
    Provider,
    TocEntry,
)


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh SQLite file per test. Resets the module-level engine."""
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


def test_corpus_roundtrip(temp_db: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack – Spring 2026", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        loaded = session.exec(select(Corpus)).one()
        assert loaded.id == corpus.id
        assert loaded.name == "Property – Pollack – Spring 2026"
        assert loaded.course == "Property"
        assert isinstance(loaded.created_at, datetime)


def test_book_with_content_hash_id(temp_db: None) -> None:
    engine = db.get_engine()
    fake_hash = "a" * 64  # SHA-256 hex length
    with Session(engine) as session:
        corpus = Corpus(name="c1", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        book = Book(
            id=fake_hash,
            corpus_id=corpus.id,
            title="Property: Cases and Materials",
            edition="9th",
            authors=["Dukeminier", "Krier", "Alexander"],
            source_pdf_path="/uploads/property.pdf",
            batch_hashes=[fake_hash[:32] + "0" * 32, "b" * 64],
            source_page_min=1,
            source_page_max=1423,
            ingestion_method=IngestionMethod.MARKER_LLM,
        )
        session.add(book)
        session.commit()

        loaded = session.exec(select(Book)).one()
        assert loaded.id == fake_hash
        assert loaded.authors == ["Dukeminier", "Krier", "Alexander"]
        assert loaded.ingestion_method is IngestionMethod.MARKER_LLM
        assert loaded.source_page_max == 1423


def test_page_and_blocks(temp_db: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="c", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        book = Book(
            id="b" * 64,
            corpus_id=corpus.id,
            title="t",
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=10,
        )
        session.add(book)
        session.commit()

        page = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="batch-3.pdf",
            pdf_page_start=1104,
            pdf_page_end=1106,
            markdown="# Shelley v. Kraemer\n\n...",
            raw_text="Shelley v. Kraemer\n\n...",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        header = Block(
            page_id=page.id,
            book_id=book.id,
            order_index=0,
            type=BlockType.CASE_HEADER,
            source_page=518,
            markdown="**Shelley v. Kraemer**",
            block_metadata={"case_name": "Shelley v. Kraemer"},
        )
        opinion = Block(
            page_id=page.id,
            book_id=book.id,
            order_index=1,
            type=BlockType.CASE_OPINION,
            source_page=518,
            markdown="The judicial enforcement of private racially restrictive covenants…",
            block_metadata={
                "court": "Supreme Court of the United States",
                "year": 1948,
                "citation": "334 U.S. 1",
                "case_name": "Shelley v. Kraemer",
            },
        )
        session.add_all([header, opinion])
        session.commit()

        loaded_page = session.exec(select(Page)).one()
        assert loaded_page.source_page == 518
        assert len(loaded_page.blocks) == 2
        types = {b.type for b in loaded_page.blocks}
        assert BlockType.CASE_HEADER in types
        assert BlockType.CASE_OPINION in types

        # block_metadata JSON roundtrips
        opinion_loaded = next(b for b in loaded_page.blocks if b.type == BlockType.CASE_OPINION)
        assert opinion_loaded.block_metadata["citation"] == "334 U.S. 1"
        assert opinion_loaded.block_metadata["year"] == 1948


def test_toc_nesting(temp_db: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="c", course="Property")
        session.add(corpus)
        session.commit()
        book = Book(
            id="c" * 64,
            corpus_id=corpus.id,
            title="t",
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=10,
        )
        session.add(book)
        session.commit()

        part = TocEntry(book_id=book.id, level=1, title="Part VIII: Takings", source_page=450, order_index=0)
        session.add(part)
        session.commit()
        session.refresh(part)

        ch = TocEntry(
            book_id=book.id,
            parent_id=part.id,
            level=2,
            title="Chapter 10: Regulatory Takings",
            source_page=455,
            order_index=0,
        )
        session.add(ch)
        session.commit()

        loaded = session.exec(select(TocEntry).where(TocEntry.level == 2)).one()
        assert loaded.parent_id == part.id
        assert loaded.title == "Chapter 10: Regulatory Takings"


def test_cost_event_recording(temp_db: None) -> None:
    """Spec §7.7.8 test: after a mocked LLM call, a CostEvent is persisted with
    correct token counts and computed cost. Here we just test the persistence
    layer; the token-counting wiring is tested in Phase 2."""
    engine = db.get_engine()
    with Session(engine) as session:
        event = CostEvent(
            session_id="session-abc",
            model="claude-opus-4-7",
            provider=Provider.ANTHROPIC,
            input_tokens=1200,
            output_tokens=450,
            input_cost_usd=Decimal("0.018"),
            output_cost_usd=Decimal("0.03375"),
            total_cost_usd=Decimal("0.05175"),
            feature="case_brief",
        )
        session.add(event)
        session.commit()

        loaded = session.exec(select(CostEvent)).one()
        assert loaded.model == "claude-opus-4-7"
        assert loaded.provider is Provider.ANTHROPIC
        assert loaded.input_tokens == 1200
        assert loaded.feature == "case_brief"
        assert not loaded.cached


def test_artifact_roundtrip(temp_db: None) -> None:
    """Spec §3.11: every generated output is an Artifact with sources + content."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        artifact = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            sources=[
                {"kind": "block", "id": "blk-1"},
                {"kind": "block", "id": "blk-2"},
            ],
            content={"case_name": "Shelley v. Kraemer", "holding": {"text": "..."}},
            prompt_template="case_brief@1.2.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0.0525"),
            cache_key="sha256-abc123",
        )
        session.add(artifact)
        session.commit()

        loaded = session.exec(select(Artifact)).one()
        assert loaded.type is ArtifactType.CASE_BRIEF
        assert loaded.created_by is CreatedBy.SYSTEM
        assert loaded.sources[0]["id"] == "blk-1"
        assert loaded.content["case_name"] == "Shelley v. Kraemer"
        assert loaded.cache_key == "sha256-abc123"
        assert loaded.cost_usd == Decimal("0.0525")
        assert loaded.regenerable is True


def test_artifact_parent_self_fk(temp_db: None) -> None:
    """A practice_answer artifact can reference its parent hypo."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="c", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

        hypo = Artifact(corpus_id=corpus.id, type=ArtifactType.HYPO, content={"prompt": "..."})
        session.add(hypo)
        session.commit()
        session.refresh(hypo)

        answer = Artifact(
            corpus_id=corpus.id,
            type=ArtifactType.PRACTICE_ANSWER,
            parent_artifact_id=hypo.id,
            created_by=CreatedBy.USER,
            content={"body": "the answer..."},
        )
        session.add(answer)
        session.commit()

        loaded = session.exec(
            select(Artifact).where(Artifact.type == ArtifactType.PRACTICE_ANSWER)
        ).one()
        assert loaded.parent_artifact_id == hypo.id


def test_cost_event_cached_hit(temp_db: None) -> None:
    """Cache hits emit a CostEvent with cached=True and $0 cost for bookkeeping
    (spec §4.3 Caching)."""
    engine = db.get_engine()
    with Session(engine) as session:
        event = CostEvent(
            session_id="s1",
            model="claude-opus-4-7",
            provider=Provider.ANTHROPIC,
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=Decimal("0"),
            feature="case_brief",
            cached=True,
        )
        session.add(event)
        session.commit()
        loaded = session.exec(select(CostEvent)).one()
        assert loaded.cached is True
        assert loaded.total_cost_usd == Decimal("0")


# ---------------------------------------------------------------------------
# Credentials envelope (spec §3.13, §7.7)
# ---------------------------------------------------------------------------


def test_credentials_display_masks() -> None:
    """SecretStr must render last-4 only (§3.13, §7.7.3)."""
    creds = Credentials(
        anthropic_api_key=SecretStr("sk-ant-api03-abcdefghijklmnopXYZ9"),
        voyage_api_key=SecretStr("pa-abcdefghijklmnopqrstuvWXYZ"),
    )
    display = creds.anthropic_display()
    assert display is not None
    assert display.startswith("sk-ant-")
    assert display.endswith("XYZ9")
    assert "abcdefghij" not in display  # middle must be masked

    voyage = creds.voyage_display()
    assert voyage is not None
    assert voyage.endswith("WXYZ")
    assert "abcdefghij" not in voyage


def test_credentials_missing_returns_none() -> None:
    creds = Credentials()
    assert creds.anthropic_display() is None
    assert creds.voyage_display() is None


def test_credentials_repr_hides_secret() -> None:
    """A casual log line containing a Credentials should not leak the key."""
    creds = Credentials(
        anthropic_api_key=SecretStr("sk-ant-api03-SECRET-KEY-VALUE-ZZZZ"),
        last_validated_at=datetime.now(tz=UTC),
        last_validation_ok=True,
    )
    rendered = repr(creds)
    assert "SECRET-KEY-VALUE" not in rendered
    assert "SecretStr" in rendered or "**" in rendered
