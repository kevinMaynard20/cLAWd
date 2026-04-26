"""What-if fact-pattern variations on a single case (spec §5.10).

Spec ambiguity (logged as Q43): no dedicated :class:`ArtifactType` enum value
exists for what-if variations. We reuse ``ArtifactType.SYNTHESIS`` and stamp
``content["kind"] = "what_if_variations"`` as a sub-discriminator. Callers
filtering for "real" multi-case syntheses should check both
``a.type == SYNTHESIS`` AND ``a.content.get("kind") != "what_if_variations"``.

Thin orchestration:
1. Budget gate.
2. Load the single CASE_BRIEF artifact.
3. Optionally load the professor profile (its ``stable_traps`` aligns at
   least one variation per the prompt's hard rules).
4. Render the ``what_if_variations`` template via ``generate()``.
5. Tag the persisted Artifact's content with ``kind="what_if_variations"``
   and return.

Error mapping:
- :class:`WhatIfError` -> 404.
- :class:`BudgetExceededError` -> 402.
- :class:`GenerateError` -> 503.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.db import session_scope
from data.models import Artifact, ArtifactType, ProfessorProfile
from primitives.generate import GenerateError, GenerateRequest, generate

# Content-discriminator value stamped onto ``Artifact.content["kind"]``.
WHAT_IF_KIND = "what_if_variations"


@dataclass
class WhatIfRequest:
    corpus_id: str
    case_brief_artifact_id: str
    num_variations: int = 5
    professor_profile_id: str | None = None
    force_regenerate: bool = False


@dataclass
class WhatIfResult:
    artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)


class WhatIfError(RuntimeError):
    """Feature-level failure -> route maps to 404."""


def generate_what_if_variations(
    session: Session,
    req: WhatIfRequest,
) -> WhatIfResult:
    """Spec §5.10. Generate N fact-variations on one case."""
    raise_if_over_budget()

    case_brief = session.exec(
        select(Artifact).where(Artifact.id == req.case_brief_artifact_id)
    ).first()
    if case_brief is None:
        raise WhatIfError(
            f"case_brief artifact {req.case_brief_artifact_id!r} not found."
        )
    if case_brief.type is not ArtifactType.CASE_BRIEF:
        raise WhatIfError(
            f"Artifact {req.case_brief_artifact_id!r} is "
            f"{case_brief.type.value!r}; expected case_brief."
        )

    profile_dict: dict[str, Any] | None = None
    if req.professor_profile_id is not None:
        profile = session.exec(
            select(ProfessorProfile).where(
                ProfessorProfile.id == req.professor_profile_id
            )
        ).first()
        if profile is None:
            raise WhatIfError(
                f"ProfessorProfile {req.professor_profile_id!r} not found."
            )
        profile_dict = _profile_to_dict(profile)

    inputs: dict[str, Any] = {
        "case_brief": dict(case_brief.content or {}),
        "num_variations": int(req.num_variations),
        "professor_profile": profile_dict,
    }

    try:
        result = generate(
            GenerateRequest(
                template_name="what_if_variations",
                inputs=inputs,
                artifact_type=ArtifactType.SYNTHESIS,  # see Q43
                corpus_id=req.corpus_id,
                retrieval=None,
                professor_profile=profile_dict,
                force_regenerate=req.force_regenerate,
                parent_artifact_id=case_brief.id,
            )
        )
    except GenerateError:
        raise

    artifact = result.artifact
    # Stamp the sub-kind discriminator on the persisted artifact unless it's
    # already there (cache hit on a previously stamped row).
    if artifact.content.get("kind") != WHAT_IF_KIND:
        new_content = dict(artifact.content or {})
        new_content["kind"] = WHAT_IF_KIND
        # Persist the discriminator in-place. We open a fresh scope rather
        # than relying on the caller's session because ``generate()`` opened
        # its own scope and expunged the row.
        with session_scope() as inner:
            row = inner.get(Artifact, artifact.id)
            if row is not None:
                row.content = new_content
                inner.add(row)
                inner.commit()
                inner.refresh(row)
                inner.expunge(row)
                artifact = row
            else:  # pragma: no cover — should be unreachable
                artifact.content = new_content

    return WhatIfResult(
        artifact=artifact,
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
    "WHAT_IF_KIND",
    "WhatIfError",
    "WhatIfRequest",
    "WhatIfResult",
    "generate_what_if_variations",
]
