"""Background-task execution + lifecycle (spec §6 large-file polish).

Long-running operations like book ingestion (Marker can take 5+ minutes per
100 pages, and Marker holds substantial RAM during processing) can't fit
inside an HTTP request *and* must not run unbounded in parallel — a user
queueing 20 casebook ingestions would OOM the box.

Pattern:

  POST /ingest/book/async  -> creates BackgroundTask(status=PENDING)
                              and pushes the work onto an internal queue
  worker pool (N=1 by default; LAWSCHOOL_TASK_CONCURRENCY env)
                              pulls one task at a time, flips it to RUNNING,
                              runs it with an `on_progress` callback
  POST /tasks/{id}/cancel   -> sets status=CANCELLED; the worker checks
                              cooperatively at every progress callback
  GET  /tasks/{id}          -> client polls; reports pct + step + final result

SQLite WAL handles read-while-write so the polling endpoint stays responsive
while the worker is mid-step.
"""

from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlmodel import Session

from data.db import session_scope
from data.models import BackgroundTask, TaskKind, TaskStatus

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Worker pool — bounded concurrency via a single worker queue
# ---------------------------------------------------------------------------


def _concurrency() -> int:
    """How many ingestion-class tasks may run at once. Default 1 — Marker is
    memory-heavy and the user's machine is one laptop. Override via env."""
    raw = os.environ.get("LAWSCHOOL_TASK_CONCURRENCY", "").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 1
    return max(1, min(n, 8))   # cap at 8 even if env is silly


_pool_lock = threading.Lock()
_pool_started = False
_workers: list[threading.Thread] = []
_task_queue: queue.Queue[tuple[str, Callable[[], None]] | None] = queue.Queue()


class TaskCancelled(Exception):
    """Raised by the cooperative cancellation check inside a running task."""


def _bump_progress(task_id: str, step: str, pct: float) -> None:
    """Per-step progress write — and a cooperative cancellation checkpoint.

    Inside its own session_scope so the polling HTTP read sees fresh data
    while the worker is mid-call. If the row's status flipped to CANCELLED
    (via `cancel_task()`), we raise `TaskCancelled` so the worker bails out
    cleanly between steps."""
    with session_scope() as s:
        row = s.get(BackgroundTask, task_id)
        if row is None:
            return
        if row.status is TaskStatus.CANCELLED:
            raise TaskCancelled(f"Task {task_id} cancelled by user")
        row.progress_step = step
        row.progress_pct = max(0.0, min(1.0, float(pct)))
        s.add(row)


def _make_progress_callback(task_id: str) -> Callable[[str, int, int], None]:
    """Convert ingest's `on_progress(step, current, total)` callback shape into
    a BackgroundTask progress write. Steps from `primitives.ingest.ingest_book`
    are ordered roughly: hashing < marker < stitching < page_markers < pages <
    blocks < toc < persisting. We map step name → an estimated bucket so the
    bar moves smoothly even when total isn't known."""

    # Rough proportions of total ingestion time, based on how much work each
    # step does in the typical Marker-with-LLM run.
    step_floor = {
        "hashing": 0.00,
        "marker": 0.05,
        "stitching": 0.55,
        "page_markers": 0.60,
        "pages": 0.65,
        "blocks": 0.70,
        "toc": 0.92,
        "persisting": 0.95,
    }
    step_ceiling = {
        "hashing": 0.05,
        "marker": 0.55,
        "stitching": 0.60,
        "page_markers": 0.65,
        "pages": 0.70,
        "blocks": 0.92,
        "toc": 0.95,
        "persisting": 1.00,
    }

    def _on(step: str, current: float, total: int) -> None:
        floor = step_floor.get(step, 0.0)
        ceil = step_ceiling.get(step, 1.0)
        if total <= 0:
            pct = floor
        else:
            inner = max(0.0, min(1.0, current / total))
            pct = floor + (ceil - floor) * inner
        _bump_progress(task_id, step, pct)

    return _on


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_pool() -> None:
    """Lazily start the worker threads. Safe to call repeatedly."""
    global _pool_started
    with _pool_lock:
        if _pool_started:
            return
        n = _concurrency()
        for i in range(n):
            t = threading.Thread(
                target=_worker_loop, name=f"task-worker-{i}", daemon=True
            )
            t.start()
            _workers.append(t)
        _pool_started = True
        log.info("task_pool_started", concurrency=n)


def _run_one(task_id: str, fn: Callable[[], None]) -> None:
    """Execute one queued task. Marks RUNNING/COMPLETED/FAILED/CANCELLED. The
    fn() must call `mark_completed()` itself on success — we don't infer
    completion here because some flows want to set custom result_json."""
    # Status-check pre-run: if cancelled while still queued, skip cleanly.
    with session_scope() as s:
        row = s.get(BackgroundTask, task_id)
        if row is None:
            log.warning("task_runner_missing_row", task_id=task_id)
            return
        if row.status is TaskStatus.CANCELLED:
            log.info("task_skipped_cancelled_in_queue", task_id=task_id)
            row.finished_at = _utcnow()
            s.add(row)
            return
        row.status = TaskStatus.RUNNING
        row.started_at = _utcnow()
        s.add(row)

    try:
        fn()
    except TaskCancelled as exc:
        log.info("task_cancelled", task_id=task_id, reason=str(exc))
        with session_scope() as s:
            row = s.get(BackgroundTask, task_id)
            if row is not None:
                row.status = TaskStatus.CANCELLED
                row.error = str(exc)[:2000]
                row.finished_at = _utcnow()
                s.add(row)
    except Exception as exc:  # noqa: BLE001 — top-level boundary
        log.exception("task_runner_failed", task_id=task_id)
        with session_scope() as s:
            row = s.get(BackgroundTask, task_id)
            if row is not None:
                row.status = TaskStatus.FAILED
                row.error = f"{type(exc).__name__}: {exc}"[:2000]
                row.finished_at = _utcnow()
                s.add(row)


def _worker_loop() -> None:
    """Forever: pull a (task_id, fn) tuple from the queue and run it. A
    sentinel `None` value lets tests signal shutdown."""
    while True:
        item = _task_queue.get()
        if item is None:
            _task_queue.task_done()
            return
        task_id, fn = item
        try:
            _run_one(task_id, fn)
        finally:
            _task_queue.task_done()


def _enqueue(task_id: str, fn: Callable[[], None]) -> None:
    """Public submission helper. Boots the pool on first use."""
    _ensure_pool()
    _task_queue.put((task_id, fn))


# Backwards-compat alias: `_start_thread` was the old per-task daemon
# launcher; existing tests still call it directly. Now it's a queue submit.
def _start_thread(task_id: str, fn: Callable[[], None]) -> None:
    _enqueue(task_id, fn)


# ---------------------------------------------------------------------------
# Public API — schedule a task
# ---------------------------------------------------------------------------


def schedule_task(
    *,
    kind: TaskKind,
    inputs: dict[str, Any],
    corpus_id: str | None = None,
) -> str:
    """Create the BackgroundTask row in PENDING and return its id. The caller
    then invokes `start_book_ingestion_task` (or peer) to actually launch.
    Splitting create vs start lets the route synchronously return a task_id
    even if the worker pool is full."""
    task = BackgroundTask(
        kind=kind,
        status=TaskStatus.PENDING,
        corpus_id=corpus_id,
        inputs_json=inputs,
    )
    with session_scope() as s:
        s.add(task)
        s.commit()
        s.refresh(task)
        task_id = task.id
    return task_id


def get_task(session: Session, task_id: str) -> BackgroundTask | None:
    return session.get(BackgroundTask, task_id)


def mark_completed(task_id: str, result: dict[str, Any]) -> None:
    with session_scope() as s:
        row = s.get(BackgroundTask, task_id)
        if row is None:
            return
        row.status = TaskStatus.COMPLETED
        row.progress_pct = 1.0
        row.result_json = result
        row.finished_at = _utcnow()
        s.add(row)


def mark_failed(task_id: str, error: str) -> None:
    with session_scope() as s:
        row = s.get(BackgroundTask, task_id)
        if row is None:
            return
        row.status = TaskStatus.FAILED
        row.error = error[:2000]
        row.finished_at = _utcnow()
        s.add(row)


def cancel_task(task_id: str) -> bool:
    """Cooperative cancel. Sets status=CANCELLED so the worker bails out at
    its next progress checkpoint. Returns True if a transition happened.

    Already-completed / already-failed tasks are no-ops (return False)."""
    with session_scope() as s:
        row = s.get(BackgroundTask, task_id)
        if row is None:
            return False
        if row.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False
        row.status = TaskStatus.CANCELLED
        s.add(row)
        return True


def queue_depth() -> int:
    """How many tasks are waiting for a worker. Drives the dashboard's
    "queued" count."""
    return _task_queue.qsize()


def worker_count() -> int:
    """Currently-alive worker threads. Drives the health endpoint."""
    return sum(1 for t in _workers if t.is_alive())


def drain_for_tests(timeout: float = 5.0) -> bool:
    """Test helper: wait until the queue is empty AND every dispatched task
    has actually finished (workers may be mid-call when the queue empties).
    Returns True on clean drain, False on timeout. Lets fixtures avoid
    `db.reset_engine()` while a worker is mid-session_scope."""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if _task_queue.unfinished_tasks == 0:
            return True
        _time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Specific task launchers
# ---------------------------------------------------------------------------


def start_book_ingestion_task(
    task_id: str,
    *,
    pdf_paths: list[str],
    title: str | None,
    edition: str | None,
    authors: list[str],
    corpus_id: str | None,
    use_llm: bool,
) -> None:
    """Launch the worker thread that runs `ingest_book` with progress reporting."""
    from pathlib import Path

    # Local imports keep the module lazy — primitives.ingest pulls in heavy deps
    # (Marker, etc.) so we don't want them imported at API startup.
    from primitives import ingest as ingest_primitive

    paths = [Path(p) for p in pdf_paths]
    on_progress = _make_progress_callback(task_id)

    def _do() -> None:
        book = ingest_primitive.ingest_book(
            paths,
            corpus_id=corpus_id,
            title=title or (paths[0].stem if paths else "Untitled"),
            authors=authors,
            edition=edition,
            use_llm=use_llm,
            on_progress=on_progress,
        )
        # Counts: read fresh under a short-lived session.
        from sqlmodel import func, select

        from data.models import Block, Page

        with session_scope() as s:
            page_count = s.exec(
                select(func.count()).select_from(Page).where(Page.book_id == book.id)
            ).one()
            block_count = s.exec(
                select(func.count()).select_from(Block).where(Block.book_id == book.id)
            ).one()
        mark_completed(
            task_id,
            {
                "book_id": book.id,
                "title": book.title,
                "corpus_id": book.corpus_id,
                "source_page_min": book.source_page_min,
                "source_page_max": book.source_page_max,
                "page_count": int(page_count or 0),
                "block_count": int(block_count or 0),
            },
        )

    _start_thread(task_id, _do)


__all__ = [
    "TaskCancelled",
    "cancel_task",
    "get_task",
    "mark_completed",
    "mark_failed",
    "queue_depth",
    "schedule_task",
    "worker_count",
    "start_book_ingestion_task",
]
