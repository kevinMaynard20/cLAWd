"""Transcript routes (spec §4.1.2 + §3.8/§3.9).

Mounted at ``/transcripts``. Three endpoints:

- ``POST /transcripts`` — ingest raw Gemini text → cleaned Transcript +
  Segments. Thin wrapper over :func:`features.transcript_ingest.ingest_transcript_text`.
- ``GET /transcripts/{transcript_id}`` — full detail: transcript fields +
  every segment.
- ``GET /transcripts?corpus_id=...`` — summary list for a corpus.

Error mapping:
- 404: corpus not found / transcript not found.
- 402: monthly budget cap exceeded.
- 503: Anthropic key missing, API failure, or cleanup schema exhausted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from costs.tracker import BudgetExceededError
from data.db import get_session
from data.models import Corpus, Transcript, TranscriptSegment
from features.transcript_ingest import (
    TranscriptIngestError,
    TranscriptIngestRequest,
    TranscriptIngestResult,
    ingest_transcript_text,
)

router = APIRouter(prefix="/transcripts", tags=["transcripts"])


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class TranscriptIngestHttpRequest(BaseModel):
    """Request body for POST /transcripts.

    ``raw_text`` is required and is the verbatim Gemini dump. Metadata
    fields are all optional — the UI fills in what the user knows."""

    corpus_id: str = Field(..., description="Which corpus the transcript belongs to.")
    raw_text: str = Field(..., description="Raw Gemini transcription text.")
    lecture_date: datetime | None = None
    topic: str | None = None
    assignment_code: str | None = None
    source_path: str | None = None


class TranscriptSegmentDTO(BaseModel):
    """Over-the-wire shape for a :class:`TranscriptSegment`."""

    id: str
    transcript_id: str
    order_index: int
    start_char: int
    end_char: int
    speaker: str
    content: str
    mentioned_cases: list[str]
    mentioned_rules: list[str]
    mentioned_concepts: list[str]
    sentiment_flags: list[str]

    @classmethod
    def from_model(cls, s: TranscriptSegment) -> TranscriptSegmentDTO:
        return cls(
            id=s.id,
            transcript_id=s.transcript_id,
            order_index=s.order_index,
            start_char=s.start_char,
            end_char=s.end_char,
            speaker=s.speaker.value,
            content=s.content,
            mentioned_cases=list(s.mentioned_cases),
            mentioned_rules=list(s.mentioned_rules),
            mentioned_concepts=list(s.mentioned_concepts),
            sentiment_flags=list(s.sentiment_flags),
        )


class TranscriptDTO(BaseModel):
    """Over-the-wire shape for a :class:`Transcript` with its segments."""

    id: str
    corpus_id: str
    source_type: str
    source_path: str | None
    lecture_date: datetime | None
    topic: str | None
    assignment_code: str | None
    raw_text: str
    cleaned_text: str
    ingested_at: datetime
    segments: list[TranscriptSegmentDTO]

    @classmethod
    def from_model(
        cls, t: Transcript, segments: list[TranscriptSegment]
    ) -> TranscriptDTO:
        return cls(
            id=t.id,
            corpus_id=t.corpus_id,
            source_type=t.source_type.value,
            source_path=t.source_path,
            lecture_date=t.lecture_date,
            topic=t.topic,
            assignment_code=t.assignment_code,
            raw_text=t.raw_text,
            cleaned_text=t.cleaned_text,
            ingested_at=t.ingested_at,
            segments=[TranscriptSegmentDTO.from_model(s) for s in segments],
        )


class TranscriptSummaryDTO(BaseModel):
    """Lightweight summary for the list endpoint — no full text or
    segments, just the fields the UI needs to render a list row."""

    id: str
    corpus_id: str
    topic: str | None
    lecture_date: datetime | None
    assignment_code: str | None
    source_type: str
    ingested_at: datetime

    @classmethod
    def from_model(cls, t: Transcript) -> TranscriptSummaryDTO:
        return cls(
            id=t.id,
            corpus_id=t.corpus_id,
            topic=t.topic,
            lecture_date=t.lecture_date,
            assignment_code=t.assignment_code,
            source_type=t.source_type.value,
            ingested_at=t.ingested_at,
        )


class TranscriptIngestHttpResponse(BaseModel):
    """Response body for POST /transcripts."""

    transcript_id: str
    cache_hit: bool
    segment_count: int
    mentioned_cases: list[str]
    unresolved_mentions: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_corpus_exists(session: Session, corpus_id: str) -> None:
    row = session.exec(select(Corpus).where(Corpus.id == corpus_id)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"corpus {corpus_id!r} not found",
        )


def _translate_ingest_error(exc: TranscriptIngestError) -> HTTPException:
    """Map a feature error to an HTTPException. Missing API key produces a
    503 with an install-hint shape matching the ``/profiles`` convention."""
    message = str(exc)
    lowered = message.lower()
    if "no anthropic api key" in lowered or "settings → api key" in lowered:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "anthropic_not_configured",
                "message": message,
                "install_command": (
                    "Set an Anthropic API key via POST /credentials/anthropic"
                ),
            },
        )
    if "not found" in lowered:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=message
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=TranscriptIngestHttpResponse)
def post_ingest_transcript(
    payload: TranscriptIngestHttpRequest,
    session: Session = Depends(get_session),
) -> TranscriptIngestHttpResponse:
    """Ingest a raw Gemini transcription text → persisted Transcript.

    Idempotent: re-ingesting the same ``raw_text`` in the same corpus is a
    no-op (returns the existing Transcript's id with ``cache_hit=True``).
    """
    _assert_corpus_exists(session, payload.corpus_id)

    req = TranscriptIngestRequest(
        corpus_id=payload.corpus_id,
        raw_text=payload.raw_text,
        lecture_date=payload.lecture_date,
        topic=payload.topic,
        assignment_code=payload.assignment_code,
        source_path=payload.source_path,
    )

    try:
        result: TranscriptIngestResult = ingest_transcript_text(session, req)
    except TranscriptIngestError as exc:
        raise _translate_ingest_error(exc) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc

    return TranscriptIngestHttpResponse(
        transcript_id=result.transcript.id,
        cache_hit=result.cache_hit,
        segment_count=result.segment_count,
        mentioned_cases=list(result.mentioned_cases),
        unresolved_mentions=list(result.unresolved_mentions),
    )


@router.get("/{transcript_id}", response_model=TranscriptDTO)
def get_transcript(
    transcript_id: str,
    session: Session = Depends(get_session),
) -> TranscriptDTO:
    """Full transcript detail: the Transcript row plus every segment."""
    transcript = session.exec(
        select(Transcript).where(Transcript.id == transcript_id)
    ).first()
    if transcript is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"transcript {transcript_id!r} not found",
        )

    segments = session.exec(
        select(TranscriptSegment)
        .where(TranscriptSegment.transcript_id == transcript_id)
        .order_by(TranscriptSegment.order_index)
    ).all()

    return TranscriptDTO.from_model(transcript, list(segments))


@router.get("", response_model=list[TranscriptSummaryDTO])
def list_transcripts(
    corpus_id: str = Query(..., description="List transcripts for this corpus."),
    session: Session = Depends(get_session),
) -> list[TranscriptSummaryDTO]:
    """List all transcripts in a corpus (summary shape — no full text).

    Used by the UI "transcripts for this course" panel. Ordered newest-first
    by ingest time so the most-recent upload appears at the top.
    """
    _assert_corpus_exists(session, corpus_id)
    rows = session.exec(
        select(Transcript)
        .where(Transcript.corpus_id == corpus_id)
        .order_by(Transcript.ingested_at.desc())
    ).all()
    return [TranscriptSummaryDTO.from_model(t) for t in rows]


# Suppress unused-import linting on Any — kept for forward-compat when DTOs
# gain nested dict fields.
_ = Any
