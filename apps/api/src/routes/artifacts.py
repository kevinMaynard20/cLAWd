"""Generic artifact-listing route.

``GET /artifacts?corpus_id=&type=&q=&limit=`` powers the synthesis / attack-
sheet / what-if / outline pickers. Returns lightweight rows (no full
``content`` JSON) so the picker UI stays snappy.

A single ``GET /artifacts/{id}`` returns the full artifact for the viewer.
The viewer also wants a markdown rendering — we surface ``content.markdown``
(if present) as a top-level field so the client doesn't have to hunt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from data.db import get_session
from data.models import Artifact, ArtifactType

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


class ArtifactRowDTO(BaseModel):
    """Lightweight artifact summary for the picker. Excludes ``content`` JSON."""

    id: str
    corpus_id: str
    type: str
    created_at: datetime
    cost_usd: str
    parent_artifact_id: str | None
    title: str  # human-readable label derived from content (case_name / topic / etc.)


class ArtifactDetailDTO(BaseModel):
    id: str
    corpus_id: str
    type: str
    created_at: datetime
    sources: list[dict[str, Any]]
    content: dict[str, Any]
    prompt_template: str
    llm_model: str
    cost_usd: str
    cache_key: str
    parent_artifact_id: str | None
    markdown: str | None  # convenience: content.markdown if present
    title: str


class ArtifactListResponse(BaseModel):
    count: int
    artifacts: list[ArtifactRowDTO]


def _derive_title(a: Artifact) -> str:
    """Best-effort human label. Different artifact types stash their primary
    label under different keys; we sniff the most common ones and fall back
    to the artifact's id slice if nothing matches."""
    c = a.content or {}
    for key in (
        "case_name",
        "topic",
        "doctrinal_area",
        "course",
        "title",
        "question_label",
    ):
        v = c.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Hypo / synthesis sometimes nest one level deeper.
    for key in ("hypo", "synthesis", "rubric"):
        nested = c.get(key)
        if isinstance(nested, dict):
            for sub in ("title", "topic", "case_name"):
                v = nested.get(sub)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return f"{a.type.value} · {a.id[:8]}"


def _row(a: Artifact) -> ArtifactRowDTO:
    return ArtifactRowDTO(
        id=a.id,
        corpus_id=a.corpus_id,
        type=a.type.value,
        created_at=a.created_at,
        cost_usd=str(a.cost_usd),
        parent_artifact_id=a.parent_artifact_id,
        title=_derive_title(a),
    )


@router.get("", response_model=ArtifactListResponse)
def list_artifacts(
    corpus_id: str = Query(..., description="Required — scope listing to one corpus."),
    type: str | None = Query(
        None,
        description="ArtifactType value (e.g., 'case_brief', 'rubric', 'synthesis').",
    ),
    q: str | None = Query(
        None,
        description="Optional substring filter against the derived title (case-insensitive).",
    ),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> ArtifactListResponse:
    """List artifacts for a corpus, optionally filtered by type + title substring."""
    stmt = select(Artifact).where(Artifact.corpus_id == corpus_id)
    if type is not None:
        try:
            kind = ArtifactType(type)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown artifact type: {type!r}",
            ) from exc
        stmt = stmt.where(Artifact.type == kind)
    stmt = stmt.order_by(Artifact.created_at.desc())

    rows = list(session.exec(stmt).all())

    if q:
        needle = q.strip().lower()
        rows = [a for a in rows if needle in _derive_title(a).lower()]

    rows = rows[:limit]
    return ArtifactListResponse(
        count=len(rows),
        artifacts=[_row(a) for a in rows],
    )


@router.get("/{artifact_id}", response_model=ArtifactDetailDTO)
def get_artifact(
    artifact_id: str,
    session: Session = Depends(get_session),
) -> ArtifactDetailDTO:
    """Full artifact detail for the viewer page."""
    a = session.exec(select(Artifact).where(Artifact.id == artifact_id)).first()
    if a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"artifact {artifact_id!r} not found",
        )
    md = None
    if isinstance(a.content, dict):
        candidate = a.content.get("markdown")
        if isinstance(candidate, str):
            md = candidate
    return ArtifactDetailDTO(
        id=a.id,
        corpus_id=a.corpus_id,
        type=a.type.value,
        created_at=a.created_at,
        sources=list(a.sources or []),
        content=dict(a.content or {}),
        prompt_template=a.prompt_template,
        llm_model=a.llm_model,
        cost_usd=str(a.cost_usd),
        cache_key=a.cache_key,
        parent_artifact_id=a.parent_artifact_id,
        markdown=md,
        title=_derive_title(a),
    )
