"""Background task polling routes.

Workflow:
1. Caller POSTs to a feature's `/async` variant → gets `{task_id}`.
2. Caller polls `GET /tasks/{task_id}` every ~1s for progress + status.
3. On `status == "completed"`, `result_json` carries the structured output;
   on `status == "failed"`, `error` carries a one-liner.

The UI uses the same shape for every long-running job so a single
`<TaskProgress>` component drives all of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from data.db import get_session
from data.models import BackgroundTask, TaskKind, TaskStatus
from features import tasks as task_features

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskDTO(BaseModel):
    id: str
    kind: str
    status: str
    progress_step: str
    progress_pct: float
    corpus_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    result: dict[str, Any]


def _to_dto(t: BackgroundTask) -> TaskDTO:
    return TaskDTO(
        id=t.id,
        kind=t.kind.value,
        status=t.status.value,
        progress_step=t.progress_step,
        progress_pct=t.progress_pct,
        corpus_id=t.corpus_id,
        created_at=t.created_at,
        started_at=t.started_at,
        finished_at=t.finished_at,
        error=t.error,
        result=t.result_json,
    )


@router.get("/{task_id}", response_model=TaskDTO)
def get_task_route(
    task_id: str, session: Session = Depends(get_session)
) -> TaskDTO:
    row = task_features.get_task(session, task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )
    return _to_dto(row)


class TaskListResponse(BaseModel):
    tasks: list[TaskDTO]


@router.get("", response_model=TaskListResponse)
def list_tasks_route(
    corpus_id: str | None = Query(None),
    kind: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> TaskListResponse:
    """Recent tasks. Drives the UI's "active jobs" sidebar."""
    stmt = select(BackgroundTask).order_by(BackgroundTask.created_at.desc()).limit(limit)
    if corpus_id is not None:
        stmt = stmt.where(BackgroundTask.corpus_id == corpus_id)
    if kind is not None:
        try:
            stmt = stmt.where(BackgroundTask.kind == TaskKind(kind))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown kind {kind!r}.",
            ) from exc
    if status_filter is not None:
        try:
            stmt = stmt.where(BackgroundTask.status == TaskStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown status {status_filter!r}.",
            ) from exc
    rows = list(session.exec(stmt).all())
    return TaskListResponse(tasks=[_to_dto(r) for r in rows])


class CancelResponse(BaseModel):
    cancelled: bool
    message: str


@router.post("/{task_id}/cancel", response_model=CancelResponse)
def cancel_task_route(
    task_id: str,
    session: Session = Depends(get_session),
) -> CancelResponse:
    """Cooperatively cancel a pending or running task.

    The cancellation is observed by the worker at its next progress
    checkpoint (steps inside `ingest_book`'s callback). Already-finished
    tasks return `cancelled=False` with a clarifying message."""
    row = task_features.get_task(session, task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )
    flipped = task_features.cancel_task(task_id)
    if flipped:
        return CancelResponse(
            cancelled=True,
            message="Cancellation requested; worker will stop at next checkpoint.",
        )
    return CancelResponse(
        cancelled=False,
        message=f"Task already in terminal status: {row.status.value}.",
    )
