"""Tests for features/tasks.py — background-task lifecycle."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlmodel import Session, select

from data import db
from data.models import BackgroundTask, TaskKind, TaskStatus
from features import tasks as task_features


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "t.db"))
    db.reset_engine()
    db.init_schema()
    yield
    task_features.drain_for_tests(timeout=5.0)
    db.reset_engine()


def test_schedule_task_creates_pending_row(temp_db: None) -> None:
    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION,
        inputs={"pdf_paths": ["/x"]},
    )
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row is not None
    assert row.status is TaskStatus.PENDING
    assert row.kind is TaskKind.BOOK_INGESTION
    assert row.inputs_json == {"pdf_paths": ["/x"]}
    assert row.progress_pct == 0.0


def test_schedule_task_with_corpus_id(temp_db: None) -> None:
    from data.models import Corpus

    with Session(db.get_engine()) as s:
        c = Corpus(name="x", course="x")
        s.add(c)
        s.commit()
        s.refresh(c)
        cid = c.id

    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION,
        inputs={},
        corpus_id=cid,
    )
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.corpus_id == cid


def test_mark_completed_sets_status_and_result(temp_db: None) -> None:
    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION, inputs={}
    )
    task_features.mark_completed(task_id, {"book_id": "b" * 64, "page_count": 42})
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.status is TaskStatus.COMPLETED
    assert row.progress_pct == 1.0
    assert row.result_json["book_id"] == "b" * 64
    assert row.finished_at is not None


def test_mark_failed_records_error(temp_db: None) -> None:
    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION, inputs={}
    )
    task_features.mark_failed(task_id, "MarkerNotInstalledError: install marker-pdf")
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.status is TaskStatus.FAILED
    assert "MarkerNotInstalled" in (row.error or "")


def test_progress_callback_maps_steps_to_buckets(temp_db: None) -> None:
    """Spec on progress mapping: each named step occupies a non-overlapping
    range of [0, 1] so the bar moves smoothly."""
    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION, inputs={}
    )
    cb = task_features._make_progress_callback(task_id)

    cb("hashing", 1, 1)
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.progress_step == "hashing"
    assert 0.0 <= row.progress_pct <= 0.06

    cb("marker", 50, 100)
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.progress_step == "marker"
    assert 0.25 < row.progress_pct < 0.40   # halfway through marker bucket

    cb("persisting", 1, 1)
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.progress_step == "persisting"
    assert row.progress_pct == 1.0


def test_progress_callback_clamps_to_unit_interval(temp_db: None) -> None:
    """Wild current/total values shouldn't push pct out of [0, 1]."""
    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION, inputs={}
    )
    cb = task_features._make_progress_callback(task_id)
    cb("marker", 10_000, 100)  # 100x overshoot
    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert 0.0 <= row.progress_pct <= 1.0


def test_start_thread_runs_and_completes(temp_db: None) -> None:
    """Smoke: a trivial fn schedules, runs in a daemon thread, completes."""
    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION, inputs={}
    )

    def _fn() -> None:
        task_features.mark_completed(task_id, {"ok": True})

    task_features._start_thread(task_id, _fn)

    # Poll up to ~2s for completion
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with Session(db.get_engine()) as s:
            row = s.get(BackgroundTask, task_id)
        if row.status is TaskStatus.COMPLETED:
            break
        time.sleep(0.05)

    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.status is TaskStatus.COMPLETED
    assert row.result_json == {"ok": True}


def test_start_thread_marks_failed_on_exception(temp_db: None) -> None:
    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION, inputs={}
    )

    def _bad() -> None:
        raise ValueError("kaboom")

    task_features._start_thread(task_id, _bad)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        with Session(db.get_engine()) as s:
            row = s.get(BackgroundTask, task_id)
        if row.status is TaskStatus.FAILED:
            break
        time.sleep(0.05)

    with Session(db.get_engine()) as s:
        row = s.get(BackgroundTask, task_id)
    assert row.status is TaskStatus.FAILED
    assert "kaboom" in (row.error or "")
