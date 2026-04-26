"""Artifact-lineage route (spec §7.4).

`GET /artifacts/{artifact_id}/lineage` returns the full parent chain, every
CostEvent ever attributed to the chain, and a per-source-citation summary.
The UI's debug page renders this directly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from data.db import get_session
from features.lineage import LineageError, build_lineage

router = APIRouter(prefix="/artifacts", tags=["lineage"])


class LineageNodeDTO(BaseModel):
    id: str
    type: str
    created_at: datetime
    created_by: str
    prompt_template: str
    llm_model: str
    cost_usd: Decimal
    cache_key: str
    parent_artifact_id: str | None
    sources_summary: list[dict[str, Any]]


class LineageEventDTO(BaseModel):
    id: str
    timestamp: datetime
    feature: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    total_cost_usd: Decimal
    cached: bool


class LineageResponse(BaseModel):
    target_artifact_id: str
    chain: list[LineageNodeDTO]
    events: list[LineageEventDTO]
    total_cost_usd: Decimal
    cited_block_count: int
    cited_segment_count: int
    missing_sources: list[str]


@router.get("/{artifact_id}/lineage", response_model=LineageResponse)
def artifact_lineage(
    artifact_id: str,
    session: Session = Depends(get_session),
) -> LineageResponse:
    try:
        report = build_lineage(session, artifact_id)
    except LineageError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return LineageResponse(
        target_artifact_id=report.target_artifact_id,
        chain=[
            LineageNodeDTO(
                id=n.id,
                type=n.type,
                created_at=n.created_at,
                created_by=n.created_by,
                prompt_template=n.prompt_template,
                llm_model=n.llm_model,
                cost_usd=n.cost_usd,
                cache_key=n.cache_key,
                parent_artifact_id=n.parent_artifact_id,
                sources_summary=n.sources_summary,
            )
            for n in report.chain
        ],
        events=[
            LineageEventDTO(
                id=e.id,
                timestamp=e.timestamp,
                feature=e.feature,
                model=e.model,
                provider=e.provider,
                input_tokens=e.input_tokens,
                output_tokens=e.output_tokens,
                total_cost_usd=e.total_cost_usd,
                cached=e.cached,
            )
            for e in report.events
        ],
        total_cost_usd=report.total_cost_usd,
        cited_block_count=report.cited_block_count,
        cited_segment_count=report.cited_segment_count,
        missing_sources=report.missing_sources,
    )
