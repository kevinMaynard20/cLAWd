"""Global search route (spec §5.14).

``GET /search?q=<query>&corpus_id=<id>&kinds=block,transcript_segment,artifact&limit=50``

Returns a ranked list of results spanning books (Blocks), transcripts
(TranscriptSegments), and artifact content.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from data.db import get_session
from features.global_search import SearchRequest, SearchResult, search

router = APIRouter(prefix="/search", tags=["search"])


class SearchResultDTO(BaseModel):
    kind: str
    id: str
    corpus_id: str
    source_context: str
    snippet: str
    score: float
    source_location: dict

    @classmethod
    def from_model(cls, r: SearchResult) -> SearchResultDTO:
        return cls(
            kind=r.kind,
            id=r.id,
            corpus_id=r.corpus_id,
            source_context=r.source_context,
            snippet=r.snippet,
            score=r.score,
            source_location=r.source_location,
        )


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[SearchResultDTO]


@router.get("", response_model=SearchResponse)
def global_search(
    q: str = Query(..., min_length=1, description="Search query string."),
    corpus_id: str | None = Query(None),
    kinds: str | None = Query(
        None,
        description="Comma-separated subset of 'block', 'transcript_segment', 'artifact'.",
    ),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> SearchResponse:
    kinds_list = [k.strip() for k in kinds.split(",")] if kinds else None
    req = SearchRequest(q=q, corpus_id=corpus_id, kinds=kinds_list, limit=limit)
    results = search(session, req)
    return SearchResponse(
        query=q,
        count=len(results),
        results=[SearchResultDTO.from_model(r) for r in results],
    )
