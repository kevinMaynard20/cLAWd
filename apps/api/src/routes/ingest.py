"""Ingestion API — spec §4.1, §5.1.

Phase 1.4: full pipeline wired up. This route calls `primitives.ingest.ingest_book`
which runs Marker (cached), extracts source-page markers, segments into typed
blocks, extracts the TOC, and persists everything inside one transaction.

Error mapping:
- `MarkerNotInstalledError` → 503 (install command in detail)
- `ValueError` (bad paths, missing corpus) → 400
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from data.db import get_session
from data.models import Block, Page
from primitives import ingest as ingest_primitive
from primitives.marker_runner import MarkerNotInstalledError

router = APIRouter(prefix="/ingest", tags=["ingest"])


class IngestBookRequest(BaseModel):
    pdf_paths: list[str] = Field(..., min_length=1)
    title: str | None = None
    edition: str | None = None
    authors: list[str] = Field(default_factory=list)
    corpus_id: str | None = None  # new corpus is created when omitted
    use_llm: bool = True


class IngestBookResponse(BaseModel):
    book_id: str
    title: str
    corpus_id: str
    source_page_min: int
    source_page_max: int
    page_count: int
    block_count: int
    was_cached: bool  # True when the book already existed and we returned it as-is


class IngestBookAsyncResponse(BaseModel):
    """Returned by the async variant — caller polls `/tasks/{task_id}` for
    progress + final result."""

    task_id: str
    poll_url: str


@router.post("/book", response_model=IngestBookResponse)
def ingest_book_route(
    payload: IngestBookRequest,
    session: Session = Depends(get_session),
) -> IngestBookResponse:
    """POST /ingest/book — run the full Phase 1.4 ingestion pipeline.

    `title` falls back to the first PDF's stem when omitted, so a user who
    just wants to hand us a PDF doesn't have to name the book up front.
    """
    if not payload.pdf_paths:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="pdf_paths must be non-empty",
        )

    pdf_paths = [Path(p) for p in payload.pdf_paths]
    title = payload.title or pdf_paths[0].stem or "Untitled Book"

    # Pre-check dedup before calling the full pipeline, so we can report
    # `was_cached=True` accurately. (The primitive also short-circuits, but
    # we can't observe it from here without book_id in hand.)
    from data.models import Book  # local import; avoids top-level cycle risk

    existing_ids = {b.id for b in session.exec(select(Book.id)).all()}

    try:
        book = ingest_primitive.ingest_book(
            pdf_paths,
            corpus_id=payload.corpus_id,
            title=title,
            authors=list(payload.authors),
            edition=payload.edition,
            use_llm=payload.use_llm,
        )
    except MarkerNotInstalledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "marker_not_installed",
                "message": str(exc),
                "install_command": "uv sync --extra marker",
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    was_cached = book.id in existing_ids

    # The DB session above is a FastAPI-scoped session and the primitive
    # used its own; make sure we see the freshly-committed rows.
    session.expire_all()
    page_count = session.exec(
        select(func.count()).select_from(Page).where(Page.book_id == book.id)
    ).one()
    block_count = session.exec(
        select(func.count()).select_from(Block).where(Block.book_id == book.id)
    ).one()

    return IngestBookResponse(
        book_id=book.id,
        title=book.title,
        corpus_id=book.corpus_id,
        source_page_min=book.source_page_min,
        source_page_max=book.source_page_max,
        page_count=int(page_count or 0),
        block_count=int(block_count or 0),
        was_cached=was_cached,
    )


# ---------------------------------------------------------------------------
# /ingest/syllabus (spec §4.1.4)
# ---------------------------------------------------------------------------


from costs.tracker import BudgetExceededError  # noqa: E402
from features.syllabus_ingest import (  # noqa: E402
    SyllabusIngestError,
    SyllabusIngestRequest,
    ingest_syllabus,
)


class IngestSyllabusRequest(BaseModel):
    corpus_id: str = Field(..., description="Corpus this syllabus belongs to.")
    syllabus_markdown: str = Field(..., min_length=1)
    book_id: str | None = Field(
        default=None,
        description="Optional. When provided, page ranges are validated against this book.",
    )
    professor_name: str | None = None
    semester_hint: str | None = None
    source_path: str | None = None


class SyllabusEntryDTO(BaseModel):
    id: str
    code: str
    title: str
    page_ranges: list[list[int]]
    cases_assigned: list[str]
    topic_tags: list[str]


class DiscrepancyDTO(BaseModel):
    code: str
    page_range: list[int]
    book_min: int
    book_max: int
    message: str


class IngestSyllabusResponse(BaseModel):
    syllabus_id: str
    title: str | None
    corpus_id: str
    entries: list[SyllabusEntryDTO]
    discrepancies: list[DiscrepancyDTO]
    warnings: list[str]


@router.post("/syllabus", response_model=IngestSyllabusResponse)
def ingest_syllabus_route(
    payload: IngestSyllabusRequest,
    session: Session = Depends(get_session),
) -> IngestSyllabusResponse:
    """Parse an uploaded syllabus into SyllabusEntry rows. Once present,
    `AssignmentCodeQuery` resolves properly in the retrieve primitive."""
    req = SyllabusIngestRequest(
        corpus_id=payload.corpus_id,
        syllabus_markdown=payload.syllabus_markdown,
        book_id=payload.book_id,
        professor_name=payload.professor_name,
        semester_hint=payload.semester_hint,
        source_path=payload.source_path,
    )

    try:
        result = ingest_syllabus(session, req)
    except SyllabusIngestError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc

    return IngestSyllabusResponse(
        syllabus_id=result.syllabus.id,
        title=result.syllabus.title,
        corpus_id=result.syllabus.corpus_id,
        entries=[
            SyllabusEntryDTO(
                id=e.id,
                code=e.code,
                title=e.title,
                page_ranges=[list(pr) for pr in e.page_ranges],
                cases_assigned=list(e.cases_assigned),
                topic_tags=list(e.topic_tags),
            )
            for e in result.entries
        ],
        discrepancies=[
            DiscrepancyDTO(
                code=d.code,
                page_range=list(d.page_range),
                book_min=d.book_min,
                book_max=d.book_max,
                message=d.message,
            )
            for d in result.discrepancies
        ],
        warnings=list(result.warnings),
    )


# ---------------------------------------------------------------------------
# /ingest/book/async — large-file ingestion path with progress polling
# ---------------------------------------------------------------------------


from data.models import TaskKind  # noqa: E402
from features import tasks as task_features  # noqa: E402


@router.post(
    "/book/async",
    response_model=IngestBookAsyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def ingest_book_async_route(payload: IngestBookRequest) -> IngestBookAsyncResponse:
    """Schedule book ingestion as a background task. Returns immediately with
    a `task_id` the caller polls via `GET /tasks/{task_id}`.

    Use this path when the book is non-trivial — Marker's LLM pass can run
    for tens of minutes on a 1400-page casebook, far exceeding any reasonable
    HTTP timeout. The synchronous `/ingest/book` endpoint is still available
    for small fixtures and integration tests where a blocking call is fine.
    """
    if not payload.pdf_paths:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="pdf_paths must be non-empty",
        )

    task_id = task_features.schedule_task(
        kind=TaskKind.BOOK_INGESTION,
        corpus_id=payload.corpus_id,
        inputs={
            "pdf_paths": list(payload.pdf_paths),
            "title": payload.title,
            "edition": payload.edition,
            "authors": list(payload.authors),
            "corpus_id": payload.corpus_id,
            "use_llm": payload.use_llm,
        },
    )
    task_features.start_book_ingestion_task(
        task_id,
        pdf_paths=list(payload.pdf_paths),
        title=payload.title,
        edition=payload.edition,
        authors=list(payload.authors),
        corpus_id=payload.corpus_id,
        use_llm=payload.use_llm,
    )
    return IngestBookAsyncResponse(task_id=task_id, poll_url=f"/tasks/{task_id}")
