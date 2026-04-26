"""Global search & cross-reference (spec §5.14).

Lexical BM25-ish search over books (Blocks), transcripts (TranscriptSegments),
and generated artifacts (Artifact.content JSON). Returns unified, ranked
results with structural context so the UI can show "Chapter 10 § B" /
"Class 14 transcript" / "Case brief: Shelley" labels.

Semantic (Voyage-embedded) search is a separate Phase 2+ concern — this
module is pure substring + token-overlap, Phase 5.14 line-item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlmodel import Session, select

from data.models import (
    Artifact,
    Block,
    BlockType,
    Book,
    TocEntry,
    Transcript,
    TranscriptSegment,
)

ResultKind = Literal["block", "transcript_segment", "artifact"]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class SearchRequest:
    q: str
    corpus_id: str | None = None
    kinds: list[str] | None = None  # ["block", "transcript_segment", "artifact"] subset
    limit: int = 50


@dataclass
class SearchResult:
    kind: ResultKind
    id: str
    corpus_id: str
    source_context: str
    snippet: str
    score: float
    source_location: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "be", "been", "being", "that", "this",
}


def _tokens(text: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS
    ]


def _score_match(query_tokens: list[str], text: str) -> float:
    """Simple token-overlap score. 0.0 when no query tokens in text; up to
    query_token_count when all are present (duplicate hits don't boost)."""
    if not query_tokens or not text:
        return 0.0
    text_lower = text.lower()
    hits = 0
    for qt in query_tokens:
        if qt in text_lower:
            hits += 1
    # Bonus for exact-phrase match.
    phrase = " ".join(query_tokens)
    if phrase and phrase in text_lower:
        hits += len(query_tokens) * 0.5
    return float(hits)


def _snippet(text: str, query: str, *, window: int = 120) -> str:
    """Return a snippet of ~window chars around the first query hit, with
    ellipses. Falls back to the head when the query isn't in the text."""
    if not text:
        return ""
    idx = text.lower().find(query.lower())
    if idx == -1:
        return text[: window * 2].replace("\n", " ").strip() + (
            "…" if len(text) > window * 2 else ""
        )
    start = max(0, idx - window)
    end = min(len(text), idx + len(query) + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[start:end] + suffix).replace("\n", " ").strip()


def _flatten_json(node: Any) -> str:
    """Recursively extract all string values from a JSON structure. Used to
    search the `content` JSON of Artifact rows."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return "\n".join(out)


def _book_structural_context(
    session: Session, block: Block, book_name_cache: dict[str, str]
) -> str:
    """Find the deepest TOC entry at or before block.source_page in the same
    book; return a breadcrumb-ish string. Falls back to 'Book X, p. N'."""
    if block.book_id not in book_name_cache:
        book = session.exec(select(Book).where(Book.id == block.book_id)).first()
        book_name_cache[block.book_id] = book.title if book else "Unknown book"

    toc = session.exec(
        select(TocEntry)
        .where(TocEntry.book_id == block.book_id)
        .where(TocEntry.source_page <= block.source_page)
        .order_by(TocEntry.source_page.desc())
        .limit(1)
    ).first()
    if toc is not None:
        return f"{book_name_cache[block.book_id]} — {toc.title} — p. {block.source_page}"
    return f"{book_name_cache[block.book_id]} — p. {block.source_page}"


def _artifact_structural_context(artifact: Artifact) -> str:
    """e.g., 'Case brief: Shelley v. Kraemer' / 'Rubric: Part II Q2'."""
    content = artifact.content or {}
    kind = artifact.type.value.replace("_", " ").title()
    label = (
        content.get("case_name")
        or content.get("question_label")
        or content.get("topic")
        or content.get("doctrinal_area")
        or content.get("course")
        or artifact.id[:8]
    )
    return f"{kind}: {label}"


def _transcript_structural_context(
    transcript: Transcript, segment: TranscriptSegment
) -> str:
    topic = transcript.topic or transcript.assignment_code or "Transcript"
    # minute-mark estimate: order_index is segment order, not time. Best we
    # can do here is surface speaker + order.
    return f"{topic} — turn {segment.order_index} ({segment.speaker.value})"


# ---------------------------------------------------------------------------
# Main search
# ---------------------------------------------------------------------------


def search(session: Session, req: SearchRequest) -> list[SearchResult]:
    """Run lexical search across blocks, transcript segments, and artifact
    content JSON. Kinds filter narrows the output; default is all three."""
    q = (req.q or "").strip()
    if not q:
        return []

    query_tokens = _tokens(q)
    kinds = set(req.kinds) if req.kinds else {"block", "transcript_segment", "artifact"}

    results: list[SearchResult] = []
    book_name_cache: dict[str, str] = {}

    # -- Blocks --
    if "block" in kinds:
        block_stmt = select(Block)
        if req.corpus_id is not None:
            book_ids = [
                b.id
                for b in session.exec(
                    select(Book).where(Book.corpus_id == req.corpus_id)
                ).all()
            ]
            if book_ids:
                block_stmt = block_stmt.where(Block.book_id.in_(book_ids))
            else:
                block_stmt = block_stmt.where(Block.book_id == "___never")  # empty

        for block in session.exec(block_stmt).all():
            score = _score_match(query_tokens, block.markdown)
            if score <= 0:
                continue
            # Case-opinion blocks get a small relevance boost — they're the
            # most useful search targets.
            if block.type is BlockType.CASE_OPINION:
                score += 0.5
            book_row = session.exec(
                select(Book).where(Book.id == block.book_id)
            ).first()
            corpus_id = book_row.corpus_id if book_row else ""
            results.append(
                SearchResult(
                    kind="block",
                    id=block.id,
                    corpus_id=corpus_id,
                    source_context=_book_structural_context(
                        session, block, book_name_cache
                    ),
                    snippet=_snippet(block.markdown, q),
                    score=score,
                    source_location={
                        "book_id": block.book_id,
                        "source_page": block.source_page,
                        "block_type": block.type.value,
                    },
                )
            )

    # -- Transcript segments --
    if "transcript_segment" in kinds:
        seg_stmt = select(TranscriptSegment, Transcript).join(
            Transcript, TranscriptSegment.transcript_id == Transcript.id
        )
        if req.corpus_id is not None:
            seg_stmt = seg_stmt.where(Transcript.corpus_id == req.corpus_id)

        for seg, transcript in session.exec(seg_stmt).all():
            score = _score_match(query_tokens, seg.content)
            if score <= 0:
                continue
            # Boost segments with emphasis sentiment flags — they tend to be
            # the passages the user cares most about.
            if any(
                flag in seg.sentiment_flags
                for flag in ("emphasis_verbal_cue", "returned_to_multiple_times")
            ):
                score += 0.5
            results.append(
                SearchResult(
                    kind="transcript_segment",
                    id=seg.id,
                    corpus_id=transcript.corpus_id,
                    source_context=_transcript_structural_context(transcript, seg),
                    snippet=_snippet(seg.content, q),
                    score=score,
                    source_location={
                        "transcript_id": transcript.id,
                        "order_index": seg.order_index,
                        "speaker": seg.speaker.value,
                    },
                )
            )

    # -- Artifact content --
    if "artifact" in kinds:
        art_stmt = select(Artifact)
        if req.corpus_id is not None:
            art_stmt = art_stmt.where(Artifact.corpus_id == req.corpus_id)

        for artifact in session.exec(art_stmt).all():
            flattened = _flatten_json(artifact.content)
            score = _score_match(query_tokens, flattened)
            if score <= 0:
                continue
            results.append(
                SearchResult(
                    kind="artifact",
                    id=artifact.id,
                    corpus_id=artifact.corpus_id,
                    source_context=_artifact_structural_context(artifact),
                    snippet=_snippet(flattened, q),
                    score=score,
                    source_location={
                        "artifact_type": artifact.type.value,
                    },
                )
            )

    # Rank DESC, stable tie-break by id for determinism.
    results.sort(key=lambda r: (-r.score, r.id))
    return results[: req.limit]


__all__ = ["SearchRequest", "SearchResult", "search"]
