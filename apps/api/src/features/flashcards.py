"""Flashcards feature (spec §5.3).

Two responsibilities live here:

1. **Generation** — orchestrate retrieval + ``generate()`` with the
   ``flashcards`` template into a ``FLASHCARD_SET`` artifact. Seed one
   :class:`FlashcardReview` row per card so the SM-2 scheduler has somewhere
   to record state.
2. **Scheduling** — pure SM-2 transitions (``apply_sm2``), the persistence
   wrapper that records a review (``record_review``), and the due-queue
   reader (``due_cards``).

Why we keep the SM-2 algorithm in this module rather than a sibling
``scheduler.py``: the algorithm is short, the spec lists flashcards + SM-2
together as one feature, and keeping them co-located means future tweaks
(e.g., the "Anki-style" lapse handling some teams switch to) live next to
the surface that exercises them.

References:
- SM-2 algorithm: https://en.wikipedia.org/wiki/SuperMemo#Description_of_SM-2_algorithm
- spec §5.3: persistence is per-card per-user; we treat the user as
  implicit (single-user local app, spec §7.6) so the rows key on
  ``(flashcard_set_id, card_id)`` only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    FlashcardReview,
    Page,
    ProfessorProfile,
)
from primitives.generate import GenerateError, GenerateRequest, generate
from primitives.retrieve import (
    CaseReferenceQuery,
    PageRangeQuery,
    RetrievalResult,
    retrieve,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public request / response types
# ---------------------------------------------------------------------------


@dataclass
class FlashcardGenerateRequest:
    """What `generate_flashcards` needs.

    At least one of (``page_start``+``page_end``+``book_id``) or ``case_name``
    must be supplied so we have something to retrieve. ``topic`` flows into
    the prompt template as the headline label the cards are organized under.
    """

    corpus_id: str
    topic: str
    book_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    case_name: str | None = None
    professor_profile_id: str | None = None
    force_regenerate: bool = False


@dataclass
class FlashcardGenerateResult:
    """Outcome of a flashcard-set generation request."""

    artifact: Artifact
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)


class FlashcardsError(RuntimeError):
    """Feature-level failure raised before we reach generate() — e.g.,
    invalid retrieval inputs, unknown professor profile, or no source blocks
    found for the given page range / case name."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _resolve_book_id(session: Session, req: FlashcardGenerateRequest) -> str | None:
    """If the caller didn't pin a book, pick the corpus's primary (oldest) book.

    Mirrors the heuristic used elsewhere in the codebase (see
    ``retrieve._retrieve_assignment_code``) — a SyllabusEntry doesn't carry a
    book_id either; one corpus typically has one casebook in v1.
    """
    if req.book_id is not None:
        return req.book_id
    book = session.exec(
        select(Book).where(Book.corpus_id == req.corpus_id).order_by(Book.ingested_at)
    ).first()
    return book.id if book is not None else None


def _build_retrieval(
    session: Session, req: FlashcardGenerateRequest
) -> RetrievalResult:
    """Dispatch to PageRangeQuery or CaseReferenceQuery based on what the
    caller supplied. Page range wins when both are set — it's the more
    specific signal."""
    if req.page_start is not None and req.page_end is not None:
        book_id = _resolve_book_id(session, req)
        if book_id is None:
            raise FlashcardsError(
                f"No book found for corpus {req.corpus_id!r}; cannot resolve "
                "page range. Ingest a casebook first."
            )
        return retrieve(
            session,
            PageRangeQuery(book_id=book_id, start=req.page_start, end=req.page_end),
        )
    if req.case_name is not None:
        return retrieve(
            session,
            CaseReferenceQuery(case_name=req.case_name, book_id=req.book_id),
        )
    raise FlashcardsError(
        "FlashcardGenerateRequest requires either page_start+page_end or "
        "case_name to retrieve source blocks."
    )


def _fetch_professor_profile(
    session: Session, profile_id: str | None
) -> ProfessorProfile | None:
    if profile_id is None:
        return None
    profile = session.exec(
        select(ProfessorProfile).where(ProfessorProfile.id == profile_id)
    ).first()
    if profile is None:
        raise FlashcardsError(f"ProfessorProfile {profile_id!r} not found.")
    return profile


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


def _extract_card_ids(content: dict[str, Any]) -> list[str]:
    """Pull the slug ids out of a generated FlashcardSet payload.

    The schema (``schemas/flashcards.json``) guarantees ``content["cards"]``
    is a list of objects each with an ``id`` field. We defensively skip any
    card lacking an id rather than failing the whole seed, since the caller
    has already accepted the artifact through generate()'s schema validation.
    """
    raw = content.get("cards") or []
    out: list[str] = []
    for card in raw:
        if isinstance(card, dict):
            cid = card.get("id")
            if isinstance(cid, str) and cid:
                out.append(cid)
    return out


def _seed_review_rows(
    session: Session,
    *,
    flashcard_set_id: str,
    corpus_id: str,
    card_ids: list[str],
    now: datetime,
) -> int:
    """Upsert a FlashcardReview row for each card.

    Upsert semantics: existing rows (matched by ``(set_id, card_id)``) are
    left alone — their accumulated SM-2 state is the user's review history
    and we don't want a regenerate to wipe it. New cards get a fresh row
    due immediately so the user sees them in the next due-queue read.
    Returns the number of NEW rows inserted (not updated)."""
    if not card_ids:
        return 0

    existing = session.exec(
        select(FlashcardReview)
        .where(FlashcardReview.flashcard_set_id == flashcard_set_id)
    ).all()
    existing_ids = {row.card_id for row in existing}

    inserted = 0
    for card_id in card_ids:
        if card_id in existing_ids:
            continue
        row = FlashcardReview(
            flashcard_set_id=flashcard_set_id,
            card_id=card_id,
            corpus_id=corpus_id,
            ease_factor=2.5,
            interval_days=0,
            repetitions=0,
            due_at=now,
            last_reviewed_at=None,
            last_grade=None,
        )
        session.add(row)
        inserted += 1
    if inserted:
        session.commit()
    return inserted


# ---------------------------------------------------------------------------
# Generation entrypoint
# ---------------------------------------------------------------------------


def generate_flashcards(
    session: Session, req: FlashcardGenerateRequest
) -> FlashcardGenerateResult:
    """End-to-end flashcard-set generation per spec §5.3.

    Steps:
      1. Budget gate.
      2. Retrieve source blocks (PageRange or CaseReference).
      3. ``generate(template="flashcards", artifact_type=FLASHCARD_SET)``.
      4. Seed FlashcardReview rows for every card (due_at=now).
      5. Return the artifact + cache-hit status.

    Raises:
        FlashcardsError: invalid retrieval inputs / unknown profile / no
            source blocks. Mapped to 404 at the route layer.
        BudgetExceededError: monthly cap hit. Mapped to 402.
        GenerateError: LLM/schema failure. Mapped to 503.
    """
    raise_if_over_budget()

    retrieval = _build_retrieval(session, req)
    if retrieval.empty:
        raise FlashcardsError(
            "Retrieval returned no blocks for the requested source. "
            f"notes={retrieval.notes}"
        )

    profile_row = _fetch_professor_profile(session, req.professor_profile_id)
    profile_dict = _profile_to_dict(profile_row)

    try:
        gen_result = generate(
            GenerateRequest(
                template_name="flashcards",
                inputs={
                    "topic": req.topic,
                    # The flashcards prompt iterates `blocks`, not `retrieval_blocks`.
                    "blocks": [
                        {
                            "id": b.id,
                            "source_page": b.source_page,
                            "type": b.type.value if hasattr(b.type, "value") else str(b.type),
                            "markdown": b.markdown,
                        }
                        for b in retrieval.blocks
                    ],
                },
                artifact_type=ArtifactType.FLASHCARD_SET,
                corpus_id=req.corpus_id,
                retrieval=retrieval,
                professor_profile=profile_dict,
                force_regenerate=req.force_regenerate,
            )
        )
    except GenerateError:
        raise

    # Seed review rows for every card. On cache hit this is a no-op for
    # already-seen cards; new cards (e.g., a regenerate that produced the
    # same artifact id but with new card slugs would be unusual but is
    # handled) get fresh rows.
    card_ids = _extract_card_ids(gen_result.artifact.content)
    inserted = _seed_review_rows(
        session,
        flashcard_set_id=gen_result.artifact.id,
        corpus_id=req.corpus_id,
        card_ids=card_ids,
        now=_utcnow(),
    )
    if inserted:
        log.info(
            "flashcard_review_seed_inserted",
            extra={
                "flashcard_set_id": gen_result.artifact.id,
                "card_count": len(card_ids),
                "inserted": inserted,
            },
        )

    return FlashcardGenerateResult(
        artifact=gen_result.artifact,
        cache_hit=gen_result.cache_hit,
        warnings=list(gen_result.validation_warnings),
    )


# ---------------------------------------------------------------------------
# SM-2 scheduler — pure function. No DB.
# ---------------------------------------------------------------------------


_SM2_EF_MIN = 1.3  # SuperMemo's lower bound on the ease factor.
_REMEMBER_THRESHOLD = 3  # quality < 3 = forgotten


def apply_sm2(
    state: FlashcardReview, grade: int, now: datetime
) -> FlashcardReview:
    """SuperMemo-2 state transition.

    Args:
        state: the current schedule for one card.
        grade: quality of recall, 0..5 inclusive (per SM-2's q variable).
        now: timestamp to anchor the next due_at off — passed in for
            test determinism.

    Returns:
        A NEW :class:`FlashcardReview` with the updated SM-2 fields. The
        caller is responsible for persisting it (see ``record_review``);
        we don't mutate ``state`` in place so the caller can compare
        before/after if needed.

    Algorithm:
        # Quality-adjusted ease factor (only when remembered, but applied
        # before the interval branch so the new ef is consistent with the
        # interval just computed):
        ef' = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
        ef' = max(1.3, ef')

        # Interval schedule on remember (q >= 3):
        reps == 0: interval = 1
        reps == 1: interval = 6
        reps >= 2: interval = round(prev_interval * ef')

        # On forget (q < 3):
        reps = 0
        interval = 1
        ef unchanged
    """
    if not 0 <= grade <= 5:
        raise ValueError(f"grade must be in [0, 5], got {grade}")

    prev_ef = state.ease_factor
    prev_interval = state.interval_days
    prev_reps = state.repetitions

    if grade < _REMEMBER_THRESHOLD:
        # Lapse: reset reps + interval, keep ef. SM-2 leaves ef alone on
        # forget; the original Wozniak paper updates ef every grade, but
        # the spec spec'd "ef unchanged" so we follow the spec.
        new_ef = prev_ef
        new_reps = 0
        new_interval = 1
    else:
        # Update ease factor first.
        delta = 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)
        new_ef = max(_SM2_EF_MIN, prev_ef + delta)

        # Schedule.
        if prev_reps == 0:
            new_interval = 1
        elif prev_reps == 1:
            new_interval = 6
        else:
            # Round to whole days — SQLite stores int, and "1.5 days from
            # now" doesn't have a meaningful interpretation in SM-2's
            # daily-rep cadence.
            new_interval = max(1, round(prev_interval * new_ef))
        new_reps = prev_reps + 1

    next_due = now + timedelta(days=new_interval)

    return FlashcardReview(
        id=state.id,
        flashcard_set_id=state.flashcard_set_id,
        card_id=state.card_id,
        corpus_id=state.corpus_id,
        ease_factor=new_ef,
        interval_days=new_interval,
        repetitions=new_reps,
        due_at=next_due,
        last_reviewed_at=now,
        last_grade=grade,
    )


# ---------------------------------------------------------------------------
# Persistence wrappers
# ---------------------------------------------------------------------------


def record_review(
    session: Session, set_id: str, card_id: str, grade: int
) -> FlashcardReview:
    """Lookup or create the FlashcardReview, apply SM-2, persist, return.

    "Or create" handles the edge case where the seeding step missed a card
    (e.g., a card was added to a set after seeding) — we don't want a
    review attempt on a real card to fail. The synthesized row picks up
    ``corpus_id`` from the parent FLASHCARD_SET artifact.
    """
    if not 0 <= grade <= 5:
        raise ValueError(f"grade must be in [0, 5], got {grade}")

    row = session.exec(
        select(FlashcardReview)
        .where(FlashcardReview.flashcard_set_id == set_id)
        .where(FlashcardReview.card_id == card_id)
    ).first()

    if row is None:
        # Lazy-create: pull corpus_id from the artifact envelope.
        artifact = session.exec(
            select(Artifact).where(Artifact.id == set_id)
        ).first()
        if artifact is None:
            raise FlashcardsError(
                f"Flashcard set {set_id!r} not found."
            )
        if artifact.type != ArtifactType.FLASHCARD_SET:
            raise FlashcardsError(
                f"Artifact {set_id!r} is type {artifact.type.value!r}, "
                "expected flashcard_set."
            )
        row = FlashcardReview(
            flashcard_set_id=set_id,
            card_id=card_id,
            corpus_id=artifact.corpus_id,
            ease_factor=2.5,
            interval_days=0,
            repetitions=0,
            due_at=_utcnow(),
            last_reviewed_at=None,
            last_grade=None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)

    updated = apply_sm2(row, grade, _utcnow())

    # Mutate the persisted row's mutable fields in place so the existing
    # row id/index stays stable.
    row.ease_factor = updated.ease_factor
    row.interval_days = updated.interval_days
    row.repetitions = updated.repetitions
    row.due_at = updated.due_at
    row.last_reviewed_at = updated.last_reviewed_at
    row.last_grade = updated.last_grade

    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Due queue
# ---------------------------------------------------------------------------


def due_cards(
    session: Session,
    corpus_id: str,
    now: datetime | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return cards whose ``due_at <= now`` for a given corpus, oldest first.

    Returns plain dicts (not SQLModel rows) so the route layer can JSON-
    serialize without an extra DTO step. Each entry carries enough fields
    to render the front/back of the card without a second round-trip:
    ``set_id``, ``card_id``, ``card`` (the dict from the artifact's
    ``content["cards"]`` matching ``card_id``), and ``due_at``.

    Cards inside an artifact whose payload no longer carries a matching
    slug (e.g., the set was regenerated and the slug changed) are
    *skipped* — there's nothing to render. The orphaned review row is
    left alone for now; a future cleanup pass can prune it if needed.
    """
    cutoff = now or _utcnow()
    rows = session.exec(
        select(FlashcardReview)
        .where(FlashcardReview.corpus_id == corpus_id)
        .where(FlashcardReview.due_at != None)  # noqa: E711 — SQLModel needs `==`/`!=`, not `is`
        .where(FlashcardReview.due_at <= cutoff)
        .order_by(FlashcardReview.due_at)
        .limit(limit)
    ).all()

    if not rows:
        return []

    # Batch-load the artifacts referenced. A user reviewing typically has
    # cards from a small handful of sets, so this is one query per unique
    # set rather than per card.
    set_ids = {r.flashcard_set_id for r in rows}
    artifacts = session.exec(
        select(Artifact).where(Artifact.id.in_(set_ids))
    ).all()
    by_id = {a.id: a for a in artifacts}

    out: list[dict[str, Any]] = []
    for row in rows:
        artifact = by_id.get(row.flashcard_set_id)
        if artifact is None:
            continue
        # Find the matching card payload.
        cards = artifact.content.get("cards") if isinstance(artifact.content, dict) else None
        if not isinstance(cards, list):
            continue
        match = None
        for card in cards:
            if isinstance(card, dict) and card.get("id") == row.card_id:
                match = card
                break
        if match is None:
            continue
        out.append(
            {
                "set_id": row.flashcard_set_id,
                "card_id": row.card_id,
                "card": match,
                "due_at": row.due_at,
            }
        )

    return out


# Suppress unused-import linting on Block / BlockType / Page — they're imported
# at module top so static analyzers don't warn when retrieval helpers reach
# for them; runtime behavior depends on the SQLModel side-effect import as
# well so removing them silently could break SQLModel registration ordering
# in future refactors.
_ = (Block, BlockType, Page)


__all__ = [
    "FlashcardGenerateRequest",
    "FlashcardGenerateResult",
    "FlashcardsError",
    "apply_sm2",
    "due_cards",
    "generate_flashcards",
    "record_review",
]
