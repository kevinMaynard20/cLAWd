"""Integration tests for /corpora/{id}/export and /artifacts/{id}/lineage."""

from __future__ import annotations

import io
import json
import tarfile
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
    CostEvent,
    CreatedBy,
    Page,
    Provider,
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
def seeded(temp_env: None) -> dict[str, str]:
    engine = db.get_engine()
    with Session(engine) as session:
        c = Corpus(name="Property", course="Property")
        session.add(c)
        session.commit()
        session.refresh(c)

        book = Book(
            id="b" * 64,
            corpus_id=c.id,
            title="Property",
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
            block_metadata={"case_name": "Test v. Case"},
        )
        session.add(block)
        session.commit()
        session.refresh(block)

        artifact = Artifact(
            corpus_id=c.id,
            type=ArtifactType.CASE_BRIEF,
            created_by=CreatedBy.SYSTEM,
            content={"case_name": "Test v. Case"},
            sources=[{"kind": "block", "id": block.id}],
            prompt_template="case_brief@1.2.0",
            llm_model="claude-opus-4-7",
            cost_usd=Decimal("0.05"),
            cache_key="k",
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)

        session.add(
            CostEvent(
                session_id="s",
                model="claude-opus-4-7",
                provider=Provider.ANTHROPIC,
                input_tokens=100,
                output_tokens=50,
                total_cost_usd=Decimal("0.05"),
                feature="case_brief",
                artifact_id=artifact.id,
            )
        )
        session.commit()

        return {"corpus_id": c.id, "artifact_id": artifact.id, "block_id": block.id}


# ---------------------------------------------------------------------------
# /corpora/{id}/export
# ---------------------------------------------------------------------------


def test_route_export_corpus_happy_path(client: TestClient, seeded: dict) -> None:
    r = client.get(f"/corpora/{seeded['corpus_id']}/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert "attachment" in r.headers["content-disposition"]
    assert ".tar.gz" in r.headers["content-disposition"]

    # The body is a valid gzipped tar containing manifest.json
    archive = r.content
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "manifest.json" in names
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert manifest["corpus_id"] == seeded["corpus_id"]


def test_route_export_unknown_corpus_404(client: TestClient, temp_env: None) -> None:
    r = client.get("/corpora/nonexistent/export")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /artifacts/{id}/lineage
# ---------------------------------------------------------------------------


def test_route_lineage_happy_path(client: TestClient, seeded: dict) -> None:
    r = client.get(f"/artifacts/{seeded['artifact_id']}/lineage")
    assert r.status_code == 200
    body = r.json()
    assert body["target_artifact_id"] == seeded["artifact_id"]
    assert len(body["chain"]) == 1
    node = body["chain"][0]
    assert node["id"] == seeded["artifact_id"]
    assert node["type"] == "case_brief"
    # The sources summary resolves the cited block
    assert any(
        s["id"] == seeded["block_id"] and s["found"] is True
        for s in node["sources_summary"]
    )
    # SQLAlchemy Numeric(20,10) text-encodes Decimals; compare numerically.
    assert float(body["total_cost_usd"]) == pytest.approx(0.05)


def test_route_lineage_unknown_artifact_404(client: TestClient, temp_env: None) -> None:
    r = client.get("/artifacts/nonexistent-id/lineage")
    assert r.status_code == 404


def test_route_lineage_includes_events(client: TestClient, seeded: dict) -> None:
    r = client.get(f"/artifacts/{seeded['artifact_id']}/lineage")
    body = r.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["feature"] == "case_brief"
    assert body["events"][0]["model"] == "claude-opus-4-7"
