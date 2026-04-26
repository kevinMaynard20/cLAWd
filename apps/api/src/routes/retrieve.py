"""Retrieval API — spec §4.2, §5.1.

Accepts a structured query body (page range, case reference, assignment code,
or semantic) and returns typed blocks with source attribution. The `type`
discriminator field in the request is a tagged-union pattern so clients can
serialize the request with a single JSON schema.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from data.db import get_session
from data.models import Block, BlockType, Page
from primitives import retrieve as retrieve_primitive

router = APIRouter(prefix="/retrieve", tags=["retrieve"])


# ---------------------------------------------------------------------------
# Request schemas (discriminated by `type`)
# ---------------------------------------------------------------------------


class PageRangeReq(BaseModel):
    type: Literal["page_range"]
    book_id: str
    start: int = Field(..., ge=1)
    end: int = Field(..., ge=1)


class CaseReferenceReq(BaseModel):
    type: Literal["case_reference"]
    case_name: str
    book_id: str | None = None


class AssignmentCodeReq(BaseModel):
    type: Literal["assignment_code"]
    corpus_id: str
    code: str


class SemanticReq(BaseModel):
    type: Literal["semantic"]
    corpus_id: str
    text: str
    top_k: int = 10


RetrieveRequest = Annotated[
    PageRangeReq | CaseReferenceReq | AssignmentCodeReq | SemanticReq,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class BlockDTO(BaseModel):
    id: str
    page_id: str
    book_id: str
    order_index: int
    type: BlockType
    source_page: int
    markdown: str
    block_metadata: dict[str, Any]

    @classmethod
    def from_model(cls, b: Block) -> BlockDTO:
        return cls(
            id=b.id,
            page_id=b.page_id,
            book_id=b.book_id,
            order_index=b.order_index,
            type=b.type,
            source_page=b.source_page,
            markdown=b.markdown,
            block_metadata=b.block_metadata,
        )


class PageDTO(BaseModel):
    id: str
    book_id: str
    source_page: int
    batch_pdf: str
    pdf_page_start: int
    pdf_page_end: int

    @classmethod
    def from_model(cls, p: Page) -> PageDTO:
        return cls(
            id=p.id,
            book_id=p.book_id,
            source_page=p.source_page,
            batch_pdf=p.batch_pdf,
            pdf_page_start=p.pdf_page_start,
            pdf_page_end=p.pdf_page_end,
        )


class RetrieveResponse(BaseModel):
    query_description: str
    pages: list[PageDTO]
    blocks: list[BlockDTO]
    notes: list[str]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@router.post("", response_model=RetrieveResponse)
def post_retrieve(
    req: Annotated[
        PageRangeReq | CaseReferenceReq | AssignmentCodeReq | SemanticReq,
        Body(discriminator="type"),
    ],
    session: Session = Depends(get_session),
) -> RetrieveResponse:
    try:
        if isinstance(req, PageRangeReq):
            query = retrieve_primitive.PageRangeQuery(
                book_id=req.book_id, start=req.start, end=req.end
            )
        elif isinstance(req, CaseReferenceReq):
            query = retrieve_primitive.CaseReferenceQuery(
                case_name=req.case_name, book_id=req.book_id
            )
        elif isinstance(req, AssignmentCodeReq):
            query = retrieve_primitive.AssignmentCodeQuery(
                corpus_id=req.corpus_id, code=req.code
            )
        else:  # SemanticReq
            query = retrieve_primitive.SemanticQuery(
                corpus_id=req.corpus_id, text=req.text, top_k=req.top_k
            )
    except ValueError as exc:
        # e.g., PageRangeQuery start > end.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    result = retrieve_primitive.retrieve(session, query)
    return RetrieveResponse(
        query_description=result.query_description,
        pages=[PageDTO.from_model(p) for p in result.pages],
        blocks=[BlockDTO.from_model(b) for b in result.blocks],
        notes=list(result.notes),
    )
