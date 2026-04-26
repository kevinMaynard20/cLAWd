"""End-to-end test for corpus export → restore round-trip (Q51)."""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    CreatedBy,
    Page,
)


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "c.enc"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


@pytest.fixture
def populated(temp_env: None) -> str:
    engine = db.get_engine()
    with Session(engine) as session:
        c = Corpus(name="Property — Pollack", course="Property", professor_name="Pollack")
        session.add(c)
        session.commit()
        session.refresh(c)
        cid = c.id
        session.add(
            Book(
                id="b" * 64,
                corpus_id=cid,
                title="Casebook",
                edition="9th",
                authors=["A", "B"],
                source_pdf_path="/p.pdf",
                source_page_min=1,
                source_page_max=100,
            )
        )
        session.commit()
        page = Page(
            book_id="b" * 64,
            source_page=1,
            batch_pdf="b.pdf",
            pdf_page_start=1,
            pdf_page_end=2,
            markdown="# m",
            raw_text="m",
        )
        session.add(page)
        session.commit()
        session.refresh(page)
        session.add(
            Block(
                page_id=page.id,
                book_id="b" * 64,
                order_index=0,
                type=BlockType.CASE_OPINION,
                source_page=1,
                markdown="opinion",
                block_metadata={"case_name": "T v. C"},
            )
        )
        session.add(
            Artifact(
                corpus_id=cid,
                type=ArtifactType.CASE_BRIEF,
                created_by=CreatedBy.SYSTEM,
                content={"case_name": "T v. C"},
                sources=[{"kind": "block", "id": "x"}],
                prompt_template="case_brief@1.2.0",
                llm_model="claude-opus-4-7",
                cost_usd=Decimal("0.05"),
                cache_key="ck",
            )
        )
        session.commit()
        return cid


def test_export_then_restore_round_trip(
    client: TestClient,
    populated: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realistic Q51 use case: export on one DB, restore on a fresh DB.
    (Restoring into the same DB hits the content-addressed Book/Transcript
    UNIQUE constraint — that's expected; the round-trip happens across
    machines.)"""
    # 1) Export
    r_export = client.get(f"/corpora/{populated}/export")
    assert r_export.status_code == 200
    archive_bytes = r_export.content
    assert archive_bytes[:2] == b"\x1f\x8b"

    # 2) Reset DB to simulate a fresh machine
    db.reset_engine()
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "fresh.db"))
    db.init_schema()

    # 3) Re-build the TestClient against the new DB
    from main import app

    fresh_client = TestClient(app)

    # 4) Restore
    r_restore = fresh_client.post(
        "/corpora/restore",
        files={"archive": ("export.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
        data={"new_corpus_name": "Restored", "preserve_corpus_id": "false"},
    )
    assert r_restore.status_code == 200, r_restore.text
    body = r_restore.json()
    assert body["new_corpus_id"] != populated
    assert body["table_counts"]["corpus"] == 1
    assert body["table_counts"]["books"] == 1
    assert body["table_counts"]["blocks"] == 1
    assert body["table_counts"]["artifacts"] == 1

    # 5) Confirm the new corpus exists with the renamed name
    new_id = body["new_corpus_id"]
    with Session(db.get_engine()) as session:
        new_c = session.exec(select(Corpus).where(Corpus.id == new_id)).one()
    assert new_c.name == "Restored"
    assert new_c.professor_name == "Pollack"


def test_restore_rejects_archive_with_wrong_schema_version(
    client: TestClient, populated: str
) -> None:
    """Manifest schema_version must match the current EXPORT_SCHEMA_VERSION."""
    import io
    import json
    import tarfile

    # Build an archive with a deliberately-bad schema_version
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        bad_manifest = json.dumps({"schema_version": 999, "corpus_id": "x"}).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(bad_manifest)
        tar.addfile(info, io.BytesIO(bad_manifest))
        corpus_blob = json.dumps({"id": "x", "name": "x", "course": "x"}).encode()
        info = tarfile.TarInfo(name="corpus.json")
        info.size = len(corpus_blob)
        tar.addfile(info, io.BytesIO(corpus_blob))

    r = client.post(
        "/corpora/restore",
        files={"archive": ("bad.tar.gz", io.BytesIO(out.getvalue()), "application/gzip")},
    )
    assert r.status_code == 400
    assert "schema_version" in r.json()["detail"]


def test_restore_rejects_empty_archive(client: TestClient) -> None:
    r = client.post(
        "/corpora/restore",
        files={"archive": ("empty.tar.gz", io.BytesIO(b""), "application/gzip")},
    )
    assert r.status_code == 400
