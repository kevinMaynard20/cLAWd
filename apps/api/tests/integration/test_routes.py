"""Integration tests for the FastAPI routes (spec §1.6).

Each endpoint is exercised via `TestClient` against a fresh in-memory schema.
External calls (Anthropic / Voyage) are mocked with `pytest-httpx`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock
from sqlmodel import Session

from credentials.validation import ANTHROPIC_MODELS_URL, VOYAGE_EMBEDDINGS_URL
from data import db
from data.models import Block, BlockType, Book, Corpus, Page


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "creds.enc"))
    db.reset_engine()
    db.init_schema()
    # Fresh session id per test so cost-totals tests are independent.
    from costs import tracker

    tracker.reset_session_id()
    yield
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# /health (sanity)
# ---------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /credentials/*
# ---------------------------------------------------------------------------


def test_credentials_status_empty(client: TestClient) -> None:
    r = client.get("/credentials/status")
    assert r.status_code == 200
    body = r.json()
    assert body["anthropic_present"] is False
    assert body["voyage_present"] is False
    assert body["anthropic_display"] is None


def test_credentials_llm_gate_disabled_without_key(client: TestClient) -> None:
    r = client.get("/credentials/gate")
    assert r.status_code == 200
    assert r.json()["llm_enabled"] is False


def test_credentials_store_anthropic_then_status(
    client: TestClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=ANTHROPIC_MODELS_URL, status_code=200, json={"data": []})
    r = client.post(
        "/credentials/anthropic",
        json={"key": "sk-ant-api03-FAKEKEY-1234567890-LASTFOUR"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["validation"]["state"] == "valid"
    assert body["display"].endswith("FOUR")
    assert "FAKEKEY" not in body["display"]

    # Status should now reflect stored key.
    r2 = client.get("/credentials/status")
    assert r2.json()["anthropic_present"] is True

    # Gate should flip to enabled.
    r3 = client.get("/credentials/gate")
    assert r3.json()["llm_enabled"] is True


def test_credentials_store_rejects_empty_key(client: TestClient) -> None:
    r = client.post("/credentials/anthropic", json={"key": "   "})
    assert r.status_code == 422 or r.status_code == 400  # pydantic min_length or ValueError


def test_credentials_store_anthropic_invalid_key(
    client: TestClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=ANTHROPIC_MODELS_URL, status_code=401)
    r = client.post(
        "/credentials/anthropic",
        json={"key": "sk-ant-api03-BADKEY-XXXXXXXXXXXXXXXXXXXXX"},
    )
    assert r.status_code == 200
    assert r.json()["validation"]["state"] == "invalid"
    # The key still got stored — spec §7.7.1 explicitly allows "save anyway, validate later".
    # The validation result in the response tells the UI to warn.


def test_credentials_clear_anthropic(client: TestClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=ANTHROPIC_MODELS_URL, status_code=200, json={"data": []})
    client.post("/credentials/anthropic", json={"key": "sk-ant-x-y-z-1234567890"})
    r = client.delete("/credentials/anthropic")
    assert r.status_code == 200
    assert r.json() == {"cleared": "anthropic"}
    assert client.get("/credentials/status").json()["anthropic_present"] is False


def test_credentials_test_without_stored_key(client: TestClient) -> None:
    r = client.post("/credentials/test", json={"provider": "anthropic"})
    assert r.status_code == 409
    assert "No Anthropic key stored" in r.json()["detail"]


def test_credentials_test_with_stored_key(
    client: TestClient, httpx_mock: HTTPXMock
) -> None:
    # First the store+validate call, then the explicit test call.
    httpx_mock.add_response(url=ANTHROPIC_MODELS_URL, status_code=200, json={"data": []})
    httpx_mock.add_response(url=ANTHROPIC_MODELS_URL, status_code=200, json={"data": []})
    client.post("/credentials/anthropic", json={"key": "sk-ant-xy-abcdefghijkl"})
    r = client.post("/credentials/test", json={"provider": "anthropic"})
    assert r.status_code == 200
    assert r.json()["state"] == "valid"


def test_credentials_voyage_roundtrip(
    client: TestClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=VOYAGE_EMBEDDINGS_URL, status_code=200, json={})
    r = client.post("/credentials/voyage", json={"key": "pa-voyage-fake-KEY123"})
    assert r.status_code == 200
    assert r.json()["validation"]["state"] == "valid"
    assert client.get("/credentials/status").json()["voyage_present"] is True


# ---------------------------------------------------------------------------
# /costs/*
# ---------------------------------------------------------------------------


def test_costs_session_empty(client: TestClient) -> None:
    r = client.get("/costs/session")
    assert r.status_code == 200
    body = r.json()
    assert body["total_usd"] in ("0", "0.00", "0E-10", "0E-9")  # Decimal serializations vary
    assert body["input_tokens"] == 0
    assert body["output_tokens"] == 0
    assert body["session_id"]


def test_costs_session_after_record(client: TestClient) -> None:
    from costs import tracker

    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=500,
        feature="case_brief",
    )
    r = client.get("/costs/session")
    body = r.json()
    # Decimal("15") * 1000 / 1M + Decimal("75") * 500 / 1M = 0.015 + 0.0375 = 0.0525
    assert float(body["total_usd"]) == pytest.approx(0.0525, rel=1e-6)
    assert body["input_tokens"] == 1000
    assert body["output_tokens"] == 500


def test_costs_lifetime(client: TestClient) -> None:
    from costs import tracker

    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=0,
        feature="a",
    )
    r = client.get("/costs/lifetime")
    assert r.status_code == 200
    assert float(r.json()["total_usd"]) > 0


def test_costs_features_breakdown(client: TestClient) -> None:
    from costs import tracker

    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=0,
        feature="brief",
    )
    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=2000,
        output_tokens=0,
        feature="grade",
    )
    r = client.get("/costs/features")
    body = r.json()
    assert "brief" in body["breakdown"]
    assert "grade" in body["breakdown"]


def test_costs_events_filter(client: TestClient) -> None:
    from costs import tracker

    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1,
        output_tokens=1,
        feature="a",
    )
    tracker.record_llm_call(
        model="claude-haiku-4-5",
        provider="anthropic",
        input_tokens=1,
        output_tokens=1,
        feature="b",
        cached=True,
    )
    all_events = client.get("/costs/events").json()
    assert all_events["count"] == 2
    # Filter cached=true
    cached_events = client.get("/costs/events?cached=true").json()
    assert cached_events["count"] == 1
    assert cached_events["events"][0]["cached"] is True
    # Filter by feature
    by_feature = client.get("/costs/events?feature=a").json()
    assert by_feature["count"] == 1


def test_costs_budget_off_by_default(client: TestClient) -> None:
    r = client.get("/costs/budget")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "off"
    assert body["cap_usd"] is None


def test_costs_budget_with_cap_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "10.00")
    from costs import tracker

    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=1000,
        feature="case_brief",
    )
    r = client.get("/costs/budget")
    body = r.json()
    assert body["state"] in ("ok", "warning", "exceeded")
    assert float(body["cap_usd"]) == 10.0


def test_costs_reset_session_rotates_id(client: TestClient) -> None:
    old_id = client.get("/costs/session").json()["session_id"]
    r = client.post("/costs/reset-session")
    new_id = r.json()["session_id"]
    assert old_id != new_id
    # /costs/session now reports the new id
    assert client.get("/costs/session").json()["session_id"] == new_id


def test_costs_export_csv(client: TestClient) -> None:
    from costs import tracker

    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=100,
        output_tokens=50,
        feature="case_brief",
    )
    r = client.get("/costs/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.text
    assert "id,timestamp,session_id,model," in text
    assert "case_brief" in text


# ---------------------------------------------------------------------------
# /retrieve
# ---------------------------------------------------------------------------


def _seed_minimal_book(temp_env: None) -> str:
    engine = db.get_engine()
    book_id = "d" * 64
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        corpus_id = corpus.id
        book = Book(
            id=book_id,
            corpus_id=corpus_id,
            title="t",
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=3,
        )
        session.add(book)
        session.commit()
        p = Page(
            book_id=book_id,
            source_page=1,
            batch_pdf="b.pdf",
            pdf_page_start=1,
            pdf_page_end=2,
            markdown="# one",
            raw_text="one",
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        page_id = p.id
        session.add(
            Block(
                page_id=page_id,
                book_id=book_id,
                order_index=0,
                type=BlockType.NARRATIVE_TEXT,
                source_page=1,
                markdown="hello",
                block_metadata={},
            )
        )
        session.commit()
    return book_id


def test_retrieve_page_range(client: TestClient, temp_env: None) -> None:
    book_id = _seed_minimal_book(temp_env)
    r = client.post(
        "/retrieve",
        json={"type": "page_range", "book_id": book_id, "start": 1, "end": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["pages"]) == 1
    assert body["pages"][0]["source_page"] == 1
    assert len(body["blocks"]) == 1
    assert body["blocks"][0]["markdown"] == "hello"


def test_retrieve_page_range_invalid_start_gt_end(
    client: TestClient, temp_env: None
) -> None:
    book_id = _seed_minimal_book(temp_env)
    r = client.post(
        "/retrieve",
        json={"type": "page_range", "book_id": book_id, "start": 10, "end": 1},
    )
    assert r.status_code == 400
    assert "start" in r.json()["detail"].lower()


def test_retrieve_semantic_stub(client: TestClient, temp_env: None) -> None:
    r = client.post(
        "/retrieve",
        json={"type": "semantic", "corpus_id": "c1", "text": "state action"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["blocks"] == []
    assert any("Phase 1" in n or "voyage" in n.lower() for n in body["notes"])


# ---------------------------------------------------------------------------
# /ingest/book
# ---------------------------------------------------------------------------


def test_ingest_book_route_happy_path(
    client: TestClient,
    temp_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full route exercise: mock Marker, POST /ingest/book, assert the
    response body has the shape the web client will consume."""
    from primitives import marker_runner

    # Redirect the Marker cache to a tmp dir so the test doesn't touch
    # repo storage/marker_raw/.
    monkeypatch.setenv("LAWSCHOOL_MARKER_CACHE_DIR", str(tmp_path / "marker_cache"))

    def fake_impl(
        pdf_path: Path, *, use_llm: bool, extract_images: bool
    ) -> marker_runner.MarkerResult:
        md = "\n".join(
            [
                "# Introduction",
                "",
                "1",
                "",
                "Body of page 1.",
                "",
                "2",
                "",
                "Body of page 2.",
            ]
        )
        return marker_runner.MarkerResult(
            markdown=md, pdf_page_count=2, pdf_page_offsets=[0, len(md) // 2]
        )

    monkeypatch.setattr(marker_runner, "_run_marker_impl", fake_impl)

    # Write a real file at the path so the primitive's existence check passes.
    pdf_path = tmp_path / "real.pdf"
    pdf_path.write_bytes(b"%PDF-ROUTE\n")

    r = client.post(
        "/ingest/book",
        json={"pdf_paths": [str(pdf_path)], "title": "Route Test Book"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["book_id"]) == 64  # SHA-256 hex
    assert body["title"] == "Route Test Book"
    assert body["page_count"] >= 2
    assert body["block_count"] >= 1
    assert body["was_cached"] is False
    assert body["source_page_min"] == 1
    assert body["source_page_max"] == 2


def test_ingest_book_route_marker_unavailable_falls_back_to_pymupdf4llm(
    client: TestClient,
    temp_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §4.1.1 sanctions a PyMuPDF4LLM fallback when Marker isn't
    available. With Marker patched out, the ingest route should return 200
    and the resulting Book's ingestion_method should reflect the fallback —
    not 503. (We mock the fallback runner too so this test stays
    hermetic + fast and doesn't actually invoke pymupdf on disk.)
    """
    from primitives import marker_runner, pymupdf4llm_runner

    monkeypatch.setenv("LAWSCHOOL_MARKER_CACHE_DIR", str(tmp_path / "marker_cache"))

    def missing_impl(*_a: object, **_k: object) -> marker_runner.MarkerResult:
        raise ImportError("No module named 'marker'")

    def fake_pymupdf(
        _pdf_path: Path, *, on_page=None
    ) -> marker_runner.MarkerResult:
        md = "\n".join(
            ["# Intro", "", "1", "", "Body of 1.", "", "2", "", "Body of 2."]
        )
        if on_page is not None:
            on_page(2, 2)
        return marker_runner.MarkerResult(
            markdown=md, pdf_page_count=2, pdf_page_offsets=[0, len(md) // 2]
        )

    monkeypatch.setattr(marker_runner, "_run_marker_impl", missing_impl)
    monkeypatch.setattr(
        pymupdf4llm_runner, "run_pymupdf4llm_cached", fake_pymupdf
    )

    pdf_path = tmp_path / "real.pdf"
    pdf_path.write_bytes(b"%PDF-FALLBACK\n")

    r = client.post(
        "/ingest/book",
        json={"pdf_paths": [str(pdf_path)], "title": "Fallback Book"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Fallback Book"
    assert body["page_count"] >= 2

    # Verify the persisted Book was tagged with the fallback engine.
    from data import db
    from data.models import Book, IngestionMethod
    from sqlmodel import Session, select

    with Session(db.get_engine()) as session:
        book = session.exec(select(Book).where(Book.id == body["book_id"])).one()
        assert book.ingestion_method is IngestionMethod.PYMUPDF4LLM


def test_ingest_book_route_returns_503_when_both_engines_fail(
    client: TestClient,
    temp_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both Marker AND PyMuPDF4LLM are unavailable, the route surfaces a
    503 with the install hint — that's the only path left to the user."""
    from primitives import marker_runner, pymupdf4llm_runner

    monkeypatch.setenv("LAWSCHOOL_MARKER_CACHE_DIR", str(tmp_path / "marker_cache"))

    def missing_marker(*_a: object, **_k: object) -> marker_runner.MarkerResult:
        raise ImportError("No module named 'marker'")

    def missing_pymupdf(
        _pdf_path: Path, *, on_page=None
    ) -> marker_runner.MarkerResult:
        raise marker_runner.MarkerNotInstalledError(
            "PyMuPDF4LLM fallback unavailable"
        )

    monkeypatch.setattr(marker_runner, "_run_marker_impl", missing_marker)
    monkeypatch.setattr(
        pymupdf4llm_runner, "run_pymupdf4llm_cached", missing_pymupdf
    )

    pdf_path = tmp_path / "real.pdf"
    pdf_path.write_bytes(b"%PDF-NEITHER\n")

    r = client.post(
        "/ingest/book",
        json={"pdf_paths": [str(pdf_path)], "title": "Property"},
    )
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["status"] == "marker_not_installed"
    assert "install_command" in detail
