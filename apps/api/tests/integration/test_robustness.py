"""Robustness tests: bounded task queue, cancellation, upload caps, health."""

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
    # Drain the shared worker queue so a previous test's in-flight task
    # doesn't crash the next test's `db.reset_engine()`. `drain_for_tests`
    # waits for `task_done()` to fire for every dispatched item — that's
    # called AFTER the worker's session_scope closes, so it's safe to reset.
    from features import tasks as tf

    tf.drain_for_tests(timeout=5.0)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Upload size limits + disk pre-flight
# ---------------------------------------------------------------------------


def test_upload_pdf_rejects_oversize_with_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap at 2 MiB, send a 3 MiB file → 413."""
    monkeypatch.setenv("LAWSCHOOL_MAX_PDF_BYTES", str(2 * 1024 * 1024))
    too_big = b"%PDF-1.4\n" + (b"x" * (3 * 1024 * 1024))
    r = client.post(
        "/uploads/pdf",
        files={"files": ("big.pdf", io.BytesIO(too_big), "application/pdf")},
    )
    assert r.status_code == 413
    assert "MiB" in r.json()["detail"]


def test_upload_pdf_at_cap_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File at the cap (not over) is accepted."""
    monkeypatch.setenv("LAWSCHOOL_MAX_PDF_BYTES", str(2 * 1024 * 1024))
    at_cap = b"%PDF-1.4\n" + (b"y" * (2 * 1024 * 1024 - 9))
    r = client.post(
        "/uploads/pdf",
        files={"files": ("ok.pdf", io.BytesIO(at_cap), "application/pdf")},
    )
    assert r.status_code == 200


def test_upload_text_has_separate_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAWSCHOOL_MAX_TEXT_BYTES cap is independent of PDF cap."""
    monkeypatch.setenv("LAWSCHOOL_MAX_TEXT_BYTES", str(64 * 1024))
    text = b"a" * (128 * 1024)
    r = client.post(
        "/uploads/text",
        files={"files": ("transcript.txt", io.BytesIO(text), "text/plain")},
    )
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# Task cancellation
# ---------------------------------------------------------------------------


def test_cancel_pending_task_succeeds(client: TestClient) -> None:
    """Schedule a task, cancel it before the worker runs, confirm CANCELLED."""
    from features import tasks as tf
    from data.models import TaskKind

    task_id = tf.schedule_task(kind=TaskKind.BOOK_INGESTION, inputs={"x": 1})
    r = client.post(f"/tasks/{task_id}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True

    poll = client.get(f"/tasks/{task_id}")
    assert poll.json()["status"] == "cancelled"


def test_cancel_404_on_unknown_task(client: TestClient) -> None:
    r = client.post("/tasks/nonexistent/cancel")
    assert r.status_code == 404


def test_cancel_completed_task_is_no_op(client: TestClient) -> None:
    """Already-completed tasks return cancelled=False with a clarifying message."""
    from features import tasks as tf
    from data.models import TaskKind

    task_id = tf.schedule_task(kind=TaskKind.BOOK_INGESTION, inputs={})
    tf.mark_completed(task_id, {"ok": True})

    r = client.post(f"/tasks/{task_id}/cancel")
    body = r.json()
    assert body["cancelled"] is False
    assert "completed" in body["message"]


def test_cancellation_observed_by_running_worker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: worker checks status at every progress checkpoint. We simulate a
    long-running task that bumps progress in a loop; cancel mid-flight; the
    next checkpoint raises TaskCancelled and the row flips to CANCELLED."""
    from features import tasks as tf
    from data.models import TaskKind

    task_id = tf.schedule_task(kind=TaskKind.BOOK_INGESTION, inputs={})

    cancellation_signal = {"flag": False, "iterations_after_cancel": 0}

    def long_running_work() -> None:
        on_progress = tf._make_progress_callback(task_id)
        for i in range(50):
            on_progress("marker", i + 1, 50)
            if cancellation_signal["flag"]:
                cancellation_signal["iterations_after_cancel"] += 1
            time.sleep(0.02)
        tf.mark_completed(task_id, {"reached_end": True})

    tf._start_thread(task_id, long_running_work)

    # Let the worker get going
    time.sleep(0.05)
    r = client.post(f"/tasks/{task_id}/cancel")
    assert r.status_code == 200
    cancellation_signal["flag"] = True

    # Wait up to 2s for cancellation to take effect
    deadline = time.time() + 2.0
    while time.time() < deadline:
        poll = client.get(f"/tasks/{task_id}").json()
        if poll["status"] == "cancelled":
            break
        time.sleep(0.05)

    final = client.get(f"/tasks/{task_id}").json()
    assert final["status"] == "cancelled", final
    # The worker should have stopped within a few iterations of the cancel.
    # Allow a generous bound; the point is "much less than 50".
    assert cancellation_signal["iterations_after_cancel"] < 25


# ---------------------------------------------------------------------------
# Bounded task queue — many tasks queue up, only one runs at a time
# ---------------------------------------------------------------------------


def test_bounded_concurrency_processes_tasks_in_order(client: TestClient) -> None:
    """Schedule 5 trivial tasks; the (default-1) worker processes them all to
    completion. We're not asserting strict serialization here — that depends
    on worker count — just that all 5 land in COMPLETED."""
    from features import tasks as tf
    from data.models import TaskKind

    task_ids: list[str] = []
    for i in range(5):
        tid = tf.schedule_task(kind=TaskKind.BOOK_INGESTION, inputs={"i": i})
        task_ids.append(tid)
        tf._start_thread(tid, lambda t=tid, n=i: tf.mark_completed(t, {"i": n}))

    # Wait up to 5s for all to complete
    deadline = time.time() + 5.0
    while time.time() < deadline:
        statuses = [client.get(f"/tasks/{tid}").json()["status"] for tid in task_ids]
        if all(s == "completed" for s in statuses):
            break
        time.sleep(0.1)

    final = [client.get(f"/tasks/{tid}").json()["status"] for tid in task_ids]
    assert final == ["completed"] * 5


def test_queue_depth_visible_via_health(client: TestClient) -> None:
    """The /system/health endpoint reports queue depth + worker count."""
    r = client.get("/system/health")
    assert r.status_code == 200
    body = r.json()
    assert "workers" in body
    assert body["workers"]["concurrency"] >= 1
    assert isinstance(body["workers"]["queue_depth"], int)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_system_health_returns_diagnostics(client: TestClient) -> None:
    r = client.get("/system/health")
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "storage" in body
    assert body["storage"]["total_bytes"] > 0
    assert "marker_available" in body
    # Marker isn't installed in the test env — should report False
    assert body["marker_available"] is False
    assert body["counts"]["corpora"] == 0


def test_system_health_shows_counts_after_seed(
    client: TestClient, temp_env: None
) -> None:
    from sqlmodel import Session

    from data.models import Corpus

    with Session(db.get_engine()) as s:
        s.add(Corpus(name="x", course="x"))
        s.commit()

    r = client.get("/system/health")
    body = r.json()
    assert body["counts"]["corpora"] == 1


# ---------------------------------------------------------------------------
# Storage cleanup (orphan GC)
# ---------------------------------------------------------------------------


def test_storage_cleanup_dry_run_reports_orphans(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Seed an orphan upload by hand; cleanup with dry_run=true reports it
    but does not delete."""
    # Find the storage/uploads dir
    from routes.system import _repo_root

    uploads = _repo_root() / "storage" / "uploads" / "pdf"
    uploads.mkdir(parents=True, exist_ok=True)
    orphan_path = uploads / ("a" * 64 + ".pdf")
    orphan_path.write_bytes(b"%PDF-1.4 orphan content")
    try:
        r_dry = client.post("/system/storage/cleanup?dry_run=true")
        assert r_dry.status_code == 200
        body = r_dry.json()
        assert body["dry_run"] is True
        assert body["deleted"] >= 1
        # File still on disk
        assert orphan_path.exists()

        r_real = client.post("/system/storage/cleanup?dry_run=false")
        assert r_real.status_code == 200
        # Now gone
        assert not orphan_path.exists()
    finally:
        if orphan_path.exists():
            orphan_path.unlink()


def test_storage_cleanup_keeps_referenced_uploads(
    client: TestClient, temp_env: None
) -> None:
    """A file whose sha matches a Book.batch_hashes entry must not be
    deleted."""
    from sqlmodel import Session

    from data.models import Book, Corpus
    from routes.system import _repo_root

    uploads = _repo_root() / "storage" / "uploads" / "pdf"
    uploads.mkdir(parents=True, exist_ok=True)
    sha = "b" * 64
    keeper = uploads / f"{sha}.pdf"
    keeper.write_bytes(b"%PDF-1.4 referenced")
    try:
        with Session(db.get_engine()) as s:
            c = Corpus(name="x", course="x")
            s.add(c)
            s.commit()
            s.refresh(c)
            s.add(
                Book(
                    id="c" * 64,
                    corpus_id=c.id,
                    title="t",
                    source_pdf_path=str(keeper),
                    batch_hashes=[sha],
                    source_page_min=1,
                    source_page_max=10,
                )
            )
            s.commit()

        r = client.post("/system/storage/cleanup?dry_run=false")
        body = r.json()
        # Keeper preserved
        assert keeper.exists()
        # And reported as kept (kept >= 1)
        assert body["kept"] >= 1
    finally:
        if keeper.exists():
            keeper.unlink()
