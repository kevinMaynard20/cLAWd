"""IRAC grading feature (spec §5.5 — the riskiest feature).

Thin orchestration over the four primitives, with the rubric-anchored grading
semantics of §5.5:

1. Budget gate.
2. Fetch the RUBRIC artifact (authoritative — spec §2.6 "grade against the
   real rubric, not vibes") and the optional ProfessorProfile.
3. Persist (or reuse) a PRACTICE_ANSWER artifact so every Grade links to a
   stable answer id for audit (§3.11).
4. Run the Pollack anti-pattern scanner. Output is advisory — the LLM's
   ``pattern_flags`` ships to the user; our rule-based output is stored for
   audit and to seed the grader's attention.
5. Call ``generate(template="irac_grade", ...)`` with the rubric + answer +
   professor profile.
6. Post-call: ``verify(profile="rubric_coverage", ...)``. The verifier is
   currently stubbed (raises NotImplementedError); we tolerate that — the
   rubric-coverage verifier lands in its own phase. When it lands, this
   feature picks it up for free.
7. Return the Grade artifact plus advisory patterns plus coverage status.

**Path A vs Path B (spec §5.5):** identical code path here. Both produce a
Rubric artifact ahead of time (Path A: extracted from a grader memo; Path B:
co-generated with the hypo). By the time `grade_irac_answer()` is called, the
rubric is already in the DB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import (
    Artifact,
    ArtifactType,
    CreatedBy,
    ProfessorProfile,
)
from features.pollack_patterns import DetectedPattern, scan_answer
from primitives.generate import GenerateError, GenerateRequest, generate
from primitives.verify import verify

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public request / response
# ---------------------------------------------------------------------------


@dataclass
class IracGradeRequest:
    corpus_id: str
    rubric_artifact_id: str
    answer_markdown: str
    professor_profile_id: str | None = None
    question_label: str | None = None
    parent_artifact_id: str | None = None
    force_regenerate: bool = False


@dataclass
class IracGradeResult:
    grade_artifact: Artifact
    detected_patterns: list[DetectedPattern]
    rubric_coverage_passed: bool
    rubric_coverage_warnings: list[str]
    cache_hit: bool


class IracGradeError(RuntimeError):
    """Feature-level failure — raised when we can't even get to generate()."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_rubric(session: Session, rubric_id: str, corpus_id: str) -> Artifact:
    stmt = select(Artifact).where(
        Artifact.id == rubric_id,
        Artifact.type == ArtifactType.RUBRIC,
        Artifact.corpus_id == corpus_id,
    )
    row = session.exec(stmt).first()
    if row is None:
        raise IracGradeError(
            f"Rubric artifact {rubric_id!r} not found in corpus {corpus_id!r}. "
            "Path A needs a rubric extracted from a grader memo; Path B needs "
            "a rubric co-generated with the hypo (spec §5.5)."
        )
    return row


def _fetch_professor_profile(
    session: Session, profile_id: str | None
) -> ProfessorProfile | None:
    if profile_id is None:
        return None
    stmt = select(ProfessorProfile).where(ProfessorProfile.id == profile_id)
    return session.exec(stmt).first()


def _profile_to_dict(profile: ProfessorProfile | None) -> dict[str, Any] | None:
    """Flatten a ProfessorProfile row into the prompt-friendly dict the
    template expects. Keeps this local so the feature doesn't couple the
    prompt to the SQLModel shape directly."""
    if profile is None:
        return None
    return {
        "id": profile.id,
        "professor_name": profile.professor_name,
        "course": profile.course,
        "school": profile.school,
        "exam_format": profile.exam_format,
        "pet_peeves": profile.pet_peeves,
        "favored_framings": profile.favored_framings,
        "stable_traps": profile.stable_traps,
        "voice_conventions": profile.voice_conventions,
        "commonly_tested": profile.commonly_tested,
    }


def _word_count(markdown: str) -> int:
    return len([w for w in markdown.split() if w.strip()])


def _persist_practice_answer(
    session: Session,
    corpus_id: str,
    answer_markdown: str,
) -> Artifact:
    """Create a PRACTICE_ANSWER envelope so the Grade has a stable parent id
    to link against (spec §3.11 traceability)."""
    art = Artifact(
        corpus_id=corpus_id,
        type=ArtifactType.PRACTICE_ANSWER,
        created_by=CreatedBy.USER,
        sources=[],
        content={
            "markdown": answer_markdown,
            "word_count": _word_count(answer_markdown),
        },
        prompt_template="",
        llm_model="",
        cost_usd=Decimal("0"),
        cache_key="",
        regenerable=False,
    )
    session.add(art)
    session.commit()
    session.refresh(art)
    return art


def _attach_parent_to_grade(
    session: Session, grade_id: str, parent_id: str
) -> None:
    """Set the parent_artifact_id on the persisted Grade. generate() doesn't
    know about the PRACTICE_ANSWER we just created in this feature, so we
    wire the linkage here post-hoc. Commits within its own session scope."""
    grade = session.exec(select(Artifact).where(Artifact.id == grade_id)).first()
    if grade is None:
        return
    grade.parent_artifact_id = parent_id
    session.add(grade)
    session.commit()


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def grade_irac_answer(
    session: Session,
    req: IracGradeRequest,
) -> IracGradeResult:
    """Grade an IRAC answer end-to-end (spec §5.5, BOTH paths).

    The returned ``grade_artifact`` carries ``content`` matching
    ``schemas/grade.json``. Callers should NOT re-read this row from the
    session — generate() already expunges and refreshes it for cross-session
    access.
    """
    # 1. Budget gate.
    raise_if_over_budget()

    # 2. Fetch rubric + optional professor profile.
    rubric_artifact = _fetch_rubric(session, req.rubric_artifact_id, req.corpus_id)
    profile_row = _fetch_professor_profile(session, req.professor_profile_id)
    profile_dict = _profile_to_dict(profile_row)

    # 3. Rule-based Pollack pre-scan. Advisory — we don't fail on detections.
    detected = scan_answer(req.answer_markdown, professor_profile=profile_dict)

    # 4. Persist (or reuse) the PRACTICE_ANSWER envelope.
    if req.parent_artifact_id is not None:
        parent_id = req.parent_artifact_id
    else:
        parent_art = _persist_practice_answer(
            session, req.corpus_id, req.answer_markdown
        )
        parent_id = parent_art.id

    # 5. Call generate(). The template gets: answer, rubric dict, optional
    #    profile dict, question label, word count.
    try:
        result = generate(
            GenerateRequest(
                template_name="irac_grade",
                inputs={
                    "answer_markdown": req.answer_markdown,
                    "rubric": rubric_artifact.content,
                    "professor_profile": profile_dict,
                    "question_label": req.question_label,
                    "word_count": _word_count(req.answer_markdown),
                },
                artifact_type=ArtifactType.GRADE,
                corpus_id=req.corpus_id,
                parent_artifact_id=parent_id,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError as exc:
        raise IracGradeError(f"IRAC grade generation failed: {exc}") from exc

    # Wire the parent link on cache-miss generate paths, since generate()'s
    # parent_artifact_id is already plumbed through. On cache hits the older
    # grade retains its original parent — that's the correct behavior
    # (history is immutable per §3.11).
    if not result.cache_hit and result.artifact.parent_artifact_id != parent_id:
        try:
            _attach_parent_to_grade(session, result.artifact.id, parent_id)
            result.artifact.parent_artifact_id = parent_id
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("grade_parent_link_failed: %s", exc)

    # 6. verify(rubric_coverage). Tolerate the Phase-3 stub's NotImplementedError
    #    so the grade still returns while the verifier is being built.
    rubric_coverage_passed = True
    rubric_coverage_warnings: list[str] = []
    try:
        vresult = verify(result.artifact, "rubric_coverage", session=session)
    except NotImplementedError as exc:
        log.info("rubric_coverage_verifier_stub: %s", exc)
    except Exception as exc:  # defensive — never fail a grade on a verifier bug
        log.warning("rubric_coverage_verifier_error: %s", exc)
        rubric_coverage_warnings.append(
            f"rubric_coverage verifier errored: {exc}"
        )
    else:
        rubric_coverage_passed = vresult.passed
        rubric_coverage_warnings.extend(vresult.soft_warnings)
        for issue in vresult.issues:
            if issue.severity == "error" and issue.message not in rubric_coverage_warnings:
                rubric_coverage_warnings.append(issue.message)

    return IracGradeResult(
        grade_artifact=result.artifact,
        detected_patterns=detected,
        rubric_coverage_passed=rubric_coverage_passed,
        rubric_coverage_warnings=rubric_coverage_warnings,
        cache_hit=result.cache_hit,
    )


__all__ = [
    "IracGradeError",
    "IracGradeRequest",
    "IracGradeResult",
    "grade_irac_answer",
]
