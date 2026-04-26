"""Integration tests for /corpora — the dashboard's data source."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db
from data.models import Corpus


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


def test_list_corpora_empty(client: TestClient) -> None:
    r = client.get("/corpora")
    assert r.status_code == 200
    assert r.json() == []


def test_create_and_list_corpus(client: TestClient) -> None:
    r_create = client.post(
        "/corpora",
        json={
            "name": "Property — Pollack — Spring 2026",
            "course": "Property",
            "professor_name": "Pollack",
            "school": "Cardozo",
        },
    )
    assert r_create.status_code == 201
    created = r_create.json()
    assert created["id"]
    assert created["name"] == "Property — Pollack — Spring 2026"
    assert created["book_count"] == 0
    assert created["artifact_count"] == 0

    r_list = client.get("/corpora")
    body = r_list.json()
    assert len(body) == 1
    assert body[0]["id"] == created["id"]


def test_get_corpus_by_id(client: TestClient) -> None:
    r_create = client.post(
        "/corpora",
        json={"name": "c", "course": "Property"},
    )
    cid = r_create.json()["id"]
    r_get = client.get(f"/corpora/{cid}")
    assert r_get.status_code == 200
    assert r_get.json()["id"] == cid


def test_get_corpus_404_on_unknown(client: TestClient) -> None:
    r = client.get("/corpora/nonexistent-id")
    assert r.status_code == 404


def test_corpus_counts_reflect_relationships(
    client: TestClient, temp_env: None
) -> None:
    """Seed a corpus + a book + a transcript + an artifact directly; the list
    endpoint's counts should pick them up."""
    from decimal import Decimal

    from data.models import (
        Artifact,
        ArtifactType,
        Book,
        CreatedBy,
        Transcript,
        TranscriptSourceType,
    )

    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Counting", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        cid = corpus.id
        session.add(
            Book(
                id="b" * 64,
                corpus_id=cid,
                title="t",
                source_pdf_path="/p.pdf",
                source_page_min=1,
                source_page_max=10,
            )
        )
        session.add(
            Transcript(
                id="t" * 64,
                corpus_id=cid,
                source_type=TranscriptSourceType.TEXT,
                raw_text="",
                cleaned_text="",
            )
        )
        session.add(
            Artifact(
                corpus_id=cid,
                type=ArtifactType.CASE_BRIEF,
                created_by=CreatedBy.SYSTEM,
                content={},
                sources=[],
                prompt_template="x",
                llm_model="y",
                cost_usd=Decimal("0"),
                cache_key="",
            )
        )
        session.commit()

    r = client.get(f"/corpora/{cid}")
    body = r.json()
    assert body["book_count"] == 1
    assert body["transcript_count"] == 1
    assert body["artifact_count"] == 1
    assert body["professor_profile_count"] == 0
