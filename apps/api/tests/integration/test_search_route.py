"""Integration test for GET /search (spec §5.14)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

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
def seeded(temp_env: None) -> str:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        cid = corpus.id

        book = Book(
            id="b" * 64,
            corpus_id=cid,
            title="Property",
            source_pdf_path="/p.pdf",
            source_page_min=500,
            source_page_max=550,
        )
        session.add(book)
        session.commit()
        page = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="b.pdf",
            pdf_page_start=1,
            pdf_page_end=2,
            markdown="m",
            raw_text="r",
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
                markdown="state action doctrine applies here",
                block_metadata={"case_name": "Shelley v. Kraemer"},
            )
        )
        session.add(
            Artifact(
                corpus_id=cid,
                type=ArtifactType.CASE_BRIEF,
                created_by=CreatedBy.SYSTEM,
                content={"case_name": "Shelley v. Kraemer", "rule": {"text": "state action"}},
                prompt_template="case_brief@1.2.0",
                llm_model="claude-opus-4-7",
                cost_usd=Decimal("0"),
                cache_key="k",
            )
        )
        session.commit()
        return cid


def test_search_route_returns_results(client: TestClient, seeded: str) -> None:
    r = client.get(f"/search?q=state%20action&corpus_id={seeded}")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "state action"
    assert body["count"] >= 1
    kinds = {res["kind"] for res in body["results"]}
    assert kinds.intersection({"block", "artifact"})


def test_search_route_missing_query_400(client: TestClient) -> None:
    r = client.get("/search")
    assert r.status_code == 422  # FastAPI param validation


def test_search_route_kinds_filter(client: TestClient, seeded: str) -> None:
    r = client.get(f"/search?q=state%20action&corpus_id={seeded}&kinds=block")
    body = r.json()
    assert all(res["kind"] == "block" for res in body["results"])


def test_search_route_limit(client: TestClient, seeded: str) -> None:
    r = client.get(f"/search?q=state%20action&corpus_id={seeded}&limit=1")
    body = r.json()
    assert body["count"] <= 1
