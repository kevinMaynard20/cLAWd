"""Integration tests for /uploads/*, /tasks/*, /ingest/book/async, /costs/daily."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data import db


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "c.enc"))
    db.reset_engine()
    db.init_schema()
    yield
    # Drain shared worker queue before reset so a mid-call worker doesn't
    # crash on a vanished engine.
    from features import tasks as tf

    tf.drain_for_tests(timeout=5.0)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# /uploads/pdf
# ---------------------------------------------------------------------------


def test_upload_pdf_streams_to_disk_and_dedupes(client: TestClient) -> None:
    fake_pdf = b"%PDF-1.4\n" + (b"x" * (2 * 1024 * 1024))  # 2 MiB synthetic
    files = {"files": ("casebook.pdf", io.BytesIO(fake_pdf), "application/pdf")}
    r1 = client.post("/uploads/pdf", files=files)
    assert r1.status_code == 200
    body = r1.json()
    assert len(body["files"]) == 1
    f = body["files"][0]
    assert f["size_bytes"] == len(fake_pdf)
    assert len(f["sha256"]) == 64
    stored1 = f["stored_path"]
    assert Path(stored1).exists()
    # Same upload again — should dedupe to the same stored path
    files = {"files": ("casebook.pdf", io.BytesIO(fake_pdf), "application/pdf")}
    r2 = client.post("/uploads/pdf", files=files)
    assert r2.json()["files"][0]["stored_path"] == stored1


def test_upload_pdf_rejects_non_pdf_extension(client: TestClient) -> None:
    files = {"files": ("notes.txt", io.BytesIO(b"hello"), "text/plain")}
    r = client.post("/uploads/pdf", files=files)
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_upload_pdf_no_files(client: TestClient) -> None:
    """No files in the multipart payload → 422 (FastAPI param validation)."""
    r = client.post("/uploads/pdf", files={})
    assert r.status_code == 422


def test_upload_pdf_multiple(client: TestClient) -> None:
    """Multiple files in one request."""
    payload = [
        ("files", ("a.pdf", io.BytesIO(b"%PDF-1.4\n" + b"a" * 1000), "application/pdf")),
        ("files", ("b.pdf", io.BytesIO(b"%PDF-1.4\n" + b"b" * 1000), "application/pdf")),
    ]
    r = client.post("/uploads/pdf", files=payload)
    assert r.status_code == 200
    body = r.json()
    assert len(body["files"]) == 2
    assert body["files"][0]["sha256"] != body["files"][1]["sha256"]


def test_upload_text_route(client: TestClient) -> None:
    """Plain text upload (transcript / memo) lands in storage/uploads/text/."""
    files = {"files": ("memo.md", io.BytesIO(b"# Memo\n\nbody"), "text/markdown")}
    r = client.post("/uploads/text", files=files)
    assert r.status_code == 200
    f = r.json()["files"][0]
    assert "/text/" in f["stored_path"]


# ---------------------------------------------------------------------------
# /tasks/* + /ingest/book/async
# ---------------------------------------------------------------------------


def test_task_404_on_unknown_id(client: TestClient) -> None:
    r = client.get("/tasks/nonexistent")
    assert r.status_code == 404


def test_async_book_ingest_schedules_task_and_polls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async ingest path returns 202 + a task_id; polling shows the task
    progress through to completion. We mock the underlying ingest_book so we
    don't need Marker."""
    from features import tasks as task_features
    from primitives import ingest as ingest_primitive

    fake_book_id = "f" * 64

    class _FakeBook:
        id = fake_book_id
        title = "Fake"
        corpus_id = "fake-corpus"
        source_page_min = 1
        source_page_max = 10

    def _fake_ingest_book(*_args, on_progress=None, **_kwargs):
        # Drive a few progress steps so the test exercises the callback.
        if on_progress is not None:
            on_progress("hashing", 1, 1)
            on_progress("marker", 5, 10)
            on_progress("persisting", 1, 1)
        return _FakeBook()

    monkeypatch.setattr(ingest_primitive, "ingest_book", _fake_ingest_book)

    # Patch the count queries to return zeros (no real persistence).
    from sqlmodel import Session, func, select

    from data.models import Block, Page

    r = client.post(
        "/ingest/book/async",
        json={"pdf_paths": ["/fake/path.pdf"], "title": "Fake Book"},
    )
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    assert r.json()["poll_url"] == f"/tasks/{task_id}"

    # Poll until task completes (the daemon thread runs concurrently).
    deadline = time.time() + 5.0
    final = None
    while time.time() < deadline:
        poll = client.get(f"/tasks/{task_id}")
        body = poll.json()
        if body["status"] in ("completed", "failed"):
            final = body
            break
        time.sleep(0.1)

    assert final is not None, "Task did not finish in time"
    assert final["status"] == "completed", final
    assert final["progress_pct"] == 1.0
    assert final["result"]["book_id"] == fake_book_id


def test_async_book_ingest_400_on_empty_paths(client: TestClient) -> None:
    r = client.post("/ingest/book/async", json={"pdf_paths": [], "title": "x"})
    # FastAPI's `min_length=1` triggers 422; either is OK.
    assert r.status_code in (400, 422)


def test_list_tasks_filters_by_status(client: TestClient) -> None:
    from features import tasks as task_features

    completed = task_features.schedule_task(
        kind=__import__("data.models", fromlist=["TaskKind"]).TaskKind.BOOK_INGESTION,
        inputs={},
    )
    task_features.mark_completed(completed, {"ok": True})

    r_all = client.get("/tasks")
    r_done = client.get("/tasks?status=completed")
    r_running = client.get("/tasks?status=running")
    assert r_all.json()["tasks"]
    assert any(t["id"] == completed for t in r_done.json()["tasks"])
    assert all(t["status"] == "running" for t in r_running.json()["tasks"])


def test_list_tasks_unknown_status_400(client: TestClient) -> None:
    r = client.get("/tasks?status=invented")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /costs/daily (Q20)
# ---------------------------------------------------------------------------


def test_costs_daily_returns_zero_filled_window(client: TestClient) -> None:
    r = client.get("/costs/daily?days_back=7")
    assert r.status_code == 200
    body = r.json()
    assert len(body["days"]) == 7
    # Each entry has YYYY-MM-DD shape and total_usd serialized as string.
    for d in body["days"]:
        assert len(d["date"]) == 10
        assert "-" in d["date"]


def test_costs_daily_sums_recorded_events(client: TestClient) -> None:
    from costs import tracker

    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=10_000,
        output_tokens=5_000,
        feature="case_brief",
    )
    body = client.get("/costs/daily?days_back=2").json()
    today_total = float(body["days"][-1]["total_usd"])
    # Opus: 10k * 15/1M + 5k * 75/1M = 0.15 + 0.375 = 0.525
    assert today_total > 0.5


def test_costs_daily_invalid_days_back_422(client: TestClient) -> None:
    r = client.get("/costs/daily?days_back=0")
    assert r.status_code == 422
