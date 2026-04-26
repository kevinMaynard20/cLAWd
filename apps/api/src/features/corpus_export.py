"""Corpus export → portable archive (spec §6.3).

Dumps a corpus's entire contents (books, pages, blocks, TOC entries,
artifacts, transcripts + segments, emphasis items, syllabi, professor
profiles, flashcard reviews) to a tarball of JSONL files plus a manifest.

This is a one-way export today — restore is logged as a follow-up
(SPEC_QUESTIONS Q51) since Phase 6 just needs the dump path per spec
("user can dump their entire corpus to a portable archive").

Format:
  corpus_<id>_<utc-iso>.tar.gz
    manifest.json        — schema version, exported_at, table counts
    corpus.json          — single Corpus row
    books.jsonl          — one Book per line
    pages.jsonl          — one Page per line
    blocks.jsonl         — one Block per line
    toc_entries.jsonl
    artifacts.jsonl
    transcripts.jsonl
    transcript_segments.jsonl
    emphasis_items.jsonl
    syllabi.jsonl
    syllabus_entries.jsonl
    professor_profiles.jsonl
    flashcard_reviews.jsonl
    cost_events.jsonl    — only events for artifacts in this corpus
"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from data.models import (
    Artifact,
    Block,
    Book,
    Corpus,
    CostEvent,
    EmphasisItem,
    Page,
    ProfessorProfile,
    Syllabus,
    SyllabusEntry,
    TocEntry,
    Transcript,
    TranscriptSegment,
)

EXPORT_SCHEMA_VERSION = 1


class CorpusExportError(RuntimeError):
    pass


def _to_jsonable(value: Any) -> Any:
    """Coerce SQLModel field types to JSON-serializable values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Use SQLModel's `model_dump()` then coerce types to JSON-friendly forms."""
    return _to_jsonable(row.model_dump())


def _add_jsonl(tar: tarfile.TarFile, name: str, rows: list[Any]) -> int:
    """Append a JSONL member to the tar. Returns row count for the manifest."""
    buf = io.BytesIO()
    for r in rows:
        buf.write((json.dumps(_row_to_dict(r), sort_keys=True) + "\n").encode("utf-8"))
    data = buf.getvalue()
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(datetime.now(tz=UTC).timestamp())
    tar.addfile(info, io.BytesIO(data))
    return len(rows)


def _add_json(tar: tarfile.TarFile, name: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(datetime.now(tz=UTC).timestamp())
    tar.addfile(info, io.BytesIO(data))


def export_corpus(session: Session, corpus_id: str) -> bytes:
    """Build a tarball of the corpus's full state. Returns the gzipped bytes
    so the caller can stream them out via FastAPI's `StreamingResponse` or
    persist to disk.
    """
    corpus = session.exec(select(Corpus).where(Corpus.id == corpus_id)).first()
    if corpus is None:
        raise CorpusExportError(f"Corpus {corpus_id!r} not found.")

    # Pull every related row. JOIN-less queries; corpus_id is indexed everywhere.
    books = list(session.exec(select(Book).where(Book.corpus_id == corpus_id)).all())
    book_ids = {b.id for b in books}

    pages = (
        list(session.exec(select(Page).where(Page.book_id.in_(book_ids))).all())
        if book_ids
        else []
    )
    blocks = (
        list(session.exec(select(Block).where(Block.book_id.in_(book_ids))).all())
        if book_ids
        else []
    )
    toc_entries = (
        list(
            session.exec(
                select(TocEntry).where(TocEntry.book_id.in_(book_ids))
            ).all()
        )
        if book_ids
        else []
    )

    transcripts = list(
        session.exec(select(Transcript).where(Transcript.corpus_id == corpus_id)).all()
    )
    transcript_ids = {t.id for t in transcripts}
    segments = (
        list(
            session.exec(
                select(TranscriptSegment).where(
                    TranscriptSegment.transcript_id.in_(transcript_ids)
                )
            ).all()
        )
        if transcript_ids
        else []
    )
    emphasis = (
        list(
            session.exec(
                select(EmphasisItem).where(
                    EmphasisItem.transcript_id.in_(transcript_ids)
                )
            ).all()
        )
        if transcript_ids
        else []
    )

    artifacts = list(
        session.exec(select(Artifact).where(Artifact.corpus_id == corpus_id)).all()
    )
    artifact_ids = {a.id for a in artifacts}

    syllabi = list(
        session.exec(select(Syllabus).where(Syllabus.corpus_id == corpus_id)).all()
    )
    syllabus_ids = {s.id for s in syllabi}
    syllabus_entries = (
        list(
            session.exec(
                select(SyllabusEntry).where(
                    SyllabusEntry.syllabus_id.in_(syllabus_ids)
                )
            ).all()
        )
        if syllabus_ids
        else []
    )

    professor_profiles = list(
        session.exec(
            select(ProfessorProfile).where(ProfessorProfile.corpus_id == corpus_id)
        ).all()
    )

    cost_events = (
        list(
            session.exec(
                select(CostEvent).where(CostEvent.artifact_id.in_(artifact_ids))
            ).all()
        )
        if artifact_ids
        else []
    )

    # FlashcardReview joins on flashcard_set_id (an Artifact id) — denormalized
    # corpus_id makes this direct.
    from data.models import FlashcardReview  # local: post-Phase 5.1 addition

    flashcard_reviews = list(
        session.exec(
            select(FlashcardReview).where(FlashcardReview.corpus_id == corpus_id)
        ).all()
    )

    # Build the tarball.
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        manifest = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "corpus_id": corpus.id,
            "corpus_name": corpus.name,
            "exported_at": datetime.now(tz=UTC).isoformat(),
            "table_counts": {},
        }

        _add_json(tar, "corpus.json", _row_to_dict(corpus))
        manifest["table_counts"]["corpus"] = 1

        manifest["table_counts"]["books"] = _add_jsonl(tar, "books.jsonl", books)
        manifest["table_counts"]["pages"] = _add_jsonl(tar, "pages.jsonl", pages)
        manifest["table_counts"]["blocks"] = _add_jsonl(tar, "blocks.jsonl", blocks)
        manifest["table_counts"]["toc_entries"] = _add_jsonl(
            tar, "toc_entries.jsonl", toc_entries
        )
        manifest["table_counts"]["artifacts"] = _add_jsonl(
            tar, "artifacts.jsonl", artifacts
        )
        manifest["table_counts"]["transcripts"] = _add_jsonl(
            tar, "transcripts.jsonl", transcripts
        )
        manifest["table_counts"]["transcript_segments"] = _add_jsonl(
            tar, "transcript_segments.jsonl", segments
        )
        manifest["table_counts"]["emphasis_items"] = _add_jsonl(
            tar, "emphasis_items.jsonl", emphasis
        )
        manifest["table_counts"]["syllabi"] = _add_jsonl(
            tar, "syllabi.jsonl", syllabi
        )
        manifest["table_counts"]["syllabus_entries"] = _add_jsonl(
            tar, "syllabus_entries.jsonl", syllabus_entries
        )
        manifest["table_counts"]["professor_profiles"] = _add_jsonl(
            tar, "professor_profiles.jsonl", professor_profiles
        )
        manifest["table_counts"]["flashcard_reviews"] = _add_jsonl(
            tar, "flashcard_reviews.jsonl", flashcard_reviews
        )
        manifest["table_counts"]["cost_events"] = _add_jsonl(
            tar, "cost_events.jsonl", cost_events
        )

        _add_json(tar, "manifest.json", manifest)

    return out.getvalue()


def export_filename(corpus: Corpus) -> str:
    """Standardized filename for downloads."""
    safe_name = "".join(c if c.isalnum() else "-" for c in corpus.name).strip("-")[:40]
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"corpus_{safe_name or corpus.id[:8]}_{ts}.tar.gz"


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "CorpusExportError",
    "export_corpus",
    "export_filename",
]
