"""Corpus listing route.

A small read-only endpoint so the UI can render a dashboard showing what the
user has ingested. The full backup/export flow already exists under
`/corpora/{id}/export`; this adds the list + single-get that the UI needs
to drive navigation.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, func, select

from data.db import get_session
from data.models import (
    Artifact,
    ArtifactType,
    Book,
    Corpus,
    ProfessorProfile,
    Transcript,
)

router = APIRouter(prefix="/corpora", tags=["corpora"])


class CorpusSummaryDTO(BaseModel):
    id: str
    name: str
    course: str
    professor_name: str | None
    school: str | None
    created_at: datetime
    book_count: int
    transcript_count: int
    artifact_count: int
    professor_profile_count: int


@router.get("", response_model=list[CorpusSummaryDTO])
def list_corpora(session: Session = Depends(get_session)) -> list[CorpusSummaryDTO]:
    """List every corpus with per-corpus counts the dashboard needs."""
    corpora = list(session.exec(select(Corpus).order_by(Corpus.created_at)).all())

    def _count(model: type, corpus_id: str) -> int:
        row = session.exec(
            select(func.count()).select_from(model).where(model.corpus_id == corpus_id)
        ).one()
        return int(row or 0)

    return [
        CorpusSummaryDTO(
            id=c.id,
            name=c.name,
            course=c.course,
            professor_name=c.professor_name,
            school=c.school,
            created_at=c.created_at,
            book_count=_count(Book, c.id),
            transcript_count=_count(Transcript, c.id),
            artifact_count=_count(Artifact, c.id),
            professor_profile_count=_count(ProfessorProfile, c.id),
        )
        for c in corpora
    ]


@router.get("/{corpus_id}", response_model=CorpusSummaryDTO)
def get_corpus(
    corpus_id: str, session: Session = Depends(get_session)
) -> CorpusSummaryDTO:
    c = session.exec(select(Corpus).where(Corpus.id == corpus_id)).first()
    if c is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Corpus {corpus_id!r} not found.",
        )

    def _count(model: type) -> int:
        row = session.exec(
            select(func.count()).select_from(model).where(model.corpus_id == corpus_id)
        ).one()
        return int(row or 0)

    return CorpusSummaryDTO(
        id=c.id,
        name=c.name,
        course=c.course,
        professor_name=c.professor_name,
        school=c.school,
        created_at=c.created_at,
        book_count=_count(Book),
        transcript_count=_count(Transcript),
        artifact_count=_count(Artifact),
        professor_profile_count=_count(ProfessorProfile),
    )


class BookSummaryDTO(BaseModel):
    """Lightweight book summary for the corpus-detail Books tab."""

    id: str
    title: str
    edition: str | None
    authors: list[str]
    source_page_min: int
    source_page_max: int
    ingested_at: datetime


@router.get("/{corpus_id}/books", response_model=list[BookSummaryDTO])
def list_corpus_books(
    corpus_id: str,
    session: Session = Depends(get_session),
) -> list[BookSummaryDTO]:
    """List books in a corpus. Powers the corpus-detail Books tab and the
    book-detail page's breadcrumb."""
    c = session.exec(select(Corpus).where(Corpus.id == corpus_id)).first()
    if c is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Corpus {corpus_id!r} not found.",
        )
    rows = list(
        session.exec(
            select(Book).where(Book.corpus_id == corpus_id).order_by(Book.ingested_at)
        ).all()
    )
    return [
        BookSummaryDTO(
            id=b.id,
            title=b.title,
            edition=b.edition,
            authors=list(b.authors or []),
            source_page_min=b.source_page_min,
            source_page_max=b.source_page_max,
            ingested_at=b.ingested_at,
        )
        for b in rows
    ]


class CorpusStatsDTO(BaseModel):
    """Per-corpus counts the UI needs to populate tabs and the outline pre-flight panel."""

    corpus_id: str
    book_count: int
    transcript_count: int
    professor_profile_count: int
    artifacts_by_type: dict[str, int]
    latest_brief_at: datetime | None
    latest_outline_at: datetime | None
    latest_emphasis_map_at: datetime | None


@router.get("/{corpus_id}/stats", response_model=CorpusStatsDTO)
def get_corpus_stats(
    corpus_id: str, session: Session = Depends(get_session)
) -> CorpusStatsDTO:
    """Counts of every entity kind in this corpus.

    The UI uses this for the corpus-detail tab badges and for the outline /
    synthesis pre-flight panels ("you have 12 briefs and 4 transcripts").
    """
    c = session.exec(select(Corpus).where(Corpus.id == corpus_id)).first()
    if c is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Corpus {corpus_id!r} not found.",
        )

    book_count = int(
        session.exec(
            select(func.count()).select_from(Book).where(Book.corpus_id == corpus_id)
        ).one()
        or 0
    )
    transcript_count = int(
        session.exec(
            select(func.count())
            .select_from(Transcript)
            .where(Transcript.corpus_id == corpus_id)
        ).one()
        or 0
    )
    profile_count = int(
        session.exec(
            select(func.count())
            .select_from(ProfessorProfile)
            .where(ProfessorProfile.corpus_id == corpus_id)
        ).one()
        or 0
    )

    artifacts_by_type: dict[str, int] = {}
    type_counts = session.exec(
        select(Artifact.type, func.count())
        .where(Artifact.corpus_id == corpus_id)
        .group_by(Artifact.type)
    ).all()
    for kind, n in type_counts:
        artifacts_by_type[kind.value if isinstance(kind, ArtifactType) else str(kind)] = int(n or 0)

    def _latest(kind: ArtifactType) -> datetime | None:
        return session.exec(
            select(func.max(Artifact.created_at))
            .where(Artifact.corpus_id == corpus_id)
            .where(Artifact.type == kind)
        ).first()

    return CorpusStatsDTO(
        corpus_id=corpus_id,
        book_count=book_count,
        transcript_count=transcript_count,
        professor_profile_count=profile_count,
        artifacts_by_type=artifacts_by_type,
        latest_brief_at=_latest(ArtifactType.CASE_BRIEF),
        latest_outline_at=_latest(ArtifactType.OUTLINE),
        latest_emphasis_map_at=None,  # EmphasisMap is its own table; queried separately if/when needed
    )


class CorpusCreateRequest(BaseModel):
    name: str
    course: str
    professor_name: str | None = None
    school: str | None = None


@router.post("", response_model=CorpusSummaryDTO, status_code=status.HTTP_201_CREATED)
def create_corpus(
    req: CorpusCreateRequest,
    session: Session = Depends(get_session),
) -> CorpusSummaryDTO:
    """Create a new corpus. Needed so the UI can set one up before ingesting."""
    c = Corpus(
        name=req.name,
        course=req.course,
        professor_name=req.professor_name,
        school=req.school,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return CorpusSummaryDTO(
        id=c.id,
        name=c.name,
        course=c.course,
        professor_name=c.professor_name,
        school=c.school,
        created_at=c.created_at,
        book_count=0,
        transcript_count=0,
        artifact_count=0,
        professor_profile_count=0,
    )
