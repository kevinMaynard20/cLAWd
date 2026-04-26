"""Flashcards routes (spec §5.3).

Three endpoints:

- ``POST /features/flashcards`` — generate a flashcard set for a topic +
  source range. Mounted under ``/features`` to match the rest of the
  features router-prefix convention even though this file lives in its own
  module (the volume of flashcard-specific routes warrants the split).
- ``GET /flashcards/due`` — read the user's due-card queue. Filtered by
  corpus_id so a user studying for one course doesn't see cards from
  another. Read-only; no LLM call, no CostEvent.
- ``POST /flashcards/review`` — record a single review. Triggers SM-2 state
  transition + persistence; returns the new schedule.

Error mapping:
- ``FlashcardsError`` → 404 (missing book / unknown profile / no source).
- ``BudgetExceededError`` → 402.
- ``GenerateError`` → 503.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from costs.tracker import BudgetExceededError
from data.db import get_session
from data.models import Artifact, FlashcardReview
from features.flashcards import (
    FlashcardGenerateRequest,
    FlashcardGenerateResult,
    FlashcardsError,
    due_cards,
    generate_flashcards,
    record_review,
)
from primitives.generate import GenerateError

router = APIRouter(tags=["flashcards"])


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class FlashcardSetArtifactDTO(BaseModel):
    """Local copy of the ArtifactDTO shape — kept here to avoid cross-route
    imports from ``routes/features.py``. The shape mirrors the canonical
    DTO so a UI consuming this can reuse the same parser."""

    id: str
    corpus_id: str
    type: str
    created_at: str
    content: dict[str, Any]
    sources: list[dict[str, Any]]
    prompt_template: str
    llm_model: str
    cost_usd: str  # stringified Decimal — keep precision over the wire
    cache_key: str
    parent_artifact_id: str | None

    @classmethod
    def from_model(cls, a: Artifact) -> FlashcardSetArtifactDTO:
        return cls(
            id=a.id,
            corpus_id=a.corpus_id,
            type=a.type.value,
            created_at=a.created_at.isoformat(),
            content=a.content,
            sources=list(a.sources),
            prompt_template=a.prompt_template,
            llm_model=a.llm_model,
            cost_usd=str(a.cost_usd),
            cache_key=a.cache_key,
            parent_artifact_id=a.parent_artifact_id,
        )


class FlashcardGenerateHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Corpus the cards belong to.")
    topic: str = Field(..., description="Headline label, e.g., 'Regulatory takings'.")
    book_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    case_name: str | None = None
    professor_profile_id: str | None = None
    force_regenerate: bool = False


class FlashcardGenerateHttpResponse(BaseModel):
    artifact: FlashcardSetArtifactDTO
    cache_hit: bool
    warnings: list[str]


class DueCardDTO(BaseModel):
    """Compact shape for the due-queue UI — front/back inlined so the
    student doesn't need a second roundtrip per card."""

    set_id: str
    card_id: str
    front: str
    back: str
    kind: str
    due_at: datetime | None


class FlashcardReviewHttpRequest(BaseModel):
    set_id: str
    card_id: str
    grade: int = Field(..., ge=0, le=5, description="SM-2 quality, 0..5.")


class FlashcardReviewDTO(BaseModel):
    """Returned after a successful review. Surfaces both the SM-2 numbers
    (so the UI can show "next due in 6 days") and identifying fields so
    the client can correlate to the original card."""

    id: str
    set_id: str
    card_id: str
    ease_factor: float
    interval_days: int
    repetitions: int
    due_at: datetime | None
    last_reviewed_at: datetime | None
    last_grade: int | None

    @classmethod
    def from_model(cls, r: FlashcardReview) -> FlashcardReviewDTO:
        return cls(
            id=r.id,
            set_id=r.flashcard_set_id,
            card_id=r.card_id,
            ease_factor=r.ease_factor,
            interval_days=r.interval_days,
            repetitions=r.repetitions,
            due_at=r.due_at,
            last_reviewed_at=r.last_reviewed_at,
            last_grade=r.last_grade,
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/features/flashcards", response_model=FlashcardGenerateHttpResponse)
def post_generate_flashcards(
    payload: FlashcardGenerateHttpRequest,
    session: Session = Depends(get_session),
) -> FlashcardGenerateHttpResponse:
    """Generate a flashcard set for a topic + source range (§5.3)."""
    if (
        payload.case_name is None
        and (payload.page_start is None or payload.page_end is None)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Provide either case_name or both page_start and page_end "
                "to scope the source material."
            ),
        )

    req = FlashcardGenerateRequest(
        corpus_id=payload.corpus_id,
        topic=payload.topic,
        book_id=payload.book_id,
        page_start=payload.page_start,
        page_end=payload.page_end,
        case_name=payload.case_name,
        professor_profile_id=payload.professor_profile_id,
        force_regenerate=payload.force_regenerate,
    )

    try:
        result: FlashcardGenerateResult = generate_flashcards(session, req)
    except FlashcardsError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc
    except GenerateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return FlashcardGenerateHttpResponse(
        artifact=FlashcardSetArtifactDTO.from_model(result.artifact),
        cache_hit=result.cache_hit,
        warnings=list(result.warnings),
    )


@router.get("/flashcards/due", response_model=list[DueCardDTO])
def get_due_cards(
    corpus_id: str = Query(..., description="Filter cards to one corpus."),
    limit: int = Query(50, ge=1, le=500, description="Max cards to return."),
    session: Session = Depends(get_session),
) -> list[DueCardDTO]:
    """Return cards due for review (oldest-first), filtered by corpus.

    The ``front``/``back``/``kind`` fields are inlined from the parent
    artifact's ``content["cards"][i]`` payload so the UI can render a
    review session without a second fetch per card.
    """
    rows = due_cards(session, corpus_id=corpus_id, limit=limit)
    out: list[DueCardDTO] = []
    for entry in rows:
        card = entry["card"]
        out.append(
            DueCardDTO(
                set_id=entry["set_id"],
                card_id=entry["card_id"],
                front=str(card.get("front", "")),
                back=str(card.get("back", "")),
                kind=str(card.get("kind", "")),
                due_at=entry.get("due_at"),
            )
        )
    return out


@router.post("/flashcards/review", response_model=FlashcardReviewDTO)
def post_record_review(
    payload: FlashcardReviewHttpRequest,
    session: Session = Depends(get_session),
) -> FlashcardReviewDTO:
    """Record a card review and run the SM-2 state transition.

    Returns the updated FlashcardReview row so the UI can immediately
    show "next review in N days".
    """
    try:
        row = record_review(
            session,
            set_id=payload.set_id,
            card_id=payload.card_id,
            grade=payload.grade,
        )
    except FlashcardsError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        # Defensive — pydantic already enforces 0..5 via Field, but the
        # feature also asserts internally.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return FlashcardReviewDTO.from_model(row)


__all__ = ["router"]
