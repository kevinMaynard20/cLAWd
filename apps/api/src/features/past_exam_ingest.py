"""Past-exam + grader-memo ingest (spec §9 Phase 3, §3.11).

Thin helper that takes uploaded past-exam text and an optional grader-memo and
persists them as Artifacts — no LLM call, no retrieval. Used as a preflight
before rubric extraction and professor-profile extraction (§5.13) so those
features can pull the same user-uploaded content out of the Artifact table by
id.

Design notes:

- Both resulting rows use ``ArtifactType.PAST_EXAM`` / ``GRADER_MEMO`` and
  ``CreatedBy.USER``. They bypass the ``generate()`` primitive because there's
  no prompt / model / schema to run — they're just storage.
- Cost is zero and no ``CostEvent`` is emitted. The spec reserves CostEvents
  for LLM calls (§3.12).
- ``cache_key`` is intentionally left as the default empty string: the
  generate-primitive cache is keyed on (template, model, inputs), none of
  which apply to user uploads.
- When a memo is provided, its ``content`` carries ``tied_to_past_exam`` with
  the exam artifact's id so downstream rubric extraction can find them as a
  pair via a simple JSON query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlmodel import Session

from data.models import Artifact, ArtifactType, CreatedBy

# ---------------------------------------------------------------------------
# Public request / response
# ---------------------------------------------------------------------------


@dataclass
class PastExamIngestRequest:
    """Inputs for a single past-exam ingest.

    ``source_paths`` captures the original filesystem paths the user uploaded
    from (if any) so later re-extraction can tell which artifacts already
    exist — the ProfessorProfile's ``source_artifact_paths`` field likewise
    tracks this lineage.
    """

    corpus_id: str
    exam_markdown: str
    grader_memo_markdown: str | None = None
    source_paths: list[str] = field(default_factory=list)
    year: int | None = None
    professor_name: str | None = None


@dataclass
class PastExamIngestResult:
    past_exam_artifact_id: str
    grader_memo_artifact_id: str | None


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def ingest_past_exam(
    session: Session,
    req: PastExamIngestRequest,
) -> PastExamIngestResult:
    """Persist the uploaded exam (+ optional memo) as two Artifacts.

    Returns both ids so the caller can immediately use them as inputs to
    rubric extraction or professor-profile extraction. The session is
    committed before returning.
    """
    exam_content: dict[str, Any] = {
        "markdown": req.exam_markdown,
        "year": req.year,
        "professor_name": req.professor_name,
        "source_paths": list(req.source_paths),
    }

    past_exam = Artifact(
        corpus_id=req.corpus_id,
        type=ArtifactType.PAST_EXAM,
        created_by=CreatedBy.USER,
        sources=[],
        content=exam_content,
        prompt_template="",
        llm_model="",
        cost_usd=Decimal("0"),
        cache_key="",
        regenerable=False,
    )
    session.add(past_exam)
    session.commit()
    session.refresh(past_exam)

    memo_id: str | None = None
    if req.grader_memo_markdown is not None:
        memo_content: dict[str, Any] = {
            "markdown": req.grader_memo_markdown,
            "tied_to_past_exam": past_exam.id,
            "year": req.year,
            "professor_name": req.professor_name,
            "source_paths": list(req.source_paths),
        }
        memo = Artifact(
            corpus_id=req.corpus_id,
            type=ArtifactType.GRADER_MEMO,
            created_by=CreatedBy.USER,
            sources=[],
            content=memo_content,
            parent_artifact_id=past_exam.id,
            prompt_template="",
            llm_model="",
            cost_usd=Decimal("0"),
            cache_key="",
            regenerable=False,
        )
        session.add(memo)
        session.commit()
        session.refresh(memo)
        memo_id = memo.id

    return PastExamIngestResult(
        past_exam_artifact_id=past_exam.id,
        grader_memo_artifact_id=memo_id,
    )


__all__ = [
    "PastExamIngestRequest",
    "PastExamIngestResult",
    "ingest_past_exam",
]
