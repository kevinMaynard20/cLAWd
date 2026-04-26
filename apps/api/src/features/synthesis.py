"""Multi-case doctrinal synthesis (spec §5.8).

Thin orchestration:

1. Budget gate.
2. Load N CASE_BRIEF artifacts and validate types.
3. Optionally load the professor profile (so ``favored_framings`` can be
   rendered as professor-specific framings).
4. Render the ``doctrinal_synthesis`` template via ``generate()`` with
   ``artifact_type=SYNTHESIS``.
5. Return the persisted Artifact.

Error mapping (route layer):
- :class:`SynthesisError` -> 404.
- :class:`BudgetExceededError` -> 402.
- :class:`GenerateError` -> 503.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import Artifact, ArtifactType, ProfessorProfile
from primitives.generate import GenerateError, GenerateRequest, generate


@dataclass
class SynthesisRequest:
    corpus_id: str
    doctrinal_area: str
    case_brief_artifact_ids: list[str] = field(default_factory=list)
    professor_profile_id: str | None = None
    force_regenerate: bool = False


@dataclass
class SynthesisResult:
    artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)


class SynthesisError(RuntimeError):
    """Feature-level failure -> route maps to 404."""


def generate_synthesis(
    session: Session,
    req: SynthesisRequest,
) -> SynthesisResult:
    """Spec §5.8. Synthesize how N cases fit together doctrinally."""
    raise_if_over_budget()

    if not req.case_brief_artifact_ids:
        raise SynthesisError(
            "case_brief_artifact_ids is empty — synthesis needs at least one "
            "CASE_BRIEF artifact (typically two or more)."
        )

    case_briefs: list[dict[str, Any]] = []
    for brief_id in req.case_brief_artifact_ids:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == brief_id)
        ).first()
        if artifact is None:
            raise SynthesisError(
                f"case_brief artifact {brief_id!r} not found."
            )
        if artifact.type is not ArtifactType.CASE_BRIEF:
            raise SynthesisError(
                f"Artifact {brief_id!r} is {artifact.type.value!r}; expected "
                "case_brief."
            )
        case_briefs.append(dict(artifact.content or {}))

    profile_dict: dict[str, Any] | None = None
    if req.professor_profile_id is not None:
        profile = session.exec(
            select(ProfessorProfile).where(
                ProfessorProfile.id == req.professor_profile_id
            )
        ).first()
        if profile is None:
            raise SynthesisError(
                f"ProfessorProfile {req.professor_profile_id!r} not found."
            )
        profile_dict = _profile_to_dict(profile)

    inputs: dict[str, Any] = {
        "doctrinal_area": req.doctrinal_area,
        "case_briefs": case_briefs,
        "professor_profile": profile_dict,
    }

    try:
        result = generate(
            GenerateRequest(
                template_name="doctrinal_synthesis",
                inputs=inputs,
                artifact_type=ArtifactType.SYNTHESIS,
                corpus_id=req.corpus_id,
                retrieval=None,
                professor_profile=profile_dict,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError:
        raise

    return SynthesisResult(
        artifact=result.artifact,
        cache_hit=result.cache_hit,
        warnings=list(result.validation_warnings),
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
    "SynthesisError",
    "SynthesisRequest",
    "SynthesisResult",
    "generate_synthesis",
]
