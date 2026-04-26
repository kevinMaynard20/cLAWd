"""Hypo generation feature (spec §5.5 Path B).

Produces a novel exam-style hypothetical AND its rubric simultaneously —
the co-generation is how we guarantee rubric coverage without a second
reconciliation pass (§5.5: "rubric and hypo are generated together so
rubric coverage is guaranteed").

Thin orchestration:

1. Budget gate.
2. Load the optional professor profile (§3.7) and the optional casebook
   blocks the caller named as grounding (§5.5 "ground in the source
   material when provided").
3. ``generate(template="hypo_generation", ...)`` with artifact_type=HYPO.
4. Return the resulting artifact — content holds both ``hypo`` (the fact
   pattern + prompt) and ``rubric`` (the ground-truth grading rubric).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import Artifact, ArtifactType, Block, Corpus, ProfessorProfile
from primitives.generate import GenerateError, GenerateRequest, generate
from primitives.retrieve import RetrievalResult
from primitives.verify import verify

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public request / response
# ---------------------------------------------------------------------------


@dataclass
class HypoRequest:
    corpus_id: str
    topics_to_cover: list[str]
    professor_profile_id: str | None = None
    source_block_ids: list[str] = field(default_factory=list)
    issue_density_target: int = 8
    force_regenerate: bool = False


@dataclass
class HypoResult:
    hypo_artifact: Artifact
    cache_hit: bool
    warnings: list[str]


class HypoError(RuntimeError):
    """Feature-level failure — raised when we can't even get to generate()."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_corpus_name(session: Session, corpus_id: str) -> str:
    row = session.exec(select(Corpus).where(Corpus.id == corpus_id)).first()
    if row is None:
        raise HypoError(f"Corpus {corpus_id!r} not found.")
    return row.name


def _fetch_professor_profile(
    session: Session, profile_id: str | None
) -> ProfessorProfile | None:
    if profile_id is None:
        return None
    return session.exec(
        select(ProfessorProfile).where(ProfessorProfile.id == profile_id)
    ).first()


def _profile_to_dict(profile: ProfessorProfile | None) -> dict[str, Any] | None:
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


def _fetch_source_blocks(
    session: Session, block_ids: list[str]
) -> list[Block]:
    if not block_ids:
        return []
    rows = list(session.exec(select(Block).where(Block.id.in_(block_ids))).all())
    # Preserve caller order so the grounding context reads in the order the
    # caller intended (e.g., matching how the casebook presents the material).
    by_id = {b.id: b for b in rows}
    return [by_id[bid] for bid in block_ids if bid in by_id]


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def generate_hypo(session: Session, req: HypoRequest) -> HypoResult:
    """§5.5 Path B. Generates a hypo artifact whose content carries both the
    fact pattern and a pre-built rubric matching ``schemas/rubric.json``."""
    raise_if_over_budget()

    corpus_name = _fetch_corpus_name(session, req.corpus_id)
    profile_row = _fetch_professor_profile(session, req.professor_profile_id)
    profile_dict = _profile_to_dict(profile_row)

    source_blocks = _fetch_source_blocks(session, req.source_block_ids)

    # Build retrieval input only when source blocks were named — the prompt
    # renders a "source context" block only when present.
    retrieval: RetrievalResult | None = None
    if source_blocks:
        retrieval = RetrievalResult(
            query_description=(
                f"hypo-generation grounding for topics={req.topics_to_cover}"
            ),
            blocks=list(source_blocks),
            pages=[],
        )

    # Template also expects a `source_blocks` variable (list of dicts) per the
    # hypo_generation prompt; we pass it through `inputs` so the renderer sees
    # the same payload via both avenues. Empty list renders nothing.
    source_blocks_dict: list[dict[str, Any]] = [
        {
            "id": b.id,
            "source_page": b.source_page,
            "markdown": b.markdown,
        }
        for b in source_blocks
    ]

    try:
        result = generate(
            GenerateRequest(
                template_name="hypo_generation",
                inputs={
                    "corpus_name": corpus_name,
                    "topics_to_cover": list(req.topics_to_cover),
                    "source_blocks": source_blocks_dict,
                    "issue_density_target": req.issue_density_target,
                },
                artifact_type=ArtifactType.HYPO,
                corpus_id=req.corpus_id,
                retrieval=retrieval,
                professor_profile=profile_dict,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError as exc:
        raise HypoError(f"Hypo generation failed: {exc}") from exc

    # verify(issue_spotting_completeness): rule-based sanity check that the
    # embedded rubric is well-formed. Tolerate a verifier stub / bug so we
    # never fail a hypo on the verifier path.
    warnings = list(result.validation_warnings)
    try:
        vresult = verify(result.artifact, "issue_spotting_completeness", session=session)
    except NotImplementedError:
        pass
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("issue_spotting_completeness_verifier_error: %s", exc)
    else:
        warnings.extend(vresult.soft_warnings)
        for issue in vresult.issues:
            if issue.severity == "error" and issue.message not in warnings:
                warnings.append(f"[issue_spotting_completeness] {issue.message}")

    return HypoResult(
        hypo_artifact=result.artifact,
        cache_hit=result.cache_hit,
        warnings=warnings,
    )


__all__ = [
    "HypoError",
    "HypoRequest",
    "HypoResult",
    "generate_hypo",
]
