"""Professor-profile + past-exam routes (spec §5.13, §9 Phase 3).

Mounted at ``/profiles`` and ``/ingest/past-exam``. Thin wrappers over
:mod:`features.professor_profile` and :mod:`features.past_exam_ingest` —
validation, DTO shaping, and HTTPException mapping live here; the orchestration
lives in the feature modules.

Error code contract:

- 404 when the referenced corpus or profile is absent.
- 400 on schema validation failures during PATCH / unknown edit fields.
- 503 when the underlying ``generate()`` primitive reports that the Anthropic
  SDK isn't wired up (missing key) — with the install / configure hint.
- 402 when the monthly budget cap is exceeded (bubbled from
  ``BudgetExceededError``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from costs.tracker import BudgetExceededError
from data.db import get_session
from data.models import Corpus, ProfessorProfile
from features.past_exam_ingest import (
    PastExamIngestRequest,
    PastExamIngestResult,
    ingest_past_exam,
)
from features.professor_profile import (
    ProfileBuildRequest,
    ProfileError,
    build_profile_from_memos,
    get_profile,
    list_profiles_for_corpus,
    seed_pollack_profile,
    update_profile,
)

router = APIRouter(tags=["profiles"])


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class ProfileDTO(BaseModel):
    """Over-the-wire shape of a :class:`ProfessorProfile`."""

    id: str
    corpus_id: str
    professor_name: str
    course: str
    school: str | None
    exam_format: dict[str, Any]
    pet_peeves: list[dict[str, Any]]
    favored_framings: list[str]
    stable_traps: list[dict[str, Any]]
    voice_conventions: list[dict[str, Any]]
    commonly_tested: list[str]
    source_artifact_paths: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, p: ProfessorProfile) -> ProfileDTO:
        return cls(
            id=p.id,
            corpus_id=p.corpus_id,
            professor_name=p.professor_name,
            course=p.course,
            school=p.school,
            exam_format=dict(p.exam_format),
            pet_peeves=list(p.pet_peeves),
            favored_framings=list(p.favored_framings),
            stable_traps=list(p.stable_traps),
            voice_conventions=list(p.voice_conventions),
            commonly_tested=list(p.commonly_tested),
            source_artifact_paths=list(p.source_artifact_paths),
            created_at=p.created_at.isoformat(),
            updated_at=p.updated_at.isoformat(),
        )


class ProfileBuildHttpRequest(BaseModel):
    corpus_id: str
    professor_name: str
    course: str
    school: str | None = None
    memo_artifact_ids: list[str] = Field(default_factory=list)
    syllabus_markdown: str | None = None


class ProfileBuildHttpResponse(BaseModel):
    profile: ProfileDTO
    cache_hit: bool
    warnings: list[str]


class ProfilePatchRequest(BaseModel):
    edits: dict[str, Any]


class SeedPollackRequest(BaseModel):
    corpus_id: str


class PastExamIngestHttpRequest(BaseModel):
    corpus_id: str
    exam_markdown: str
    grader_memo_markdown: str | None = None
    source_paths: list[str] = Field(default_factory=list)
    year: int | None = None
    professor_name: str | None = None


class PastExamIngestHttpResponse(BaseModel):
    past_exam_artifact_id: str
    grader_memo_artifact_id: str | None


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


def _translate_generate_error(exc: ProfileError) -> HTTPException:
    """Map a ProfileError into the right HTTP status.

    The generate primitive raises a catch-all ``GenerateError``; when the key
    is missing we want 503 with a hint (same shape ingest uses for Marker), so
    downstream UIs know this is a recoverable "go configure credentials"
    state rather than a permanent server failure.
    """
    message = str(exc)
    lowered = message.lower()
    if "no anthropic api key" in lowered or "settings → api key" in lowered:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "anthropic_not_configured",
                "message": message,
                "install_command": "Set an Anthropic API key via POST /credentials/anthropic",
            },
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=message,
    )


# ---------------------------------------------------------------------------
# /profiles/build
# ---------------------------------------------------------------------------


@router.post("/profiles/build", response_model=ProfileBuildHttpResponse)
def post_build_profile(
    payload: ProfileBuildHttpRequest,
    session: Session = Depends(get_session),
) -> ProfileBuildHttpResponse:
    _assert_corpus_exists(session, payload.corpus_id)

    req = ProfileBuildRequest(
        corpus_id=payload.corpus_id,
        professor_name=payload.professor_name,
        course=payload.course,
        school=payload.school,
        memo_artifact_ids=list(payload.memo_artifact_ids),
        syllabus_markdown=payload.syllabus_markdown,
    )
    try:
        result = build_profile_from_memos(session, req)
    except ProfileError as exc:
        raise _translate_generate_error(exc) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc

    return ProfileBuildHttpResponse(
        profile=ProfileDTO.from_model(result.profile),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# /profiles (list)
# ---------------------------------------------------------------------------


@router.get("/profiles", response_model=list[ProfileDTO])
def list_profiles(
    corpus_id: str = Query(..., description="Corpus to list profiles for."),
    professor_name: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[ProfileDTO]:
    _assert_corpus_exists(session, corpus_id)
    rows = list_profiles_for_corpus(session, corpus_id, professor_name)
    return [ProfileDTO.from_model(p) for p in rows]


# ---------------------------------------------------------------------------
# /profiles/seed-pollack
# ---------------------------------------------------------------------------


@router.post("/profiles/seed-pollack", response_model=ProfileDTO)
def post_seed_pollack(
    payload: SeedPollackRequest,
    session: Session = Depends(get_session),
) -> ProfileDTO:
    _assert_corpus_exists(session, payload.corpus_id)
    profile = seed_pollack_profile(session, payload.corpus_id)
    return ProfileDTO.from_model(profile)


# ---------------------------------------------------------------------------
# /profiles/{profile_id}
# ---------------------------------------------------------------------------


@router.get("/profiles/{profile_id}", response_model=ProfileDTO)
def get_profile_by_id(
    profile_id: str,
    session: Session = Depends(get_session),
) -> ProfileDTO:
    profile = get_profile(session, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"profile {profile_id!r} not found",
        )
    return ProfileDTO.from_model(profile)


@router.patch("/profiles/{profile_id}", response_model=ProfileDTO)
def patch_profile(
    profile_id: str,
    payload: ProfilePatchRequest,
    session: Session = Depends(get_session),
) -> ProfileDTO:
    try:
        profile = update_profile(session, profile_id, payload.edits)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return ProfileDTO.from_model(profile)


# ---------------------------------------------------------------------------
# /ingest/past-exam
# ---------------------------------------------------------------------------


@router.post("/ingest/past-exam", response_model=PastExamIngestHttpResponse)
def post_ingest_past_exam(
    payload: PastExamIngestHttpRequest,
    session: Session = Depends(get_session),
) -> PastExamIngestHttpResponse:
    _assert_corpus_exists(session, payload.corpus_id)

    req = PastExamIngestRequest(
        corpus_id=payload.corpus_id,
        exam_markdown=payload.exam_markdown,
        grader_memo_markdown=payload.grader_memo_markdown,
        source_paths=list(payload.source_paths),
        year=payload.year,
        professor_name=payload.professor_name,
    )
    result: PastExamIngestResult = ingest_past_exam(session, req)
    return PastExamIngestHttpResponse(
        past_exam_artifact_id=result.past_exam_artifact_id,
        grader_memo_artifact_id=result.grader_memo_artifact_id,
    )
