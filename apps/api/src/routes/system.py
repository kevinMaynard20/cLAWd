"""System diagnostics: rich health endpoint + storage GC.

`GET /system/health` — disk free, Marker availability, DB row counts,
worker status, queue depth, monthly-budget posture. Drives the dashboard's
"is the platform OK" panel.

`POST /system/storage/cleanup` — delete uploaded PDFs/text files that no
longer correspond to a Book.batch_hash or any other live reference. Useful
after a series of failed ingest attempts left orphans on disk.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlmodel import Session, func, select

from costs.tracker import get_budget_status
from data.db import get_session
from data.models import (
    Artifact,
    BackgroundTask,
    Block,
    Book,
    Corpus,
    Page,
    TaskStatus,
    Transcript,
)
from features import tasks as task_features

router = APIRouter(prefix="/system", tags=["system"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _storage_root() -> Path:
    """Where writable state lives. Bundled .app: user data dir; dev:
    `<repo>/storage/`. See ``paths.storage_root`` for the full rationale —
    in short, ``Path.cwd()`` resolves to ``/`` in the .app and trying to
    ``mkdir(/storage)`` is what 500'd this endpoint."""
    from paths import storage_root

    return storage_root()


def _disk_stats(path: Path) -> dict[str, int]:
    """Free + total bytes on the filesystem holding `path`."""
    try:
        st = os.statvfs(path)
        free = int(st.f_bavail) * int(st.f_frsize)
        total = int(st.f_blocks) * int(st.f_frsize)
        return {"free_bytes": free, "total_bytes": total}
    except (OSError, AttributeError):
        return {"free_bytes": 0, "total_bytes": 0}


def _process_rss_bytes() -> int:
    """Best-effort current process RSS. Uses `resource` on POSIX (in
    KiB on Linux, bytes on macOS — we normalize). Returns 0 on Windows
    or any failure."""
    try:
        import resource

        rusage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux reports KiB, macOS reports bytes.
        if os.uname().sysname == "Darwin":
            return int(rusage.ru_maxrss)
        return int(rusage.ru_maxrss) * 1024
    except Exception:
        return 0


def _marker_available() -> bool:
    """Cheap check: can we import marker-pdf without crashing? Doesn't
    actually run inference — just the dependency presence."""
    try:
        import marker  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# /system/health
# ---------------------------------------------------------------------------


class StorageStats(BaseModel):
    free_bytes: int
    total_bytes: int


class WorkerStats(BaseModel):
    concurrency: int
    alive_workers: int
    queue_depth: int
    pending_tasks: int
    running_tasks: int


class CountsStats(BaseModel):
    corpora: int
    books: int
    pages: int
    blocks: int
    transcripts: int
    artifacts: int


class HealthResponse(BaseModel):
    status: str  # "ok" / "degraded" — no "down" since this is a single-process app
    server_time_utc: datetime
    storage: StorageStats
    process_rss_bytes: int
    marker_available: bool
    workers: WorkerStats
    counts: CountsStats
    budget_state: str   # "off" | "ok" | "warning" | "exceeded"


@router.get("/health", response_model=HealthResponse)
def system_health(session: Session = Depends(get_session)) -> HealthResponse:
    """Rich health snapshot. Drives the dashboard's diagnostics card."""
    storage_dir = _storage_root()
    storage_dir.mkdir(parents=True, exist_ok=True)
    disk = _disk_stats(storage_dir)

    pending = session.exec(
        select(func.count())
        .select_from(BackgroundTask)
        .where(BackgroundTask.status == TaskStatus.PENDING)
    ).one()
    running = session.exec(
        select(func.count())
        .select_from(BackgroundTask)
        .where(BackgroundTask.status == TaskStatus.RUNNING)
    ).one()

    counts = CountsStats(
        corpora=int(session.exec(select(func.count()).select_from(Corpus)).one() or 0),
        books=int(session.exec(select(func.count()).select_from(Book)).one() or 0),
        pages=int(session.exec(select(func.count()).select_from(Page)).one() or 0),
        blocks=int(session.exec(select(func.count()).select_from(Block)).one() or 0),
        transcripts=int(
            session.exec(select(func.count()).select_from(Transcript)).one() or 0
        ),
        artifacts=int(
            session.exec(select(func.count()).select_from(Artifact)).one() or 0
        ),
    )

    budget = get_budget_status()
    overall = "degraded" if budget.state == "exceeded" else "ok"
    if disk["total_bytes"] > 0 and disk["free_bytes"] < 100 * 1024 * 1024:
        overall = "degraded"

    return HealthResponse(
        status=overall,
        server_time_utc=datetime.now(tz=UTC),
        storage=StorageStats(**disk),
        process_rss_bytes=_process_rss_bytes(),
        marker_available=_marker_available(),
        workers=WorkerStats(
            concurrency=task_features._concurrency(),
            alive_workers=task_features.worker_count(),
            queue_depth=task_features.queue_depth(),
            pending_tasks=int(pending or 0),
            running_tasks=int(running or 0),
        ),
        counts=counts,
        budget_state=budget.state,
    )


# ---------------------------------------------------------------------------
# /system/storage/cleanup — delete orphaned uploads
# ---------------------------------------------------------------------------


class CleanupResult(BaseModel):
    scanned: int
    deleted: int
    deleted_bytes: int
    kept: int
    paths_deleted: list[str]
    dry_run: bool


@router.post("/storage/cleanup", response_model=CleanupResult)
def storage_cleanup_route(
    dry_run: bool = Query(
        True,
        description="When True (default), report what WOULD be deleted without actually unlinking.",
    ),
    session: Session = Depends(get_session),
) -> CleanupResult:
    """Garbage-collect orphaned uploads in `storage/uploads/`.

    A file is considered orphaned when its sha256 (filename stem) is NOT
    referenced by any live `Book.batch_hashes` entry, doesn't match a
    `Transcript.id` (transcripts are content-addressed), and isn't a
    `.part` temp file from a partial upload.

    Default `dry_run=True` — call again with `dry_run=false` to actually
    delete. The .part temp files (incomplete uploads) are always removed."""
    # Honor LAWSCHOOL_UPLOADS_DIR (used by the bundled .app to redirect
    # writes outside the read-only bundle) — the actual upload writer
    # uses the same resolver, so cleanup must agree on the location.
    from routes.uploads import _resolve_uploads_dir

    uploads_root = _resolve_uploads_dir()
    if not uploads_root.exists():
        return CleanupResult(
            scanned=0, deleted=0, deleted_bytes=0, kept=0, paths_deleted=[], dry_run=dry_run
        )

    # Build the live-reference set
    live_hashes: set[str] = set()
    for book in session.exec(select(Book)).all():
        for h in book.batch_hashes or []:
            if isinstance(h, str):
                live_hashes.add(h)
    for transcript in session.exec(select(Transcript)).all():
        live_hashes.add(transcript.id)

    scanned = 0
    deleted = 0
    deleted_bytes = 0
    kept = 0
    paths_deleted: list[str] = []
    for sub in ("pdf", "text"):
        d = uploads_root / sub
        if not d.exists():
            continue
        for path in d.iterdir():
            scanned += 1
            name = path.name
            stem = path.stem
            # Always purge .part temp files
            if name.endswith(".part"):
                size = path.stat().st_size if path.exists() else 0
                if not dry_run:
                    path.unlink(missing_ok=True)
                deleted += 1
                deleted_bytes += size
                paths_deleted.append(str(path))
                continue
            if stem in live_hashes:
                kept += 1
                continue
            # Orphan
            size = path.stat().st_size if path.exists() else 0
            if not dry_run:
                path.unlink(missing_ok=True)
            deleted += 1
            deleted_bytes += size
            paths_deleted.append(str(path))

    return CleanupResult(
        scanned=scanned,
        deleted=deleted,
        deleted_bytes=deleted_bytes,
        kept=kept,
        paths_deleted=paths_deleted,
        dry_run=dry_run,
    )
