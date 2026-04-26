"""Books route — case index for one book.

The UI needs to list cases inside a book without grepping raw block JSON. We
already store ``block_metadata.case_name`` on every CASE_OPINION block during
ingestion, so this is purely a read.

``GET /books/{book_id}/cases`` returns the case list. ``random=true`` plus an
optional page range powers the cold-call random-pick flow (workflow §6).
"""

from __future__ import annotations

import random as _random
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from data.db import get_session
from data.models import Block, BlockType, Book

router = APIRouter(prefix="/books", tags=["books"])


class CaseRowDTO(BaseModel):
    block_id: str
    case_name: str
    source_page: int
    court: str | None = None
    year: int | None = None
    citation: str | None = None
    judge: str | None = None
    excerpt: str  # first ~200 chars of the opinion's markdown for preview


class CasesResponse(BaseModel):
    book_id: str
    book_title: str
    count: int
    cases: list[CaseRowDTO]


def _meta(b: Block, key: str) -> Any:
    md = b.block_metadata or {}
    val = md.get(key)
    return val


def _to_case_row(b: Block) -> CaseRowDTO:
    name = _meta(b, "case_name") or "(unnamed case)"
    excerpt = (b.markdown or "").strip().replace("\n", " ")
    if len(excerpt) > 220:
        excerpt = excerpt[:220].rstrip() + "…"
    year_raw = _meta(b, "year")
    try:
        year = int(year_raw) if year_raw is not None else None
    except (TypeError, ValueError):
        year = None
    return CaseRowDTO(
        block_id=b.id,
        case_name=str(name),
        source_page=int(b.source_page),
        court=_meta(b, "court"),
        year=year,
        citation=_meta(b, "citation"),
        judge=_meta(b, "judge"),
        excerpt=excerpt,
    )


@router.get("/{book_id}/cases", response_model=CasesResponse)
def list_cases(
    book_id: str,
    page_start: int | None = Query(None, ge=1, description="Source-page lower bound (inclusive)."),
    page_end: int | None = Query(None, ge=1, description="Source-page upper bound (inclusive)."),
    random: bool = Query(False, description="If true and at least one match, return exactly one random row."),
    session: Session = Depends(get_session),
) -> CasesResponse:
    """List CASE_OPINION blocks in a book, ordered by source_page.

    The UI uses this for the corpus-detail "Cases" tab. ``random=true``
    plus a ``(page_start, page_end)`` range is the cold-call random pick.
    """
    book = session.exec(select(Book).where(Book.id == book_id)).first()
    if book is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"book {book_id!r} not found",
        )

    if page_start is not None and page_end is not None and page_end < page_start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="page_end must be >= page_start",
        )

    stmt = select(Block).where(Block.book_id == book_id).where(
        Block.type == BlockType.CASE_OPINION
    )
    if page_start is not None:
        stmt = stmt.where(Block.source_page >= page_start)
    if page_end is not None:
        stmt = stmt.where(Block.source_page <= page_end)
    stmt = stmt.order_by(Block.source_page, Block.order_index)

    rows = list(session.exec(stmt).all())

    if random and rows:
        rows = [_random.choice(rows)]

    cases = [_to_case_row(b) for b in rows]
    return CasesResponse(
        book_id=book.id,
        book_title=book.title,
        count=len(cases),
        cases=cases,
    )
