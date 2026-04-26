"""Rubric extraction from a grader memo (spec §5.5 Path A step 2).

Orchestrates ingested ``PastExam`` + ``GraderMemo`` artifacts plus an optional
``ProfessorProfile`` row into a ground-truth :class:`Rubric` artifact via the
``rubric_from_memo`` prompt template.

Contract with the upstream graders:
- The caller hands us artifact ids for existing ``PAST_EXAM`` + ``GRADER_MEMO``
  rows. We fetch their ``content["markdown"]`` fields (the convention the
  sibling past_exam_ingest feature writes).
- The professor profile is optional — when provided, we render it into the
  template as structured context so anti-pattern inheritance works (see
  ``rubric_from_memo.prompt.md`` hard rule 4). When absent we pass ``None``
  and the Handlebars ``{{#if professor_profile}}`` block collapses.
- We pass ``retrieval=None`` to ``generate()``: the memo itself IS the
  source content, not a page-range retrieval — instead every piece of
  template context lives in ``inputs``.

Error mapping intent (surfaced at the route layer):
- :class:`RubricExtractionError` → 404 (artifact not found / wrong type).
- :class:`BudgetExceededError` → 402 (bubbled from ``raise_if_over_budget``).
- :class:`GenerateError` → 503 (bubbled from ``generate()``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import Artifact, ArtifactType, ProfessorProfile
from primitives.generate import GenerateError, GenerateRequest, generate


@dataclass
class RubricExtractionRequest:
    """Inputs for extracting a rubric from a grader memo.

    The ``past_exam_artifact_id`` / ``grader_memo_artifact_id`` fields are ids
    of already-ingested artifacts; this feature does not ingest raw PDFs.
    ``question_label`` flows directly into the rubric (e.g., "Part II Q2").
    """

    corpus_id: str
    past_exam_artifact_id: str
    grader_memo_artifact_id: str
    question_label: str
    professor_profile_id: str | None = None
    force_regenerate: bool = False


@dataclass
class RubricExtractionResult:
    """Orchestration output.

    ``rubric_artifact`` is the persisted ``RUBRIC`` artifact (detached from
    the session, safe to read after teardown). ``cache_hit`` is True when the
    generate primitive short-circuited on the artifact cache. ``warnings``
    aggregates schema-coercion warnings from the generate retry path.
    """

    rubric_artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)


class RubricExtractionError(RuntimeError):
    """Feature-level failure that should surface as HTTP 404 — the caller
    referenced artifact ids that do not exist in the corpus, or that are the
    wrong :class:`ArtifactType`."""


def extract_rubric_from_memo(
    session: Session,
    req: RubricExtractionRequest,
) -> RubricExtractionResult:
    """Build a Rubric artifact from a (past_exam, grader_memo) pair.

    Steps:
      1. Budget gate — ``raise_if_over_budget`` bubbles :class:`BudgetExceededError`
         up to the route layer for a 402 response.
      2. Fetch + type-check both input artifacts.
      3. Optionally fetch the professor profile and render it to a plain dict
         (SQLModel rows don't serialize cleanly into JSON-style templates on
         their own — see ``_profile_to_dict`` below).
      4. Call ``generate()`` with ``template_name="rubric_from_memo"`` and
         ``retrieval=None``. The template reads its inputs from the ``inputs``
         dict only.
      5. Return the persisted Rubric artifact + cache-hit status + warnings.

    Args:
        session: caller-owned :class:`Session`. We use it for artifact lookup
            but ``generate()`` opens its own ``session_scope()`` for the
            cache check / persist — so we don't need to commit here.
        req: :class:`RubricExtractionRequest` — see field docs above.

    Raises:
        RubricExtractionError: past_exam / grader_memo ids don't exist or
            aren't the expected ``ArtifactType``.
        BudgetExceededError: via ``raise_if_over_budget`` — the route maps to
            402.
        GenerateError: from the ``generate()`` primitive — the route maps to
            503.
    """
    # 1. Budget gate.
    raise_if_over_budget()

    # 2. Fetch + type-check input artifacts.
    past_exam = _fetch_artifact(
        session,
        req.past_exam_artifact_id,
        expected_type=ArtifactType.PAST_EXAM,
        label="past_exam",
    )
    grader_memo = _fetch_artifact(
        session,
        req.grader_memo_artifact_id,
        expected_type=ArtifactType.GRADER_MEMO,
        label="grader_memo",
    )

    past_exam_markdown = _extract_markdown(past_exam, "past_exam")
    grader_memo_markdown = _extract_markdown(grader_memo, "grader_memo")

    # 3. Optionally resolve professor profile.
    professor_profile_dict: dict[str, Any] | None = None
    if req.professor_profile_id is not None:
        profile = session.exec(
            select(ProfessorProfile).where(
                ProfessorProfile.id == req.professor_profile_id
            )
        ).first()
        if profile is None:
            raise RubricExtractionError(
                f"ProfessorProfile {req.professor_profile_id!r} not found."
            )
        professor_profile_dict = _profile_to_dict(profile)

    # 4. Generate. The template's Handlebars reads from these input keys
    #    directly — past_exam_question, grader_memo_markdown, question_label,
    #    professor_profile (optional).
    inputs: dict[str, Any] = {
        "past_exam_question": past_exam_markdown,
        "grader_memo_markdown": grader_memo_markdown,
        "question_label": req.question_label,
        "professor_profile": professor_profile_dict,
    }

    try:
        result = generate(
            GenerateRequest(
                template_name="rubric_from_memo",
                inputs=inputs,
                artifact_type=ArtifactType.RUBRIC,
                corpus_id=req.corpus_id,
                retrieval=None,
                professor_profile=professor_profile_dict,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError:
        # Let the route layer map this to 503.
        raise

    return RubricExtractionResult(
        rubric_artifact=result.artifact,
        cache_hit=result.cache_hit,
        warnings=list(result.validation_warnings),
    )


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------


def _fetch_artifact(
    session: Session,
    artifact_id: str,
    *,
    expected_type: ArtifactType,
    label: str,
) -> Artifact:
    """Look up an artifact by id and enforce its :class:`ArtifactType`.

    ``label`` is a user-friendly slot name ("past_exam" / "grader_memo") so
    the error message points the caller at the specific field they got wrong.
    """
    found = session.exec(
        select(Artifact).where(Artifact.id == artifact_id)
    ).first()
    if found is None:
        raise RubricExtractionError(
            f"{label} artifact {artifact_id!r} not found."
        )
    if found.type is not expected_type:
        raise RubricExtractionError(
            f"Artifact {artifact_id!r} is {found.type.value!r}, expected "
            f"{expected_type.value!r} for the {label} slot."
        )
    return found


def _extract_markdown(artifact: Artifact, label: str) -> str:
    """Pull ``content["markdown"]`` out of an ingested artifact.

    The sibling past_exam_ingest feature stores the raw text under this key —
    see spec §5.5 Path A step 1. If the field is missing we raise a
    :class:`RubricExtractionError` rather than silently feeding empty text to
    the LLM (which would produce a hollow rubric).
    """
    content = artifact.content or {}
    markdown = content.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise RubricExtractionError(
            f"{label} artifact {artifact.id!r} has no markdown content to "
            f"feed the rubric extractor."
        )
    return markdown


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def _profile_to_dict(profile: ProfessorProfile) -> dict[str, Any]:
    """Convert a :class:`ProfessorProfile` row to the plain-dict shape the
    ``rubric_from_memo`` template expects.

    We intentionally serialize the pet_peeves / stable_traps / etc. lists
    verbatim — they're already JSON-shaped in the DB. Timestamps are dropped
    since the template doesn't use them and JSON-rendering a datetime inside
    Handlebars is noisy.
    """
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
    "RubricExtractionError",
    "RubricExtractionRequest",
    "RubricExtractionResult",
    "extract_rubric_from_memo",
]
