"""Multiple-choice question practice (spec §5.12).

Thin orchestration:

1. Budget gate.
2. Retrieve source blocks via :class:`PageRangeQuery` (when book_id +
   page_start/page_end given) or :class:`CaseReferenceQuery` (when
   case_name given). At least one selector is required so the MC questions
   have grounded source material.
3. Optionally load the professor profile so two questions can target its
   ``stable_traps`` per the prompt's hard rules.
4. Render the ``mc_questions`` template via ``generate()`` with
   ``artifact_type=MC_QUESTION_SET``.
5. Return the persisted Artifact.

Error mapping:
- :class:`MCQuestionsError` -> 404.
- :class:`BudgetExceededError` -> 402.
- :class:`GenerateError` -> 503.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import Artifact, ArtifactType, Book, ProfessorProfile
from primitives.generate import GenerateError, GenerateRequest, generate
from primitives.retrieve import (
    CaseReferenceQuery,
    PageRangeQuery,
    RetrievalResult,
    retrieve,
)


@dataclass
class MCQuestionsRequest:
    corpus_id: str
    topic: str
    book_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    case_name: str | None = None
    num_questions: int = 10
    professor_profile_id: str | None = None
    force_regenerate: bool = False


@dataclass
class MCQuestionsResult:
    artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)


class MCQuestionsError(RuntimeError):
    """Feature-level failure -> route maps to 404."""


def generate_mc_questions(
    session: Session,
    req: MCQuestionsRequest,
) -> MCQuestionsResult:
    """Spec §5.12. Generate N multiple-choice questions on a topic."""
    raise_if_over_budget()

    if req.num_questions < 1 or req.num_questions > 20:
        raise MCQuestionsError(
            f"num_questions must be in [1, 20]; got {req.num_questions}."
        )

    # 2. Build the retrieval query. Prefer page range when given, else fall
    #    back to case reference. At least one is required.
    retrieval = _retrieve_blocks(session, req)
    if retrieval.empty:
        raise MCQuestionsError(
            "No source blocks found for the requested selector "
            "(book_id+page range or case_name). MC question generation "
            "requires grounded source material."
        )

    # 3. Optional professor profile.
    profile_dict: dict[str, Any] | None = None
    if req.professor_profile_id is not None:
        profile = session.exec(
            select(ProfessorProfile).where(
                ProfessorProfile.id == req.professor_profile_id
            )
        ).first()
        if profile is None:
            raise MCQuestionsError(
                f"ProfessorProfile {req.professor_profile_id!r} not found."
            )
        profile_dict = _profile_to_dict(profile)

    inputs: dict[str, Any] = {
        "topic": req.topic,
        "blocks": [
            {
                "id": b.id,
                "source_page": b.source_page,
                "markdown": b.markdown,
            }
            for b in retrieval.blocks
        ],
        "num_questions": int(req.num_questions),
        "professor_profile": profile_dict,
    }

    try:
        result = generate(
            GenerateRequest(
                template_name="mc_questions",
                inputs=inputs,
                artifact_type=ArtifactType.MC_QUESTION_SET,
                corpus_id=req.corpus_id,
                retrieval=retrieval,
                professor_profile=profile_dict,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError:
        raise

    return MCQuestionsResult(
        artifact=result.artifact,
        cache_hit=result.cache_hit,
        warnings=list(result.validation_warnings),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _retrieve_blocks(session: Session, req: MCQuestionsRequest) -> RetrievalResult:
    """Dispatch to the right retrieve primitive based on request shape."""
    if req.page_start is not None and req.page_end is not None:
        if req.book_id is None:
            raise MCQuestionsError(
                "book_id is required when page_start / page_end are provided."
            )
        # Validate the book belongs to the corpus.
        book = session.exec(
            select(Book).where(Book.id == req.book_id)
        ).first()
        if book is None:
            raise MCQuestionsError(f"book {req.book_id!r} not found.")
        if book.corpus_id != req.corpus_id:
            raise MCQuestionsError(
                f"book {req.book_id!r} does not belong to corpus "
                f"{req.corpus_id!r}."
            )
        return retrieve(
            session,
            PageRangeQuery(
                book_id=req.book_id,
                start=req.page_start,
                end=req.page_end,
            ),
        )

    if req.case_name is not None:
        return retrieve(
            session,
            CaseReferenceQuery(
                case_name=req.case_name, book_id=req.book_id
            ),
        )

    raise MCQuestionsError(
        "MC question generation requires either (book_id + page_start + "
        "page_end) or case_name. Neither was provided."
    )


def _profile_to_dict(profile: ProfessorProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "professor_name": profile.professor_name,
        "course": profile.course,
        "school": profile.school,
        "exam_format": dict(profile.exam_format or {}),
        "pet_peeves": list(profile.pet_peeves or []),
        "favored_framings": list(profile.favored_framings or []),
        "stable_traps": list(profile.stable_traps or []),
        "voice_conventions": list(profile.voice_conventions or []),
        "commonly_tested": list(profile.commonly_tested or []),
    }


__all__ = [
    "MCQuestionsError",
    "MCQuestionsRequest",
    "MCQuestionsResult",
    "generate_mc_questions",
]
