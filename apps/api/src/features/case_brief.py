"""Case-brief feature (spec §5.2).

Thin orchestration over the four primitives:

1. Budget gate (`tracker.raise_if_over_budget`).
2. Retrieve the case opinion + trailing notes via `primitives.retrieve`.
3. Generate the brief via `primitives.generate` using the `case_brief` template.
4. Verify with `citation_grounding` and `rule_fidelity` profiles.
5. Return the persisted Artifact plus soft warnings.

The feature doesn't own any data model — Artifact persistence is inside
`generate()`, and CostEvent emission is too. What this module owns is the
*shape* of a case-brief request and the wiring between primitives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import Artifact, ArtifactType, Block, BlockType
from primitives.generate import GenerateError, GenerateRequest, generate
from primitives.retrieve import CaseReferenceQuery, retrieve
from primitives.verify import verify


@dataclass
class CaseBriefRequest:
    """What the feature needs.

    At least one of these resolution paths must be supplied (most-specific
    wins): ``block_id`` > ``case_name`` (+ optional ``book_id``) > ``book_id``
    + ``page_start`` + ``page_end`` (picks the first case_opinion block in
    that range — used by the book-detail page's page-range action sidebar).
    """

    corpus_id: str
    case_name: str | None = None
    block_id: str | None = None
    book_id: str | None = None  # narrow case-name search to a specific book
    page_start: int | None = None
    page_end: int | None = None
    professor_profile: dict[str, Any] | None = None
    model_override: str | None = None
    force_regenerate: bool = False


@dataclass
class CaseBriefResult:
    """What the feature returns.

    `artifact` is the persisted case-brief Artifact. `cache_hit` is True when
    the generate primitive short-circuited on the Artifact cache. `warnings`
    collects soft warnings from both the generate retry path (schema coercions)
    and the verify pass (rule-fidelity borderline cases). `verification_failed`
    is True only when a hard-error verification issue was surfaced — the UI
    should call this out visibly; the artifact is still returned so the user
    can inspect what was produced.
    """

    artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)
    verification_failed: bool = False


class CaseBriefError(RuntimeError):
    """Feature-level failure — raised when we can't even get to `generate()`,
    e.g., case not found in corpus."""


def generate_case_brief(
    session: Session,
    req: CaseBriefRequest,
) -> CaseBriefResult:
    """Orchestrate a case brief end-to-end (spec §5.2)."""
    # 1. Budget gate. `BudgetExceededError` bubbles up to the route layer.
    raise_if_over_budget()

    # 2. Retrieve — prefer block_id when provided.
    opinion_block, trailing_blocks = _fetch_opinion_and_notes(session, req)
    if opinion_block is None:
        raise CaseBriefError(
            "Could not locate a case_opinion block for "
            f"case_name={req.case_name!r} / block_id={req.block_id!r} / "
            f"book_id={req.book_id!r}. Check that the book has been ingested "
            "and the case name matches the block_metadata.case_name."
        )

    # The case_brief prompt decides Path A (casebook source) vs Path B
    # (knowledge fallback) based on whether `case_opinion_block.markdown` is
    # substantive. We let the prompt make the call so a single LLM round
    # trip handles both paths — no preflight bail.

    # 3. Build the retrieval payload + bind `case_opinion_block` /
    #    `following_notes` explicitly. The case_brief prompt template
    #    references those names directly (it does NOT walk
    #    `retrieval_blocks`), so we have to pass them as inputs — otherwise
    #    Handlebars renders empty strings for the case name + opinion text
    #    and the LLM brings nothing useful back.
    from primitives.retrieve import RetrievalResult  # local to avoid circular import risk

    retrieval = RetrievalResult(
        query_description=f"case_brief source for {opinion_block.block_metadata.get('case_name', opinion_block.id)}",
        blocks=[opinion_block, *trailing_blocks],
        pages=[],  # pages aren't required for case_brief template; it cites blocks
    )

    # 4. Generate.
    try:
        result = generate(
            GenerateRequest(
                template_name="case_brief",
                inputs={
                    "case_opinion_block": opinion_block,
                    "following_notes": trailing_blocks,
                },
                artifact_type=ArtifactType.CASE_BRIEF,
                corpus_id=req.corpus_id,
                retrieval=retrieval,
                professor_profile=req.professor_profile,
                model_override=req.model_override,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError as exc:
        raise CaseBriefError(f"Case-brief generation failed: {exc}") from exc

    # 5. Verify. Run rule_fidelity always; skip citation_grounding when the
    # brief was generated from general knowledge (it legitimately has no
    # block ids to verify against the casebook). The user-facing artifact
    # already carries `from_general_knowledge=true` so the UI can flag it.
    warnings = list(result.validation_warnings)
    verification_failed = False

    is_knowledge_brief = bool(
        isinstance(result.artifact.content, dict)
        and result.artifact.content.get("from_general_knowledge") is True
    )
    if is_knowledge_brief:
        warnings.append(
            "Brief generated from the model's general knowledge — the casebook "
            "text for this case was not available. Cross-check key wording "
            "against the printed opinion before relying on quoted rule language."
        )
    profiles_to_run = (
        ("rule_fidelity",)
        if is_knowledge_brief
        else ("citation_grounding", "rule_fidelity")
    )

    for profile in profiles_to_run:
        vresult = verify(result.artifact, profile, session=session)
        warnings.extend(vresult.soft_warnings)
        for issue in vresult.issues:
            if issue.severity == "error":
                verification_failed = True
                warnings.append(f"[{profile}] {issue.message}")

    return CaseBriefResult(
        artifact=result.artifact,
        cache_hit=result.cache_hit,
        warnings=warnings,
        verification_failed=verification_failed,
    )


# ---------------------------------------------------------------------------
# Retrieval helper
# ---------------------------------------------------------------------------


def _fetch_opinion_and_notes(
    session: Session,
    req: CaseBriefRequest,
) -> tuple[Block | None, list[Block]]:
    """Return (opinion_block, trailing_notes). Trailing notes include narrative
    continuations and numbered_notes — see `_retrieve_case_reference` for the
    "up to next case boundary" rule we rely on.
    """
    if req.block_id is not None:
        opinion = session.exec(
            select(Block).where(Block.id == req.block_id)
        ).first()
        if opinion is None or opinion.type is not BlockType.CASE_OPINION:
            return None, []
        # Reuse the retrieve primitive by synthesizing a CaseReferenceQuery
        # with the opinion's known case_name. Simpler than duplicating the
        # "trailing blocks up to next case" logic here.
        case_name = opinion.block_metadata.get("case_name")
        if not case_name:
            return opinion, []
        retrieval = retrieve(
            session,
            CaseReferenceQuery(case_name=str(case_name), book_id=opinion.book_id),
        )
    elif req.case_name is not None:
        retrieval = retrieve(
            session,
            CaseReferenceQuery(case_name=req.case_name, book_id=req.book_id),
        )
    elif (
        req.book_id is not None
        and req.page_start is not None
        and req.page_end is not None
    ):
        # Page-range path: pick the first case_opinion block whose source_page
        # falls in [page_start, page_end]. This is what the book-detail page's
        # "Brief this case" button hits when the user has selected a range.
        opinion = session.exec(
            select(Block)
            .where(Block.book_id == req.book_id)
            .where(Block.type == BlockType.CASE_OPINION)
            .where(Block.source_page >= req.page_start)
            .where(Block.source_page <= req.page_end)
            .order_by(Block.source_page, Block.order_index)
        ).first()
        if opinion is None:
            return None, []
        case_name = opinion.block_metadata.get("case_name")
        if not case_name:
            return opinion, []
        retrieval = retrieve(
            session,
            CaseReferenceQuery(case_name=str(case_name), book_id=opinion.book_id),
        )
    else:
        raise CaseBriefError(
            "CaseBriefRequest requires one of: block_id, case_name, "
            "or (book_id + page_start + page_end)."
        )

    if not retrieval.blocks:
        return None, []

    # The retrieve primitive always puts the matching case_opinion first.
    first, *rest = retrieval.blocks
    if first.type is not BlockType.CASE_OPINION:
        return None, []
    return first, rest


__all__ = [
    "CaseBriefError",
    "CaseBriefRequest",
    "CaseBriefResult",
    "generate_case_brief",
]
