"""Primitive 1: Ingest (spec §4.1).

Phase 1 scope: the source-page-marker extraction algorithm (spec §4.1.1 step 4,
"the critical bit") and its supporting helpers. Marker integration, block
segmentation, TOC extraction, and the full `ingest_book` orchestration land in
subsequent slices of Phase 1 — the algorithm here is a standalone unit the rest
of the pipeline builds on.

Why source page markers matter (spec §2.3): the user says "pages 518–559"
meaning the *printed* numbers, not PDF-page indices. Casebook PDFs reflow
across 2–3 PDF pages per printed page, with the printed number preserved
inline as a bare numeric line at the page break. If we get this wrong, every
downstream feature breaks.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageMarker:
    """A validated source-page marker, extracted from the Marker-produced markdown.

    `line_index` is the 0-based line number in the markdown at which the bare
    numeric line appears; downstream code slices the markdown at this position
    to form Page.markdown (spec §4.1.1 step 5).
    """

    line_index: int
    source_page: int


@dataclass(frozen=True)
class NumericLineCandidate:
    """A candidate bare numeric line that *might* be a source-page marker.

    Footnote numbers, chapter numbers in a short header, and leftover structural
    artifacts from Marker's extraction all show up as candidates. The algorithm
    in `extract_source_page_markers` filters them against a monotonicity +
    small-gap-tolerance constraint.
    """

    line_index: int
    value: int


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------


def find_numeric_line_candidates(markdown: str) -> list[NumericLineCandidate]:
    """Scan the markdown text for bare-numeric lines.

    A *bare numeric line* is one whose content, after stripping whitespace, is
    exactly a positive integer. Anything else (prose containing a number,
    bullet lists, headers with numbers, etc.) is rejected at this stage.

    Returns candidates in order of appearance. The ordering is preserved so
    `extract_source_page_markers` can rely on it for position-based tie-breaks.
    """
    out: list[NumericLineCandidate] = []
    for i, raw_line in enumerate(markdown.splitlines()):
        stripped = raw_line.strip()
        if stripped.isdigit():
            # `isdigit` excludes minus signs, plus signs, and decimal points,
            # which is what we want — printed page numbers are always positive
            # integers (front-matter Roman numerals are handled separately).
            out.append(NumericLineCandidate(line_index=i, value=int(stripped)))
    return out


# ---------------------------------------------------------------------------
# The main algorithm
# ---------------------------------------------------------------------------


def extract_source_page_markers(
    candidates: list[NumericLineCandidate],
    *,
    max_gap: int = 2,
    max_start_value: int = 3,
) -> list[PageMarker]:
    """Extract the sequence of source-page markers from the candidate list.

    Algorithm (spec §4.1.1):

    - Start at a candidate whose value is a small starting number (1, 2, or 3
      by default — some books open with an "Introduction" page numbered 1, 2,
      or 3).
    - Extend the sequence as long as each next candidate (in line order) has a
      value strictly greater than the previous *and* differs by at most
      `max_gap` (default 2, allowing one missing marker — e.g., a blank
      part-divider page with no printed number).
    - Footnote numbers naturally fail these constraints: they're typically
      higher than the current page cursor (so would pass the ">" check) but
      they're not monotonic across the whole document — the next real page
      marker resets the expectation, so the footnote is stranded.

    Concretely, this is a dynamic-program longest-valid-chain problem on the
    position-ordered candidate list. O(n²) in the worst case; n is typically
    a few hundred to a few thousand per book so this is fine.

    Returns an empty list if no valid chain exists (e.g., all candidates are
    footnote noise and none start in 1..max_start_value). Caller should flag
    this condition for manual review.
    """
    n = len(candidates)
    if n == 0:
        return []

    # best_len[i] = length of longest valid chain ending at candidate i.
    # prev[i]     = index of the predecessor in that chain, or -1 if i is the start.
    best_len: list[int] = [0] * n
    prev: list[int] = [-1] * n

    for i, cand_i in enumerate(candidates):
        # Can candidate i be the *start* of a chain?
        if 1 <= cand_i.value <= max_start_value:
            best_len[i] = 1
            prev[i] = -1

        # Can candidate i extend a chain that ended at some earlier j?
        for j in range(i):
            cand_j = candidates[j]
            if best_len[j] == 0:
                continue
            diff = cand_i.value - cand_j.value
            if 1 <= diff <= max_gap:
                extended = best_len[j] + 1
                if extended > best_len[i]:
                    best_len[i] = extended
                    prev[i] = j

    # Find the tail of the best chain — the candidate whose best_len is maximal
    # (ties broken by earliest position, giving a deterministic result).
    best_tail = -1
    for i in range(n):
        if best_len[i] > 0 and (best_tail == -1 or best_len[i] > best_len[best_tail]):
            best_tail = i

    if best_tail == -1:
        return []

    # Reconstruct the chain by walking the predecessor links backwards.
    result_indices: list[int] = []
    cursor = best_tail
    while cursor != -1:
        result_indices.append(cursor)
        cursor = prev[cursor]
    result_indices.reverse()

    return [
        PageMarker(
            line_index=candidates[idx].line_index,
            source_page=candidates[idx].value,
        )
        for idx in result_indices
    ]


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def extract_page_markers_from_markdown(
    markdown: str,
    *,
    max_gap: int = 2,
    max_start_value: int = 3,
) -> list[PageMarker]:
    """Scan raw markdown for bare-numeric lines and return the inferred page-marker chain.

    Convenience composition of `find_numeric_line_candidates` and
    `extract_source_page_markers` for callers that have markdown text in hand
    and don't need to inspect the intermediate candidates.
    """
    candidates = find_numeric_line_candidates(markdown)
    return extract_source_page_markers(
        candidates,
        max_gap=max_gap,
        max_start_value=max_start_value,
    )


__all__ = [
    "NumericLineCandidate",
    "PageMarker",
    "extract_page_markers_from_markdown",
    "extract_source_page_markers",
    "find_numeric_line_candidates",
    "ingest_book",
]


# ===========================================================================
# Phase 1.4: full `ingest_book` orchestration (spec §4.1.1 steps 1–8)
# ===========================================================================
#
# Everything above this line is the source-page-marker algorithm and its
# immediate helpers. Everything below composes that algorithm with the
# Marker runner, block segmenter, and TOC extractor into the full pipeline
# described in spec §4.1.1.
#
# Imports are placed here (not at module top) so the algorithm section stays
# decoupled from the ORM/ingestion-specific deps and tests that only exercise
# the algorithm don't need SQLModel in scope.


import hashlib  # noqa: E402
import re  # noqa: E402
from collections.abc import Callable  # noqa: E402
from contextlib import AbstractContextManager  # noqa: E402
from pathlib import Path  # noqa: E402

import structlog  # noqa: E402
from sqlmodel import Session  # noqa: E402

from data.db import session_scope  # noqa: E402
from data.models import (  # noqa: E402
    Block,
    Book,
    Corpus,
    IngestionMethod,
    Page,
    TocEntry,
)
from primitives import marker_runner, pymupdf4llm_runner, toc_extractor  # noqa: E402
from primitives.block_segmenter import segment_page_markdown  # noqa: E402
from primitives.marker_runner import MarkerNotInstalledError  # noqa: E402

_log = structlog.get_logger(__name__)


# The sentinel we emit between stitched batch markdowns. It's an HTML comment
# so it's invisible to rendered markdown, AND its line content is not bare-
# numeric, so `extract_page_markers_from_markdown` ignores it by design.
_BATCH_SENTINEL_TMPL = "<!-- batch:{index} path:{path} -->"
_BATCH_SENTINEL_RE = re.compile(r"^<!--\s*batch:(\d+)\s+path:(.+?)\s*-->\s*$")


# `current` is float-compatible — pymupdf4llm's per-page callback emits
# fractional batch progress (e.g., batch 0 + 350/1400 pages = 0.25). The
# task-tracker's `_on` already does `current / total`, which handles floats
# transparently.
ProgressCallback = Callable[[str, float, int], None]
SessionMaker = Callable[[], AbstractContextManager[Session]]


def _markdown_to_raw_text(md: str) -> str:
    """Phase-1 plaintext fallback: strip heading markers and bold stars.

    Good enough for search-as-you-type over `Page.raw_text`; richer text
    extraction (link text, table flattening) is a Phase 2+ concern.
    """
    out = md
    # Heading `#`s at line start.
    out = re.sub(r"^\s*#{1,6}\s*", "", out, flags=re.MULTILINE)
    # Bold/italic stars surrounding text.
    out = re.sub(r"\*{1,3}", "", out)
    # Leftover HTML comments (e.g., the batch sentinel).
    out = re.sub(r"<!--.*?-->", "", out, flags=re.DOTALL)
    return out.strip()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdf_page_span_for_offset(
    char_offset_start: int,
    char_offset_end: int,
    batch_offset_in_combined: int,
    pdf_page_offsets: list[int],
    pdf_page_count: int,
) -> tuple[int, int]:
    """Map a combined-markdown char range to a 1-based (pdf_start, pdf_end).

    `pdf_page_offsets[i]` is the char offset in the *batch* markdown where
    PDF page `i` (0-indexed) begins. We subtract `batch_offset_in_combined`
    to get the batch-local offset, then find the greatest index whose
    recorded start is <= the offset.

    1-based output matches the user-facing convention in spec §3.3.
    """
    local_start = max(0, char_offset_start - batch_offset_in_combined)
    local_end = max(local_start, char_offset_end - batch_offset_in_combined)

    def find_page(offset: int) -> int:
        best = 0
        for i, pos in enumerate(pdf_page_offsets):
            if pos <= offset:
                best = i
            else:
                break
        return best

    start_idx = find_page(local_start)
    end_idx = find_page(local_end)
    count = pdf_page_count if pdf_page_count > 0 else max(1, len(pdf_page_offsets))
    start = min(start_idx + 1, count)
    end = min(end_idx + 1, count)
    end = max(end, start)
    return start, end


def _find_batch_for_char(char_offset: int, batch_starts: list[int]) -> int:
    """Given a combined-markdown char offset, return the batch index (0-based)
    whose markdown contains that offset."""
    best = 0
    for i, start in enumerate(batch_starts):
        if start <= char_offset:
            best = i
        else:
            break
    return best


def _emit(
    on_progress: ProgressCallback | None,
    step: str,
    current: float,
    total: int,
) -> None:
    if on_progress is not None:
        try:
            on_progress(step, current, total)
        except Exception:
            # A buggy progress callback must not break ingestion.
            _log.warning("progress_callback_error", step=step, exc_info=True)


def _page_slices(
    combined_md: str, markers: list[PageMarker]
) -> list[tuple[PageMarker, int, int, str]]:
    """Slice `combined_md` at each PageMarker's line_index.

    Each slice `(marker, char_start, char_end, page_md)` covers from the line
    *after* `marker.line_index` up to (but not including) the next marker's
    line — i.e., the content that belongs to that printed page. The bare
    numeric line itself is excluded from the page body since it's a printing
    artifact, not part of the page content.

    Returns an empty list if `markers` is empty.
    """
    if not markers:
        return []

    lines = combined_md.splitlines(keepends=True)
    # Pre-compute the character offset at the start of each line for O(1)
    # line_index -> char offset lookup.
    line_char_starts: list[int] = [0]
    cumulative = 0
    for line in lines:
        cumulative += len(line)
        line_char_starts.append(cumulative)

    slices: list[tuple[PageMarker, int, int, str]] = []
    for i, marker in enumerate(markers):
        start_line = min(marker.line_index + 1, len(lines))
        if i + 1 < len(markers):
            end_line = markers[i + 1].line_index
        else:
            end_line = len(lines)
        end_line = min(max(end_line, start_line), len(lines))

        start_char = line_char_starts[start_line]
        end_char = line_char_starts[end_line]
        page_md = "".join(lines[start_line:end_line])
        slices.append((marker, start_char, end_char, page_md))

    return slices


def _resolve_or_create_corpus(
    session: Session,
    corpus_id: str | None,
    title: str,
) -> Corpus:
    """Return an existing corpus by id, or create a new one titled `title`.

    Raises `ValueError` if `corpus_id` is provided but doesn't exist.
    """
    if corpus_id is not None:
        corpus = session.get(Corpus, corpus_id)
        if corpus is None:
            raise ValueError(f"corpus_id not found: {corpus_id}")
        return corpus
    corpus = Corpus(name=title, course=title)
    session.add(corpus)
    session.commit()
    session.refresh(corpus)
    return corpus


def ingest_book(
    pdf_paths: list[Path],
    *,
    corpus_id: str | None,
    title: str,
    authors: list[str] | None = None,
    edition: str | None = None,
    use_llm: bool = True,
    session_maker: SessionMaker | None = None,
    on_progress: ProgressCallback | None = None,
) -> Book:
    """Full ingestion pipeline (spec §4.1.1 steps 1–8).

    Returns the persisted `Book` row. Idempotent by content hash: ingesting
    the same PDF set twice returns the already-persisted book without re-
    running Marker.

    Args:
      pdf_paths: batch PDFs in user-specified order. Step 3 concatenates
        their Marker outputs in this same order, so order matters.
      corpus_id: target Corpus; if None a new Corpus is created titled
        `title`.
      title: book title — required for both the book record and corpus
        creation.
      authors, edition: optional metadata.
      use_llm: forwarded to Marker's `use_llm` flag.
      session_maker: test hook; must yield a `Session` context manager.
        Defaults to `data.db.session_scope`.
      on_progress: optional callback `(step_label, current, total)`. Step
        labels: "hashing", "marker", "stitching", "page_markers", "blocks",
        "toc", "persisting". `total` may be 0 for steps without a natural
        denominator.
    """
    if not pdf_paths:
        raise ValueError("pdf_paths must be non-empty")

    authors_list = list(authors or [])

    # ---- Step 1: hash + dedupe (spec §4.1.1 step 1)
    _emit(on_progress, "hashing", 0, len(pdf_paths))
    batch_hashes: list[str] = []
    for i, p in enumerate(pdf_paths):
        if not p.exists():
            raise ValueError(f"pdf_path does not exist: {p}")
        batch_hashes.append(_sha256_file(p))
        _emit(on_progress, "hashing", i + 1, len(pdf_paths))

    # Book id = SHA-256 over the concatenated per-batch hashes in order.
    # Equivalent for dedup purposes to hashing the concatenated bytes, but
    # avoids a second pass over large PDFs.
    book_id = hashlib.sha256(
        "\n".join(batch_hashes).encode("utf-8")
    ).hexdigest()

    sm = session_maker if session_maker is not None else session_scope

    # Early-exit dedup (spec §4.1.1 step 1 verbatim).
    with sm() as session:
        existing = session.get(Book, book_id)
        if existing is not None:
            _log.info("ingest_book_dedup_hit", book_id=book_id)
            # Materialize every attr a caller might touch, then detach so
            # the returned instance survives the session close.
            _ = (
                existing.id,
                existing.corpus_id,
                existing.title,
                existing.edition,
                existing.authors,
                existing.source_pdf_path,
                existing.batch_hashes,
                existing.source_page_min,
                existing.source_page_max,
                existing.ingestion_method,
                existing.ingestion_version,
                existing.ingested_at,
            )
            session.expunge(existing)
            return existing

    # ---- Step 2: PDF -> markdown via Marker (preferred) or PyMuPDF4LLM
    # fallback (spec §4.1.1: "Marker with --use_llm, PyMuPDF4LLM fallback").
    # We probe Marker once via the first batch; if it's not importable we
    # downshift the whole job to PyMuPDF4LLM so the user gets a complete book
    # rather than a half-ingested one. The caller learns which engine ran via
    # the resulting Book.ingestion_method.
    #
    # Progress reporting: Marker has no per-page hook so we only emit between
    # batches. The fallback exposes one (PageProgressCallback), and we wrap
    # it to interpolate "marker" step progress as `batch_index + page/total`
    # so a single-batch 1400-page casebook doesn't sit at 5% for 15 minutes.
    _emit(on_progress, "marker", 0, len(pdf_paths))
    batch_results: list[marker_runner.MarkerResult] = []
    used_fallback = False

    def _make_page_cb(batch_idx: int):
        def _cb(done: int, total: int) -> None:
            inner = (done / total) if total > 0 else 0.0
            # Cast to float for _emit's int hint — the tracker handles floats.
            _emit(on_progress, "marker", batch_idx + inner, len(pdf_paths))
        return _cb

    for i, p in enumerate(pdf_paths):
        if used_fallback:
            result = pymupdf4llm_runner.run_pymupdf4llm_cached(
                p, on_page=_make_page_cb(i)
            )
        else:
            try:
                result = marker_runner.run_marker_cached(p, use_llm=use_llm)
            except MarkerNotInstalledError:
                _log.warning(
                    "marker_unavailable_falling_back_to_pymupdf4llm",
                    pdf=str(p),
                    batch_index=i,
                )
                used_fallback = True
                result = pymupdf4llm_runner.run_pymupdf4llm_cached(
                    p, on_page=_make_page_cb(i)
                )
        batch_results.append(result)
        _emit(on_progress, "marker", i + 1, len(pdf_paths))

    # ---- Step 3: stitch (spec §4.1.1 step 3)
    _emit(on_progress, "stitching", 0, len(pdf_paths))
    combined_chunks: list[str] = []
    batch_char_starts: list[int] = []
    cursor = 0
    for i, (p, result) in enumerate(zip(pdf_paths, batch_results, strict=True)):
        if i > 0:
            sentinel = (
                "\n\n" + _BATCH_SENTINEL_TMPL.format(index=i, path=p.name) + "\n\n"
            )
            combined_chunks.append(sentinel)
            cursor += len(sentinel)
        batch_char_starts.append(cursor)
        combined_chunks.append(result.markdown)
        cursor += len(result.markdown)
    combined_md = "".join(combined_chunks)
    _emit(on_progress, "stitching", len(pdf_paths), len(pdf_paths))

    # ---- Step 4: extract source page markers (spec §4.1.1 step 4)
    _emit(on_progress, "page_markers", 0, 0)
    page_markers = extract_page_markers_from_markdown(combined_md)
    _emit(on_progress, "page_markers", len(page_markers), len(page_markers))

    # ---- Step 5 & 6: slice pages, segment blocks
    _emit(on_progress, "blocks", 0, max(1, len(page_markers)))
    slices = _page_slices(combined_md, page_markers)

    pages_payload: list[dict] = []
    for idx, (marker, start_char, end_char, page_md) in enumerate(slices):
        batch_idx = _find_batch_for_char(start_char, batch_char_starts)
        batch_pdf = pdf_paths[batch_idx].name
        batch_result = batch_results[batch_idx]
        batch_start = batch_char_starts[batch_idx]

        pdf_start, pdf_end = _pdf_page_span_for_offset(
            start_char,
            end_char,
            batch_start,
            batch_result.pdf_page_offsets,
            batch_result.pdf_page_count,
        )

        # Strip any batch sentinel that happened to fall inside a slice.
        clean_md = _BATCH_SENTINEL_RE.sub("", page_md).strip("\n")

        blocks = segment_page_markdown(clean_md, marker.source_page)
        pages_payload.append(
            {
                "source_page": marker.source_page,
                "batch_pdf": batch_pdf,
                "pdf_page_start": pdf_start,
                "pdf_page_end": pdf_end,
                "markdown": clean_md,
                "raw_text": _markdown_to_raw_text(clean_md),
                "blocks": blocks,
            }
        )
        _emit(on_progress, "blocks", idx + 1, max(1, len(slices)))

    # ---- Step 7: TOC extraction (spec §4.1.1 step 7)
    _emit(on_progress, "toc", 0, 0)
    toc_drafts = toc_extractor.extract_toc(combined_md, page_markers)
    _emit(on_progress, "toc", len(toc_drafts), len(toc_drafts))

    # ---- Step 8: persist everything in one transaction (spec §4.1.1 step 8)
    _emit(on_progress, "persisting", 0, 1)
    source_pages = [p["source_page"] for p in pages_payload]
    source_page_min = min(source_pages) if source_pages else 0
    source_page_max = max(source_pages) if source_pages else 0

    with sm() as session:
        # Re-check dedup in case of a race with the optimistic early-exit.
        existing = session.get(Book, book_id)
        if existing is not None:
            _ = (
                existing.id,
                existing.corpus_id,
                existing.title,
                existing.edition,
                existing.authors,
                existing.source_pdf_path,
                existing.batch_hashes,
                existing.source_page_min,
                existing.source_page_max,
                existing.ingestion_method,
                existing.ingestion_version,
                existing.ingested_at,
            )
            session.expunge(existing)
            return existing

        corpus = _resolve_or_create_corpus(session, corpus_id, title)

        if used_fallback:
            ingestion_method = IngestionMethod.PYMUPDF4LLM
        elif use_llm:
            ingestion_method = IngestionMethod.MARKER_LLM
        else:
            ingestion_method = IngestionMethod.MARKER

        book = Book(
            id=book_id,
            corpus_id=corpus.id,
            title=title,
            edition=edition,
            authors=authors_list,
            source_pdf_path=";".join(str(p) for p in pdf_paths),
            batch_hashes=batch_hashes,
            source_page_min=source_page_min,
            source_page_max=source_page_max,
            ingestion_method=ingestion_method,
        )
        session.add(book)
        session.flush()

        for payload in pages_payload:
            page = Page(
                book_id=book.id,
                source_page=payload["source_page"],
                batch_pdf=payload["batch_pdf"],
                pdf_page_start=payload["pdf_page_start"],
                pdf_page_end=payload["pdf_page_end"],
                markdown=payload["markdown"],
                raw_text=payload["raw_text"],
            )
            session.add(page)
            session.flush()
            for block_draft in payload["blocks"]:
                session.add(
                    Block(
                        page_id=page.id,
                        book_id=book.id,
                        order_index=block_draft.order_index,
                        type=block_draft.type,
                        source_page=block_draft.source_page,
                        markdown=block_draft.markdown,
                        block_metadata=dict(block_draft.block_metadata),
                    )
                )

        # TOC rows — insert in order, resolve parent_id from in-memory list.
        toc_ids: list[str] = []
        for draft in toc_drafts:
            parent_id = (
                toc_ids[draft.parent_offset]
                if draft.parent_offset is not None
                and 0 <= draft.parent_offset < len(toc_ids)
                else None
            )
            entry = TocEntry(
                book_id=book.id,
                parent_id=parent_id,
                level=draft.level,
                title=draft.title,
                source_page=draft.source_page,
                order_index=draft.order_index,
            )
            session.add(entry)
            session.flush()
            toc_ids.append(entry.id)

        session.commit()
        session.refresh(book)
        # Eagerly materialize every attribute the caller may touch — once
        # `session_scope` exits and the session is closed the instance will
        # be detached, and SQLAlchemy would otherwise try (and fail) to
        # lazy-load expired attrs on first access.
        _ = (
            book.id,
            book.corpus_id,
            book.title,
            book.edition,
            book.authors,
            book.source_pdf_path,
            book.batch_hashes,
            book.source_page_min,
            book.source_page_max,
            book.ingestion_method,
            book.ingestion_version,
            book.ingested_at,
        )
        session.expunge(book)
        _emit(on_progress, "persisting", 1, 1)
        return book
