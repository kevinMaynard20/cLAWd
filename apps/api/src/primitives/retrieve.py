"""Primitive 2: Retrieve (spec §4.2).

Given a query, return relevant structured content from the corpus — typed
Blocks with their source metadata, never a flat text blob (§4.2 verbatim).

Phase 1 scope:
- `PageRangeQuery` — the canonical case: "pages 518–559 of book X".
- `CaseReferenceQuery` — find a case's opinion block + the numbered notes that
  follow it on subsequent pages until the next case opens.
- `AssignmentCodeQuery` — stubbed here; full resolution via Syllabus (§3.6)
  lands in Phase 4 when syllabus ingestion is built.
- `SemanticQuery` — stubbed here; embedding retrieval lands in Phase 2+ when
  Voyage embeddings are populated at ingest time.

Every retrieval result carries structural context so downstream generate calls
can cite sources back to Block ids (spec §2.8, anti-hallucination).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlmodel import Session, select

from data.models import Block, BlockType, Page, Syllabus, SyllabusEntry

# ---------------------------------------------------------------------------
# Query types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRangeQuery:
    """Retrieve every Page and Block in `[start, end]` (inclusive, in source-page
    numbering — spec §2.3). When the range overshoots the book, the result is
    truncated to what actually exists and `notes` records a warning."""

    book_id: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(
                f"PageRangeQuery: start ({self.start}) > end ({self.end})"
            )


@dataclass(frozen=True)
class CaseReferenceQuery:
    """Find the case_opinion block whose `case_name` metadata matches (case-
    insensitive equality or normalized-equivalent match — see `_case_name_matches`).

    When `book_id` is None, search all books; with a book_id, constrain to that
    book — the usual case when the caller is already in a book's reading view.
    """

    case_name: str
    book_id: str | None = None


@dataclass(frozen=True)
class AssignmentCodeQuery:
    """Resolve a syllabus assignment code (e.g., "PROP-C5") to a page range via
    Syllabus (§3.6). Stubbed in Phase 1 — returns an empty result with a note."""

    corpus_id: str
    code: str


@dataclass(frozen=True)
class SemanticQuery:
    """Embedding-based retrieval. Stubbed in Phase 1."""

    corpus_id: str
    text: str
    top_k: int = 10


Query = PageRangeQuery | CaseReferenceQuery | AssignmentCodeQuery | SemanticQuery


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class RetrievalResult:
    """Structured output. The spec explicitly forbids flattening to a text blob
    at this layer — callers flatten *after* receiving this, for LLM input."""

    query_description: str  # human-readable summary for logging / UI breadcrumbs
    pages: list[Page] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # warnings, e.g., "range truncated"

    @property
    def empty(self) -> bool:
        return not self.blocks and not self.pages


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


def retrieve(
    session: Session,
    query: Query,
) -> RetrievalResult:
    """Dispatch retrieval by query type.

    The session is passed in rather than opened here so the caller controls
    transaction boundaries (e.g., a FastAPI endpoint using `Depends(get_session)`
    vs. a CLI using `session_scope`).
    """
    if isinstance(query, PageRangeQuery):
        return _retrieve_page_range(session, query)
    if isinstance(query, CaseReferenceQuery):
        return _retrieve_case_reference(session, query)
    if isinstance(query, AssignmentCodeQuery):
        return _retrieve_assignment_code(session, query)
    if isinstance(query, SemanticQuery):
        return _retrieve_semantic_stub(query)
    raise TypeError(f"Unknown query type: {type(query).__name__}")


# ---------------------------------------------------------------------------
# Page range
# ---------------------------------------------------------------------------


def _retrieve_page_range(session: Session, q: PageRangeQuery) -> RetrievalResult:
    pages = session.exec(
        select(Page)
        .where(Page.book_id == q.book_id)
        .where(Page.source_page >= q.start)
        .where(Page.source_page <= q.end)
        .order_by(Page.source_page)
    ).all()

    result = RetrievalResult(
        query_description=f"pages {q.start}–{q.end} of book {q.book_id[:8]}…",
    )

    if not pages:
        result.notes.append(
            f"No pages found in range {q.start}–{q.end} for book {q.book_id[:8]}…"
        )
        return result

    # Load blocks by (book_id, source_page) so we get them in reading order
    # regardless of which Page they hang off (they always hang off their own
    # Page, but the SQL form is page-agnostic and robust to future changes).
    blocks = session.exec(
        select(Block)
        .where(Block.book_id == q.book_id)
        .where(Block.source_page >= q.start)
        .where(Block.source_page <= q.end)
        .order_by(Block.source_page, Block.order_index)
    ).all()

    result.pages = list(pages)
    result.blocks = list(blocks)

    # If the user asked for a range wider than the book, note that.
    actual_min = pages[0].source_page
    actual_max = pages[-1].source_page
    if actual_min > q.start:
        result.notes.append(
            f"Start page {q.start} requested, first available is {actual_min}"
        )
    if actual_max < q.end:
        result.notes.append(
            f"End page {q.end} requested, last available is {actual_max}"
        )
    return result


# ---------------------------------------------------------------------------
# Case reference
# ---------------------------------------------------------------------------


def _normalize_case_name(s: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation that commonly varies
    across casebooks ('v.' vs 'v' vs 'vs.'). Used for exact-ish matching."""
    collapsed = " ".join(s.lower().split())
    for pat, repl in (
        (" v. ", " v "),
        (" vs. ", " v "),
        (" vs ", " v "),
    ):
        collapsed = collapsed.replace(pat, repl)
    return collapsed.strip().rstrip(".")


def _retrieve_case_reference(
    session: Session, q: CaseReferenceQuery
) -> RetrievalResult:
    """Match on `Block.block_metadata["case_name"]` for case_opinion blocks.

    Phase-1 simplification: once the matching opinion block is located, return
    it plus every block on the same or later pages up to (but not including)
    the next case_opinion — spec §4.2 says "numbered_note blocks … that
    reference it," but in casebook layout the notes immediately following a
    case are about that case, so the "up to next case_opinion" heuristic gets
    us the same material without solving name-resolution in the note text.
    That resolution lands properly in Phase 4 alongside the fuzzy resolver.
    """
    normalized_target = _normalize_case_name(q.case_name)

    opinion_query = select(Block).where(Block.type == BlockType.CASE_OPINION)
    if q.book_id is not None:
        opinion_query = opinion_query.where(Block.book_id == q.book_id)

    candidates = session.exec(opinion_query).all()

    match: Block | None = None
    for cand in candidates:
        stored = cand.block_metadata.get("case_name", "")
        if _normalize_case_name(str(stored)) == normalized_target:
            match = cand
            break

    result = RetrievalResult(
        query_description=f"case reference: {q.case_name!r}",
    )

    if match is None:
        result.notes.append(f"No case_opinion block with case_name matching {q.case_name!r}")
        return result

    # Find the next *case boundary* in the same book to bound the trailing
    # notes. A boundary is either a CASE_HEADER or a CASE_OPINION, whichever
    # comes first — in well-segmented casebook markdown the header precedes
    # the opinion, so using header as the cutoff avoids sweeping the next
    # case's header into this case's trailing material.
    next_boundary_q = (
        select(Block)
        .where(Block.book_id == match.book_id)
        .where(Block.type.in_((BlockType.CASE_HEADER, BlockType.CASE_OPINION)))
        .where(
            (Block.source_page > match.source_page)
            | (
                (Block.source_page == match.source_page)
                & (Block.order_index > match.order_index)
            )
        )
        .order_by(Block.source_page, Block.order_index)
        .limit(1)
    )
    next_opinion = session.exec(next_boundary_q).first()

    trailing_q = (
        select(Block)
        .where(Block.book_id == match.book_id)
        .where(
            (Block.source_page > match.source_page)
            | (
                (Block.source_page == match.source_page)
                & (Block.order_index > match.order_index)
            )
        )
        .order_by(Block.source_page, Block.order_index)
    )
    if next_opinion is not None:
        trailing_q = trailing_q.where(
            (Block.source_page < next_opinion.source_page)
            | (
                (Block.source_page == next_opinion.source_page)
                & (Block.order_index < next_opinion.order_index)
            )
        )
    trailing_blocks = session.exec(trailing_q).all()

    # Assemble in reading order: opinion first, then everything between it and
    # the next opinion.
    result.blocks = [match, *trailing_blocks]

    # Collect the unique Pages covered for source-attribution rendering.
    page_numbers = sorted({b.source_page for b in result.blocks})
    if page_numbers:
        pages = session.exec(
            select(Page)
            .where(Page.book_id == match.book_id)
            .where(Page.source_page >= page_numbers[0])
            .where(Page.source_page <= page_numbers[-1])
            .order_by(Page.source_page)
        ).all()
        result.pages = list(pages)

    # If any trailing block is NOT a numbered_note or narrative_text, flag it —
    # spec names numbered_notes specifically; this gives the UI a hint.
    foreign_types = {
        b.type
        for b in trailing_blocks
        if b.type
        not in (BlockType.NUMBERED_NOTE, BlockType.NARRATIVE_TEXT, BlockType.FOOTNOTE)
    }
    if foreign_types:
        result.notes.append(
            "Trailing blocks include types other than numbered_note/narrative_text: "
            + ", ".join(sorted(t.value for t in foreign_types))
        )
    return result


# ---------------------------------------------------------------------------
# Assignment code resolution (Phase 4.5 — needs Syllabus ingestion)
# ---------------------------------------------------------------------------


def _retrieve_assignment_code(
    session: Session, q: AssignmentCodeQuery
) -> RetrievalResult:
    """Resolve a syllabus assignment code to a page range, then delegate to
    `_retrieve_page_range` for the actual Block/Page loading.

    A code can appear in multiple Syllabus rows (e.g., mid-semester update
    creates a new syllabus). Prefer the most recent syllabus by `created_at`.
    Codes with multiple page_ranges get unioned: the retrieval result
    contains blocks from every named range.
    """
    # Pick the newest SyllabusEntry matching this code in this corpus.
    stmt = (
        select(SyllabusEntry, Syllabus)
        .join(Syllabus, SyllabusEntry.syllabus_id == Syllabus.id)
        .where(Syllabus.corpus_id == q.corpus_id)
        .where(SyllabusEntry.code == q.code)
        .order_by(Syllabus.created_at.desc())
    )
    rows = session.exec(stmt).all()

    if not rows:
        return RetrievalResult(
            query_description=f"assignment code: {q.code!r}",
            notes=[
                f"No SyllabusEntry matches code {q.code!r} in corpus {q.corpus_id[:8]}…. "
                "Ingest a syllabus first (POST /ingest/syllabus) or check the code spelling."
            ],
        )

    entry, syllabus = rows[0]

    if not entry.page_ranges:
        return RetrievalResult(
            query_description=f"assignment code: {q.code!r}",
            notes=[
                f"Assignment {q.code!r} has no page_ranges in the syllabus. "
                "The syllabus extractor may have missed them; try re-ingesting."
            ],
        )

    # Find the book for this corpus. If multiple books exist (unusual for a
    # single syllabus but possible), use the first — the SyllabusEntry doesn't
    # currently carry a book_id; page_ranges assume the corpus's primary book.
    from data.models import Book  # local import to avoid top-level cycle

    book = session.exec(
        select(Book).where(Book.corpus_id == q.corpus_id).order_by(Book.ingested_at)
    ).first()
    if book is None:
        return RetrievalResult(
            query_description=f"assignment code: {q.code!r}",
            notes=[
                f"Corpus {q.corpus_id[:8]}… has no books ingested. Ingest the "
                "casebook before resolving assignment codes."
            ],
        )

    # Union all the named page ranges.
    all_pages: list[Page] = []
    all_blocks: list[Block] = []
    notes: list[str] = []
    for pr in entry.page_ranges:
        if not isinstance(pr, list) or len(pr) != 2:
            continue
        start, end = int(pr[0]), int(pr[1])
        sub = _retrieve_page_range(
            session,
            PageRangeQuery(book_id=book.id, start=start, end=end),
        )
        all_pages.extend(sub.pages)
        all_blocks.extend(sub.blocks)
        notes.extend(sub.notes)

    # Dedupe by id while preserving order.
    seen_pages: set[str] = set()
    uniq_pages: list[Page] = []
    for p in all_pages:
        if p.id not in seen_pages:
            uniq_pages.append(p)
            seen_pages.add(p.id)
    seen_blocks: set[str] = set()
    uniq_blocks: list[Block] = []
    for b in all_blocks:
        if b.id not in seen_blocks:
            uniq_blocks.append(b)
            seen_blocks.add(b.id)

    return RetrievalResult(
        query_description=(
            f"assignment code: {q.code!r} → {entry.title!r} "
            f"({len(entry.page_ranges)} range(s); syllabus {syllabus.title!r})"
        ),
        pages=uniq_pages,
        blocks=uniq_blocks,
        notes=notes,
    )


def _retrieve_semantic_stub(q: SemanticQuery) -> RetrievalResult:
    return RetrievalResult(
        query_description=f"semantic: {q.text!r} (top {q.top_k})",
        notes=[
            "SemanticQuery is stubbed in Phase 1 — Voyage embeddings are "
            "populated at ingest in Phase 2+. Until then, use PageRangeQuery "
            "or CaseReferenceQuery for deterministic retrieval."
        ],
    )


# ---------------------------------------------------------------------------
# Retrieval scope marker (for future use; referenced in spec §4.2 signature)
# ---------------------------------------------------------------------------


RetrievalScope = Literal["auto", "book_only", "transcripts_only", "both"]
"""Reserved for Phase 4 cross-source retrieval (books + transcripts). Today
every query implicitly targets books only; keeping the name present so
callers can start threading the scope through."""


__all__ = [
    "AssignmentCodeQuery",
    "CaseReferenceQuery",
    "PageRangeQuery",
    "Query",
    "RetrievalResult",
    "RetrievalScope",
    "SemanticQuery",
    "retrieve",
]
