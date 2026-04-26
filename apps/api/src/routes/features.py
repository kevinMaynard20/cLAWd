"""Feature routes (spec §5.*). Each feature is a thin orchestration over the
four primitives; this router exposes them via HTTP.

Phase 2 adds case brief (§5.2) only. Phase 3+ features (IRAC grading,
synthesis, attack sheets, etc.) land in follow-up slices.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from costs.tracker import BudgetExceededError
from data.db import get_session
from data.models import Artifact
from features.attack_sheet import (
    AttackSheetError,
    AttackSheetRequest,
    AttackSheetResult,
    generate_attack_sheet,
)
from features.case_brief import (
    CaseBriefError,
    CaseBriefRequest,
    CaseBriefResult,
    generate_case_brief,
)
from features.cold_call import (
    ColdCallError,
    cold_call_debrief,
    cold_call_next_turn,
)
from features.emphasis_mapper import (
    EmphasisMapError,
    EmphasisMapRequest,
    EmphasisMapResult,
    build_emphasis_map,
)
from features.hypo import (
    HypoError,
    HypoRequest,
    HypoResult,
    generate_hypo,
)
from features.irac_grading import (
    IracGradeError,
    IracGradeRequest,
    IracGradeResult,
    grade_irac_answer,
)
from features.mc_questions import (
    MCQuestionsError,
    MCQuestionsRequest,
    MCQuestionsResult,
    generate_mc_questions,
)
from features.outline import (
    OutlineError,
    OutlineRequest,
    OutlineResult,
    generate_outline,
)
from features.rubric_extraction import (
    RubricExtractionError,
    RubricExtractionRequest,
    RubricExtractionResult,
    extract_rubric_from_memo,
)
from features.socratic_drill import (
    SocraticDrillError,
    SocraticTurnRequest,
    SocraticTurnResult,
    socratic_next_turn,
)
from features.synthesis import (
    SynthesisError,
    SynthesisRequest,
    SynthesisResult,
    generate_synthesis,
)
from features.what_if import (
    WhatIfError,
    WhatIfRequest,
    WhatIfResult,
    generate_what_if_variations,
)
from primitives.generate import GenerateError

router = APIRouter(prefix="/features", tags=["features"])


# ---------------------------------------------------------------------------
# /features/case-brief
# ---------------------------------------------------------------------------


class CaseBriefHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required — which corpus the book lives in.")
    case_name: str | None = None
    block_id: str | None = None
    book_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    professor_profile: dict[str, Any] | None = None
    model_override: str | None = None
    force_regenerate: bool = False


class ArtifactDTO(BaseModel):
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
    def from_model(cls, a: Artifact) -> ArtifactDTO:
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


class CaseBriefHttpResponse(BaseModel):
    artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]
    verification_failed: bool


@router.post("/case-brief", response_model=CaseBriefHttpResponse)
def post_case_brief(
    payload: CaseBriefHttpRequest,
    session: Session = Depends(get_session),
) -> CaseBriefHttpResponse:
    """Generate a case brief for the named case or the given opinion block."""
    has_range = (
        payload.book_id is not None
        and payload.page_start is not None
        and payload.page_end is not None
    )
    if payload.case_name is None and payload.block_id is None and not has_range:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide one of: case_name, block_id, or (book_id + page_start + page_end).",
        )

    req = CaseBriefRequest(
        corpus_id=payload.corpus_id,
        case_name=payload.case_name,
        block_id=payload.block_id,
        book_id=payload.book_id,
        page_start=payload.page_start,
        page_end=payload.page_end,
        professor_profile=payload.professor_profile,
        model_override=payload.model_override,
        force_regenerate=payload.force_regenerate,
    )

    try:
        result: CaseBriefResult = generate_case_brief(session, req)
    except CaseBriefError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc

    return CaseBriefHttpResponse(
        artifact=ArtifactDTO.from_model(result.artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
        verification_failed=result.verification_failed,
    )


# ---------------------------------------------------------------------------
# /features/rubric-extract (spec §5.5 Path A step 2)
# ---------------------------------------------------------------------------


class RubricExtractHttpRequest(BaseModel):
    """Request body for POST /features/rubric-extract.

    ``past_exam_artifact_id`` + ``grader_memo_artifact_id`` must point to
    already-ingested artifacts of the right ``ArtifactType``. A missing /
    wrong-type id produces 404.
    """

    corpus_id: str = Field(..., description="Required — which corpus owns the exam.")
    past_exam_artifact_id: str = Field(..., description="ArtifactType=PAST_EXAM row id.")
    grader_memo_artifact_id: str = Field(
        ..., description="ArtifactType=GRADER_MEMO row id."
    )
    question_label: str = Field(..., description="e.g., 'Part II Q2'.")
    professor_profile_id: str | None = None
    force_regenerate: bool = False


class RubricExtractHttpResponse(BaseModel):
    rubric_artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]


@router.post("/rubric-extract", response_model=RubricExtractHttpResponse)
def post_rubric_extract(
    payload: RubricExtractHttpRequest,
    session: Session = Depends(get_session),
) -> RubricExtractHttpResponse:
    """Extract a ground-truth Rubric from a (past_exam, grader_memo) pair.

    Error mapping per the feature spec:
    - :class:`RubricExtractionError` → 404 (referenced artifacts not found
      or wrong type).
    - :class:`BudgetExceededError` → 402 (monthly cap hit).
    - :class:`GenerateError` → 503 (Anthropic/network failure or
      exhausted-retries schema failure).
    """
    if not payload.corpus_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="corpus_id is required.",
        )

    req = RubricExtractionRequest(
        corpus_id=payload.corpus_id,
        past_exam_artifact_id=payload.past_exam_artifact_id,
        grader_memo_artifact_id=payload.grader_memo_artifact_id,
        question_label=payload.question_label,
        professor_profile_id=payload.professor_profile_id,
        force_regenerate=payload.force_regenerate,
    )

    try:
        result: RubricExtractionResult = extract_rubric_from_memo(session, req)
    except RubricExtractionError as exc:
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

    return RubricExtractHttpResponse(
        rubric_artifact=ArtifactDTO.from_model(result.rubric_artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# /features/irac-grade (spec §5.5 — the riskiest feature)
# ---------------------------------------------------------------------------


class DetectedPatternDTO(BaseModel):
    name: str
    severity: str
    excerpt: str
    line_offset: int
    message: str


class IracGradeHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required — corpus the rubric/answer belong to.")
    rubric_artifact_id: str = Field(..., description="RUBRIC artifact id.")
    answer_markdown: str = Field(..., description="Student answer markdown.")
    professor_profile_id: str | None = None
    question_label: str | None = None
    parent_artifact_id: str | None = Field(
        default=None,
        description="Optional PRACTICE_ANSWER artifact id to link this grade to.",
    )
    force_regenerate: bool = False


class IracGradeHttpResponse(BaseModel):
    grade_artifact: ArtifactDTO
    detected_patterns: list[DetectedPatternDTO]
    rubric_coverage_passed: bool
    rubric_coverage_warnings: list[str]
    cache_hit: bool


@router.post("/irac-grade", response_model=IracGradeHttpResponse)
def post_irac_grade(
    payload: IracGradeHttpRequest,
    session: Session = Depends(get_session),
) -> IracGradeHttpResponse:
    """Grade an IRAC answer end-to-end (§5.5)."""
    req = IracGradeRequest(
        corpus_id=payload.corpus_id,
        rubric_artifact_id=payload.rubric_artifact_id,
        answer_markdown=payload.answer_markdown,
        professor_profile_id=payload.professor_profile_id,
        question_label=payload.question_label,
        parent_artifact_id=payload.parent_artifact_id,
        force_regenerate=payload.force_regenerate,
    )

    try:
        result: IracGradeResult = grade_irac_answer(session, req)
    except IracGradeError as exc:
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

    return IracGradeHttpResponse(
        grade_artifact=ArtifactDTO.from_model(result.grade_artifact),
        detected_patterns=[
            DetectedPatternDTO(
                name=p.name,
                severity=p.severity,
                excerpt=p.excerpt,
                line_offset=p.line_offset,
                message=p.message,
            )
            for p in result.detected_patterns
        ],
        rubric_coverage_passed=result.rubric_coverage_passed,
        rubric_coverage_warnings=result.rubric_coverage_warnings,
        cache_hit=result.cache_hit,
    )


# ---------------------------------------------------------------------------
# /features/hypo (spec §5.5 Path B)
# ---------------------------------------------------------------------------


class HypoHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required — corpus to ground the hypo in.")
    topics_to_cover: list[str] = Field(..., description="Topics the hypo must test.")
    professor_profile_id: str | None = None
    source_block_ids: list[str] = Field(
        default_factory=list,
        description="Optional Block ids for casebook grounding.",
    )
    issue_density_target: int = Field(default=8, ge=1, le=20)
    force_regenerate: bool = False


class HypoHttpResponse(BaseModel):
    hypo_artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]


@router.post("/hypo", response_model=HypoHttpResponse)
def post_hypo(
    payload: HypoHttpRequest,
    session: Session = Depends(get_session),
) -> HypoHttpResponse:
    """Generate a novel exam hypo + its rubric (§5.5 Path B)."""
    req = HypoRequest(
        corpus_id=payload.corpus_id,
        topics_to_cover=list(payload.topics_to_cover),
        professor_profile_id=payload.professor_profile_id,
        source_block_ids=list(payload.source_block_ids),
        issue_density_target=payload.issue_density_target,
        force_regenerate=payload.force_regenerate,
    )

    try:
        result: HypoResult = generate_hypo(session, req)
    except HypoError as exc:
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

    return HypoHttpResponse(
        hypo_artifact=ArtifactDTO.from_model(result.hypo_artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# /features/emphasis-map (spec §5.7)
# ---------------------------------------------------------------------------


class EmphasisMapHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required — corpus the transcript belongs to.")
    transcript_id: str = Field(..., description="Transcript id to analyze.")
    professor_profile_id: str | None = None
    force_regenerate: bool = False


class EmphasisItemDTO(BaseModel):
    """Serialized EmphasisItem row for the UI — the ranked emphasis list view."""

    id: str
    subject_kind: str
    subject_label: str
    minutes_on: float
    return_count: int
    hypotheticals_run: list[str]
    disclaimed: bool
    engaged_questions: int
    exam_signal_score: float
    justification: str


class EmphasisMapHttpResponse(BaseModel):
    items: list[EmphasisItemDTO]
    summary: str | None = None
    cache_hit: bool
    warnings: list[str]


@router.post("/emphasis-map", response_model=EmphasisMapHttpResponse)
def post_emphasis_map(
    payload: EmphasisMapHttpRequest,
    session: Session = Depends(get_session),
) -> EmphasisMapHttpResponse:
    """Compute / load the EmphasisMap for a transcript (§5.7).

    Error mapping:
    - :class:`EmphasisMapError` -> 404 (transcript not found / LLM output
      schema failed / missing credentials).
    - :class:`BudgetExceededError` -> 402 (monthly cap hit).
    - Any other unexpected generation/SDK error -> 503.
    """
    req = EmphasisMapRequest(
        corpus_id=payload.corpus_id,
        transcript_id=payload.transcript_id,
        professor_profile_id=payload.professor_profile_id,
        force_regenerate=payload.force_regenerate,
    )

    try:
        result: EmphasisMapResult = build_emphasis_map(session, req)
    except EmphasisMapError as exc:
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

    return EmphasisMapHttpResponse(
        items=[
            EmphasisItemDTO(
                id=item.id,
                subject_kind=item.subject_kind.value,
                subject_label=item.subject_label,
                minutes_on=item.minutes_on,
                return_count=item.return_count,
                hypotheticals_run=list(item.hypotheticals_run),
                disclaimed=item.disclaimed,
                engaged_questions=item.engaged_questions,
                exam_signal_score=item.exam_signal_score,
                justification=item.justification,
            )
            for item in result.items
        ],
        summary=result.summary,
        cache_hit=result.cache_hit,
        warnings=list(result.warnings),
    )


# ---------------------------------------------------------------------------
# /features/attack-sheet (spec §5.9)
# ---------------------------------------------------------------------------


class AttackSheetHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required — corpus the briefs live in.")
    topic: str = Field(..., description="Doctrinal topic (e.g., 'takings').")
    case_brief_artifact_ids: list[str] = Field(
        default_factory=list,
        description="CASE_BRIEF artifact ids of the controlling cases.",
    )
    emphasis_map_artifact_id: str | None = None
    professor_profile_id: str | None = None
    force_regenerate: bool = False


class AttackSheetHttpResponse(BaseModel):
    artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]


@router.post("/attack-sheet", response_model=AttackSheetHttpResponse)
def post_attack_sheet(
    payload: AttackSheetHttpRequest,
    session: Session = Depends(get_session),
) -> AttackSheetHttpResponse:
    """Generate a one-page attack sheet (§5.9)."""
    req = AttackSheetRequest(
        corpus_id=payload.corpus_id,
        topic=payload.topic,
        case_brief_artifact_ids=list(payload.case_brief_artifact_ids),
        emphasis_map_artifact_id=payload.emphasis_map_artifact_id,
        professor_profile_id=payload.professor_profile_id,
        force_regenerate=payload.force_regenerate,
    )
    try:
        result: AttackSheetResult = generate_attack_sheet(session, req)
    except AttackSheetError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    except GenerateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return AttackSheetHttpResponse(
        artifact=ArtifactDTO.from_model(result.artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# /features/synthesis (spec §5.8)
# ---------------------------------------------------------------------------


class SynthesisHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required.")
    doctrinal_area: str = Field(..., description="Doctrinal area being synthesized.")
    case_brief_artifact_ids: list[str] = Field(default_factory=list)
    professor_profile_id: str | None = None
    force_regenerate: bool = False


class SynthesisHttpResponse(BaseModel):
    artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]


@router.post("/synthesis", response_model=SynthesisHttpResponse)
def post_synthesis(
    payload: SynthesisHttpRequest,
    session: Session = Depends(get_session),
) -> SynthesisHttpResponse:
    """Generate a multi-case doctrinal synthesis (§5.8)."""
    req = SynthesisRequest(
        corpus_id=payload.corpus_id,
        doctrinal_area=payload.doctrinal_area,
        case_brief_artifact_ids=list(payload.case_brief_artifact_ids),
        professor_profile_id=payload.professor_profile_id,
        force_regenerate=payload.force_regenerate,
    )
    try:
        result: SynthesisResult = generate_synthesis(session, req)
    except SynthesisError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    except GenerateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return SynthesisHttpResponse(
        artifact=ArtifactDTO.from_model(result.artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# /features/what-if (spec §5.10)
# ---------------------------------------------------------------------------


class WhatIfHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required.")
    case_brief_artifact_id: str = Field(..., description="The case to vary.")
    num_variations: int = Field(default=5, ge=3, le=10)
    professor_profile_id: str | None = None
    force_regenerate: bool = False


class WhatIfHttpResponse(BaseModel):
    artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]


@router.post("/what-if", response_model=WhatIfHttpResponse)
def post_what_if(
    payload: WhatIfHttpRequest,
    session: Session = Depends(get_session),
) -> WhatIfHttpResponse:
    """Generate N fact-variations on one case (§5.10)."""
    req = WhatIfRequest(
        corpus_id=payload.corpus_id,
        case_brief_artifact_id=payload.case_brief_artifact_id,
        num_variations=payload.num_variations,
        professor_profile_id=payload.professor_profile_id,
        force_regenerate=payload.force_regenerate,
    )
    try:
        result: WhatIfResult = generate_what_if_variations(session, req)
    except WhatIfError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    except GenerateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return WhatIfHttpResponse(
        artifact=ArtifactDTO.from_model(result.artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# /features/outline (spec §5.11)
# ---------------------------------------------------------------------------


class OutlineHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required.")
    course: str = Field(..., description="Course name (e.g., 'Property').")
    book_id: str | None = None
    force_regenerate: bool = False


class OutlineHttpResponse(BaseModel):
    artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]
    input_artifact_count: int


@router.post("/outline", response_model=OutlineHttpResponse)
def post_outline(
    payload: OutlineHttpRequest,
    session: Session = Depends(get_session),
) -> OutlineHttpResponse:
    """Assemble a hierarchical course outline (§5.11)."""
    req = OutlineRequest(
        corpus_id=payload.corpus_id,
        course=payload.course,
        book_id=payload.book_id,
        force_regenerate=payload.force_regenerate,
    )
    try:
        result: OutlineResult = generate_outline(session, req)
    except OutlineError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    except GenerateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return OutlineHttpResponse(
        artifact=ArtifactDTO.from_model(result.artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
        input_artifact_count=result.input_artifact_count,
    )


# ---------------------------------------------------------------------------
# /features/mc-questions (spec §5.12)
# ---------------------------------------------------------------------------


class MCQuestionsHttpRequest(BaseModel):
    corpus_id: str = Field(..., description="Required.")
    topic: str = Field(..., description="Topic the questions cover.")
    book_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    case_name: str | None = None
    num_questions: int = Field(default=10, ge=1, le=20)
    professor_profile_id: str | None = None
    force_regenerate: bool = False


class MCQuestionsHttpResponse(BaseModel):
    artifact: ArtifactDTO
    cache_hit: bool
    warnings: list[str]


@router.post("/mc-questions", response_model=MCQuestionsHttpResponse)
def post_mc_questions(
    payload: MCQuestionsHttpRequest,
    session: Session = Depends(get_session),
) -> MCQuestionsHttpResponse:
    """Generate a multiple-choice question set (§5.12)."""
    req = MCQuestionsRequest(
        corpus_id=payload.corpus_id,
        topic=payload.topic,
        book_id=payload.book_id,
        page_start=payload.page_start,
        page_end=payload.page_end,
        case_name=payload.case_name,
        num_questions=payload.num_questions,
        professor_profile_id=payload.professor_profile_id,
        force_regenerate=payload.force_regenerate,
    )
    try:
        result: MCQuestionsResult = generate_mc_questions(session, req)
    except MCQuestionsError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    except GenerateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return MCQuestionsHttpResponse(
        artifact=ArtifactDTO.from_model(result.artifact),
        cache_hit=result.cache_hit,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# /features/socratic/turn  +  /features/cold-call/{turn,debrief}
#
# Both features share the SocraticTurnRequest / SocraticTurnResult shape;
# we declare one HTTP request body and one response body that serve all
# three endpoints. (Phase 5.2 + 5.7.)
# ---------------------------------------------------------------------------


class ChatTurnHttpRequest(BaseModel):
    """Body for /features/socratic/turn and /features/cold-call/turn.

    On the very first turn the caller sends ``session_id=None`` and
    ``user_answer=None``; the feature creates the session and returns its
    id alongside the opening question. On subsequent turns, ``session_id``
    is the id returned by the prior call.
    """

    corpus_id: str = Field(..., description="Required — corpus the case lives in.")
    case_block_id: str = Field(..., description="CASE_OPINION block id.")
    session_id: str | None = None
    user_answer: str | None = None
    professor_profile_id: str | None = None


class ChatTurnHttpResponse(BaseModel):
    """Response for both Socratic and cold-call turn endpoints + the debrief
    endpoint. Mirrors :class:`SocraticTurnResult` field-for-field."""

    session_id: str
    turn_index: int
    professor_turn: dict[str, Any]
    history: list[dict[str, Any]]


class ColdCallDebriefHttpRequest(BaseModel):
    session_id: str = Field(..., description="Cold-call session id to debrief.")


def _result_to_dto(result: SocraticTurnResult) -> ChatTurnHttpResponse:
    return ChatTurnHttpResponse(
        session_id=result.session_id,
        turn_index=result.turn_index,
        professor_turn=dict(result.professor_turn),
        history=list(result.history),
    )


@router.post("/socratic/turn", response_model=ChatTurnHttpResponse)
def post_socratic_turn(
    payload: ChatTurnHttpRequest,
    session: Session = Depends(get_session),
) -> ChatTurnHttpResponse:
    """Compute the next professor turn in a Socratic drill (§5.4).

    Error mapping:
    - :class:`SocraticDrillError` -> 404 (case block missing / wrong type /
      session not found / LLM output failed schema after retries).
    - :class:`BudgetExceededError` -> 402.
    """
    req = SocraticTurnRequest(
        corpus_id=payload.corpus_id,
        case_block_id=payload.case_block_id,
        session_id=payload.session_id,
        user_answer=payload.user_answer,
        professor_profile_id=payload.professor_profile_id,
    )
    try:
        result = socratic_next_turn(session, req)
    except SocraticDrillError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    return _result_to_dto(result)


@router.post("/cold-call/turn", response_model=ChatTurnHttpResponse)
def post_cold_call_turn(
    payload: ChatTurnHttpRequest,
    session: Session = Depends(get_session),
) -> ChatTurnHttpResponse:
    """Compute the next professor turn in a cold-call session (§5.6)."""
    req = SocraticTurnRequest(
        corpus_id=payload.corpus_id,
        case_block_id=payload.case_block_id,
        session_id=payload.session_id,
        user_answer=payload.user_answer,
        professor_profile_id=payload.professor_profile_id,
    )
    try:
        result = cold_call_next_turn(session, req)
    except ColdCallError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    return _result_to_dto(result)


@router.post("/cold-call/debrief", response_model=ChatTurnHttpResponse)
def post_cold_call_debrief(
    payload: ColdCallDebriefHttpRequest,
    session: Session = Depends(get_session),
) -> ChatTurnHttpResponse:
    """Final cold-call debrief turn — also marks the session ended."""
    try:
        result = cold_call_debrief(session, payload.session_id)
    except ColdCallError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc
    return _result_to_dto(result)
