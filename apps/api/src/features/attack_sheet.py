"""Attack sheet builder (spec §5.9).

Thin orchestration over :mod:`primitives.generate`:

1. Budget gate.
2. Load the named CASE_BRIEF artifacts (the controlling cases for the topic)
   and validate their type.
3. Optionally load a transcript-emphasis Artifact's items + the professor
   profile so the prompt can fold their `stable_traps` and per-subject
   `exam_signal_score` rankings into the trap list / issue-spotting triggers.
4. Render the ``attack_sheet`` template via ``generate()`` with
   ``artifact_type=ATTACK_SHEET``.
5. Return the persisted Artifact.

The ``emphasis_map_artifact_id`` field is optional and refers to either
a synthesis-style artifact carrying ranked EmphasisItems OR (more commonly)
a free-form list of items the caller assembled from the EmphasisItem rows in
:mod:`features.emphasis_mapper`. We accept any artifact with a content
``items`` field shaped like the EmphasisMap output to keep this feature
decoupled from the EmphasisMap-row table; if the schema diverges, the prompt
collapses the section.

Error mapping (surfaced at the route layer):
- :class:`AttackSheetError` -> 404 (artifact not found / wrong type / profile
  not found).
- :class:`BudgetExceededError` -> 402.
- :class:`GenerateError` -> 503.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import Artifact, ArtifactType, EmphasisItem, ProfessorProfile
from primitives.generate import GenerateError, GenerateRequest, generate


@dataclass
class AttackSheetRequest:
    corpus_id: str
    topic: str
    case_brief_artifact_ids: list[str] = field(default_factory=list)
    emphasis_map_artifact_id: str | None = None
    professor_profile_id: str | None = None
    force_regenerate: bool = False


@dataclass
class AttackSheetResult:
    artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)


class AttackSheetError(RuntimeError):
    """Feature-level failure -> route maps to 404."""


def generate_attack_sheet(
    session: Session,
    req: AttackSheetRequest,
) -> AttackSheetResult:
    """Spec §5.9. Build a one-page topic attack sheet from controlling case
    briefs + optional emphasis input + optional professor profile."""
    # 1. Budget gate.
    raise_if_over_budget()

    if not req.case_brief_artifact_ids:
        raise AttackSheetError(
            "case_brief_artifact_ids is empty — at least one CASE_BRIEF "
            "artifact is required to build an attack sheet."
        )

    # 2. Load + type-check the case brief artifacts.
    controlling_briefs: list[dict[str, Any]] = []
    for brief_id in req.case_brief_artifact_ids:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == brief_id)
        ).first()
        if artifact is None:
            raise AttackSheetError(
                f"case_brief artifact {brief_id!r} not found."
            )
        if artifact.type is not ArtifactType.CASE_BRIEF:
            raise AttackSheetError(
                f"Artifact {brief_id!r} is {artifact.type.value!r}; expected "
                "case_brief."
            )
        controlling_briefs.append(dict(artifact.content or {}))

    # 3. Optional emphasis input. If the caller supplies an artifact id, we
    #    pull the items list out of its content (loose contract — see module
    #    docstring). If they pass a transcript emphasis artifact whose content
    #    happens to live in EmphasisItem rows under another path, they should
    #    pre-flatten it.
    emphasis_items: list[dict[str, Any]] = []
    if req.emphasis_map_artifact_id is not None:
        emphasis_artifact = session.exec(
            select(Artifact).where(Artifact.id == req.emphasis_map_artifact_id)
        ).first()
        if emphasis_artifact is None:
            raise AttackSheetError(
                f"emphasis_map artifact {req.emphasis_map_artifact_id!r} "
                "not found."
            )
        raw_items = (emphasis_artifact.content or {}).get("items") or []
        if isinstance(raw_items, list):
            emphasis_items = [
                dict(item) for item in raw_items if isinstance(item, dict)
            ]

    # 4. Optional professor profile.
    profile_dict: dict[str, Any] | None = None
    if req.professor_profile_id is not None:
        profile = session.exec(
            select(ProfessorProfile).where(
                ProfessorProfile.id == req.professor_profile_id
            )
        ).first()
        if profile is None:
            raise AttackSheetError(
                f"ProfessorProfile {req.professor_profile_id!r} not found."
            )
        profile_dict = _profile_to_dict(profile)

    # 5. Generate.
    inputs: dict[str, Any] = {
        "topic": req.topic,
        "controlling_case_briefs": controlling_briefs,
        "emphasis_items": emphasis_items,
        "professor_profile": profile_dict,
    }

    try:
        result = generate(
            GenerateRequest(
                template_name="attack_sheet",
                inputs=inputs,
                artifact_type=ArtifactType.ATTACK_SHEET,
                corpus_id=req.corpus_id,
                retrieval=None,
                professor_profile=profile_dict,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError:
        raise

    return AttackSheetResult(
        artifact=result.artifact,
        cache_hit=result.cache_hit,
        warnings=list(result.validation_warnings),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile_to_dict(profile: ProfessorProfile) -> dict[str, Any]:
    """Plain-dict view of a ProfessorProfile row for Handlebars rendering."""
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


# Deliberately also exported so callers can construct items list with the
# EmphasisItem table directly when they have rows but no Artifact wrapper.
def emphasis_items_from_rows(rows: list[EmphasisItem]) -> list[dict[str, Any]]:
    """Convert :class:`EmphasisItem` SQLModel rows into the dict shape the
    attack-sheet prompt expects under ``emphasis_items``. Useful when callers
    have raw rows but no wrapping artifact."""
    return [
        {
            "subject_kind": row.subject_kind.value,
            "subject_label": row.subject_label,
            "exam_signal_score": row.exam_signal_score,
            "justification": row.justification,
        }
        for row in rows
    ]


__all__ = [
    "AttackSheetError",
    "AttackSheetRequest",
    "AttackSheetResult",
    "emphasis_items_from_rows",
    "generate_attack_sheet",
]
