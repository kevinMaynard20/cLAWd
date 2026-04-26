"""Hierarchical course outline (spec §5.11).

Thin orchestration:

1. Budget gate.
2. Gather all CASE_BRIEF + FLASHCARD_SET artifacts in the corpus.
3. Pull the book's :class:`TocEntry` rows (when ``book_id`` is supplied;
   otherwise the largest book in the corpus by page count is used).
4. Render the ``outline_hierarchical`` template.
5. Return the persisted Artifact + how many input artifacts fed the LLM.

Input-volume policy (design decision, see Q44 in SPEC_QUESTIONS.md): briefs
+ flashcards within a corpus can grow to dozens or low hundreds across a
semester. We pass them all to the prompt unfiltered and rely on the
configured ``max_tokens`` (10000) plus the prompt's "dedupe" rule to keep
the output coherent. If real corpora blow the context window, the next
revision should rank by recency / relevance rather than truncate blindly.

Error mapping:
- :class:`OutlineError` -> 404.
- :class:`BudgetExceededError` -> 402.
- :class:`GenerateError` -> 503.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import Artifact, ArtifactType, Book, TocEntry
from primitives.generate import GenerateError, GenerateRequest, generate


@dataclass
class OutlineRequest:
    corpus_id: str
    course: str
    book_id: str | None = None
    force_regenerate: bool = False


@dataclass
class OutlineResult:
    artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)
    input_artifact_count: int = 0


class OutlineError(RuntimeError):
    """Feature-level failure -> route maps to 404."""


def generate_outline(
    session: Session,
    req: OutlineRequest,
) -> OutlineResult:
    """Spec §5.11. Assemble a course outline from briefs + flashcards + TOC."""
    raise_if_over_budget()

    # 2. Gather all CASE_BRIEF + FLASHCARD_SET artifacts in the corpus.
    artifacts = list(
        session.exec(
            select(Artifact)
            .where(Artifact.corpus_id == req.corpus_id)
            .where(
                Artifact.type.in_(  # type: ignore[attr-defined]
                    (
                        ArtifactType.CASE_BRIEF,
                        ArtifactType.FLASHCARD_SET,
                    )
                )
            )
            .order_by(Artifact.created_at)  # type: ignore[arg-type]
        ).all()
    )
    case_briefs = [
        dict(a.content or {})
        for a in artifacts
        if a.type is ArtifactType.CASE_BRIEF
    ]
    flashcard_sets = [
        dict(a.content or {})
        for a in artifacts
        if a.type is ArtifactType.FLASHCARD_SET
    ]

    # 3. Resolve book + TOC.
    book = _resolve_book(session, req)
    toc_rows = list(
        session.exec(
            select(TocEntry)
            .where(TocEntry.book_id == book.id)
            .order_by(TocEntry.order_index)  # type: ignore[arg-type]
        ).all()
    ) if book is not None else []

    # The outline_hierarchical template renders TOC indentation via
    # `{{#each (range 0 this.level)}}  {{/each}}`. pybars3 has no built-in
    # ``range`` helper, so we pre-bake the indentation into each entry's
    # title and zero out ``level`` to short-circuit the `{{#if this.level}}`
    # block in the template. This keeps the TOC visually nested in the
    # rendered prompt without modifying the (frozen) template.
    toc_dicts: list[dict[str, Any]] = [
        {
            "title": ("  " * max(int(row.level) - 1, 0)) + row.title,
            "level": 0,
            "source_page": row.source_page,
        }
        for row in toc_rows
    ]

    inputs: dict[str, Any] = {
        "course": req.course,
        "toc": toc_dicts,
        "case_briefs": case_briefs,
        "flashcard_sets": flashcard_sets,
        "professor_profile": None,
        "attack_sheets": [],
    }

    try:
        result = generate(
            GenerateRequest(
                template_name="outline_hierarchical",
                inputs=inputs,
                artifact_type=ArtifactType.OUTLINE,
                corpus_id=req.corpus_id,
                retrieval=None,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError:
        raise

    return OutlineResult(
        artifact=result.artifact,
        cache_hit=result.cache_hit,
        warnings=list(result.validation_warnings),
        input_artifact_count=len(case_briefs) + len(flashcard_sets),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_book(session: Session, req: OutlineRequest) -> Book | None:
    """Resolve which book's TOC to use.

    - ``req.book_id`` set -> use that book; raise if it doesn't belong to the
      corpus.
    - ``req.book_id`` None -> pick the corpus's largest book by page count, so
      the corpus's primary casebook drives the outline structure.
    - corpus has no books -> return None and let the prompt collapse the TOC
      section. The outline is still useful when the user has only briefs and
      flashcards but no ingested book.
    """
    if req.book_id is not None:
        book = session.exec(
            select(Book).where(Book.id == req.book_id)
        ).first()
        if book is None:
            raise OutlineError(f"book {req.book_id!r} not found.")
        if book.corpus_id != req.corpus_id:
            raise OutlineError(
                f"book {req.book_id!r} does not belong to corpus "
                f"{req.corpus_id!r}."
            )
        return book

    # No explicit book — pick the largest book in the corpus by page count
    # (source_page_max - source_page_min).
    books = list(
        session.exec(
            select(Book).where(Book.corpus_id == req.corpus_id)
        ).all()
    )
    if not books:
        return None
    return max(
        books,
        key=lambda b: (b.source_page_max or 0) - (b.source_page_min or 0),
    )


__all__ = [
    "OutlineError",
    "OutlineRequest",
    "OutlineResult",
    "generate_outline",
]
