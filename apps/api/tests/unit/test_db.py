"""Unit tests for data/db.py — engine creation, schema init, sqlite-vec load.

Spec §7.1 says SQLite + sqlite-vec; sqlite-vec install is optional for Phase 1
so the tests are conditional on its presence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import SQLModel

from data import db


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    yield
    db.reset_engine()


def test_engine_creates_db_file(temp_db: None, tmp_path: Path) -> None:
    engine = db.get_engine()
    db.init_schema()
    assert (tmp_path / "test.db").exists()
    # Check at least one known table landed.
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='book'")
        ).all()
        assert len(rows) == 1


def test_init_schema_idempotent(temp_db: None) -> None:
    db.init_schema()
    # Second call must not raise.
    db.init_schema()
    # Tables should still be there and intact.
    engine = db.get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")).one()
        # Phase 1–6: 14 prior + background_task = 15
        assert result[0] >= 15


def test_foreign_keys_on(temp_db: None) -> None:
    db.init_schema()
    engine = db.get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("PRAGMA foreign_keys")).one()
        assert row[0] == 1


def test_journal_mode_wal(temp_db: None) -> None:
    db.init_schema()
    engine = db.get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("PRAGMA journal_mode")).one()
        assert row[0].lower() == "wal"


def test_sqlite_vec_loads_if_available(temp_db: None) -> None:
    """If sqlite-vec is installed, vec0 virtual table creation should succeed."""
    pytest.importorskip("sqlite_vec")
    db.init_schema()
    engine = db.get_engine()
    with engine.connect() as conn:
        # Create and drop a trivial vec0 virtual table — confirms the extension
        # is actually loaded, not just import-available.
        conn.execute(text("CREATE VIRTUAL TABLE _vec_test USING vec0(embedding float[4])"))
        conn.execute(text("DROP TABLE _vec_test"))


def test_session_scope_commits_on_success(temp_db: None) -> None:
    from sqlmodel import select

    from data.models import Corpus

    db.init_schema()
    with db.session_scope() as session:
        session.add(Corpus(name="Property", course="Property"))
    # Fresh session should see it (new connection).
    with db.session_scope() as session:
        rows = session.exec(select(Corpus)).all()
        assert len(rows) == 1


def test_session_scope_rolls_back_on_exception(temp_db: None) -> None:
    from sqlmodel import select

    from data.models import Corpus

    db.init_schema()
    with pytest.raises(RuntimeError):
        with db.session_scope() as session:
            session.add(Corpus(name="Will rollback", course="Property"))
            raise RuntimeError("simulated failure")
    with db.session_scope() as session:
        rows = session.exec(select(Corpus)).all()
        assert len(rows) == 0


def test_metadata_registered_all_tables() -> None:
    """All Phase 1–6 models must be in SQLModel.metadata.tables."""
    expected = {
        "corpus",
        "book",
        "page",
        "block",
        "toc_entry",
        "cost_event",
        "artifact",
        "professor_profile",
        "transcript",
        "transcript_segment",
        "emphasis_item",
        "syllabus",
        "syllabus_entry",
        "flashcard_review",
        "background_task",
    }
    assert expected.issubset(set(SQLModel.metadata.tables.keys()))
