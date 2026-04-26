"""Corpus restore from a portable archive (spec §6.3 / Q51).

Reads a tar.gz produced by ``features.corpus_export.export_corpus`` and
re-creates every row inside a new corpus. The new corpus gets a fresh id by
default so the user can restore an archive into a clean environment without
clobbering anything; passing ``preserve_corpus_id=True`` is allowed when
the original corpus_id is known to be absent.

Schema-version gate: the manifest's ``schema_version`` must equal
``corpus_export.EXPORT_SCHEMA_VERSION`` exactly. Future migrations can layer
in cross-version translators here without breaking the lookup contract.

Restore is best-effort idempotent on archive replay: if the same archive is
restored twice into the same target corpus_id (with preserve_corpus_id=True),
content-addressed rows (Book, Transcript) round-trip fine; UUID-keyed rows
get fresh ids on the second pass, which means re-running the restore
duplicates them. Document and accept; the user-facing flow is "create empty
corpus → restore → done."
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlmodel import Session

from data.db import session_scope
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    CostEvent,
    CreatedBy,
    EmphasisItem,
    EmphasisSubjectKind,
    FlashcardReview,
    IngestionMethod,
    Page,
    ProfessorProfile,
    Provider,
    Speaker,
    Syllabus,
    SyllabusEntry,
    TocEntry,
    Transcript,
    TranscriptSegment,
    TranscriptSourceType,
)
from features.corpus_export import EXPORT_SCHEMA_VERSION

log = structlog.get_logger(__name__)


class CorpusRestoreError(RuntimeError):
    pass


@dataclass
class RestoreResult:
    new_corpus_id: str
    table_counts: dict[str, int]
    skipped: dict[str, int]   # rows that couldn't be restored (per table)


# Field types that need explicit deserialization. Other fields pass through.
_DATETIME_FIELDS = {"created_at", "updated_at", "ingested_at", "timestamp", "due_at",
                    "last_reviewed_at", "lecture_date", "assignment_date",
                    "started_at", "finished_at"}
_DECIMAL_FIELDS = {"cost_usd", "total_cost_usd", "input_cost_usd", "output_cost_usd"}
_ENUM_FIELDS = {
    "type": {"book": None, "block": BlockType, "artifact": ArtifactType},
    "speaker": Speaker,
    "subject_kind": EmphasisSubjectKind,
    "ingestion_method": IngestionMethod,
    "source_type": TranscriptSourceType,
    "provider": Provider,
    "created_by": CreatedBy,
}


def _coerce(value: Any, field: str, model_name: str) -> Any:
    """Deserialize a JSON-friendly value back into the SQLModel-expected type."""
    if value is None:
        return None
    if field in _DATETIME_FIELDS and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    if field in _DECIMAL_FIELDS and isinstance(value, (str, int, float)):
        return Decimal(str(value))
    if field == "type":
        if model_name == "block":
            return BlockType(value)
        if model_name == "artifact":
            return ArtifactType(value)
    if field == "speaker":
        return Speaker(value)
    if field == "subject_kind":
        return EmphasisSubjectKind(value)
    if field == "ingestion_method":
        return IngestionMethod(value)
    if field == "source_type":
        return TranscriptSourceType(value)
    if field == "provider":
        return Provider(value)
    if field == "created_by":
        return CreatedBy(value)
    return value


def _coerce_dict(row: dict[str, Any], model_name: str) -> dict[str, Any]:
    return {k: _coerce(v, k, model_name) for k, v in row.items()}


def _read_jsonl(tar: tarfile.TarFile, name: str) -> list[dict[str, Any]]:
    member = tar.extractfile(name)
    if member is None:
        return []
    text = member.read().decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _read_json(tar: tarfile.TarFile, name: str) -> dict[str, Any] | None:
    member = tar.extractfile(name)
    if member is None:
        return None
    return json.loads(member.read().decode("utf-8"))


def _new_id() -> str:
    return uuid.uuid4().hex


def restore_corpus(
    archive_bytes: bytes,
    *,
    new_corpus_name: str | None = None,
    preserve_corpus_id: bool = False,
) -> RestoreResult:
    """Restore a corpus archive into the live database.

    UUID-keyed rows (Page, Block, TocEntry, Artifact, EmphasisItem, FlashcardReview,
    Syllabus, SyllabusEntry, ProfessorProfile, CostEvent) get fresh ids on
    restore, with FK references rewritten via an id-remap table. Content-
    addressed rows (Book, Transcript) keep their original ids since the hash
    IS their identity.

    Returns `RestoreResult` with the new corpus_id and per-table counts.
    """
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        manifest = _read_json(tar, "manifest.json")
        if manifest is None:
            raise CorpusRestoreError("Archive missing manifest.json")
        if int(manifest.get("schema_version", -1)) != EXPORT_SCHEMA_VERSION:
            raise CorpusRestoreError(
                f"Archive schema_version {manifest.get('schema_version')!r} "
                f"!= current {EXPORT_SCHEMA_VERSION}; restore not supported."
            )

        corpus_payload = _read_json(tar, "corpus.json")
        if corpus_payload is None:
            raise CorpusRestoreError("Archive missing corpus.json")

        original_corpus_id = corpus_payload["id"]
        new_corpus_id = (
            original_corpus_id if preserve_corpus_id else _new_id()
        )

        books = _read_jsonl(tar, "books.jsonl")
        pages = _read_jsonl(tar, "pages.jsonl")
        blocks = _read_jsonl(tar, "blocks.jsonl")
        toc_entries = _read_jsonl(tar, "toc_entries.jsonl")
        artifacts = _read_jsonl(tar, "artifacts.jsonl")
        transcripts = _read_jsonl(tar, "transcripts.jsonl")
        segments = _read_jsonl(tar, "transcript_segments.jsonl")
        emphasis = _read_jsonl(tar, "emphasis_items.jsonl")
        syllabi = _read_jsonl(tar, "syllabi.jsonl")
        syllabus_entries = _read_jsonl(tar, "syllabus_entries.jsonl")
        professor_profiles = _read_jsonl(tar, "professor_profiles.jsonl")
        flashcard_reviews = _read_jsonl(tar, "flashcard_reviews.jsonl")
        cost_events = _read_jsonl(tar, "cost_events.jsonl")

    counts: dict[str, int] = {}
    skipped: dict[str, int] = {}

    # ID remap: original_id → new_id for every UUID-keyed entity. We pre-allocate
    # so that FK rewrites can lookup by old id even when the parent row hasn't
    # been inserted yet.
    page_ids = {row["id"]: _new_id() for row in pages}
    toc_ids = {row["id"]: _new_id() for row in toc_entries}
    block_ids = {row["id"]: _new_id() for row in blocks}
    artifact_ids = {row["id"]: _new_id() for row in artifacts}
    emphasis_ids = {row["id"]: _new_id() for row in emphasis}
    syllabus_ids = {row["id"]: _new_id() for row in syllabi}
    sye_ids = {row["id"]: _new_id() for row in syllabus_entries}
    profile_ids = {row["id"]: _new_id() for row in professor_profiles}
    review_ids = {row["id"]: _new_id() for row in flashcard_reviews}
    cost_ids = {row["id"]: _new_id() for row in cost_events}

    def _remap(field: str, value: Any) -> Any:
        """FK rewrites — find the new id for this old reference."""
        if value is None:
            return None
        if field == "page_id":
            return page_ids.get(value, value)
        if field == "parent_id":
            return toc_ids.get(value, value)
        if field == "parent_artifact_id":
            return artifact_ids.get(value, value)
        if field == "syllabus_id":
            return syllabus_ids.get(value, value)
        if field == "flashcard_set_id":
            return artifact_ids.get(value, value)
        if field == "artifact_id":
            return artifact_ids.get(value, value)
        if field == "transcript_id":
            return value  # content-addressed; preserved
        if field == "book_id":
            return value  # content-addressed; preserved
        return value

    with session_scope() as session:
        # 1) Corpus first — flush so books/transcripts can FK to it
        corpus_dict = _coerce_dict(corpus_payload, "corpus")
        corpus_dict["id"] = new_corpus_id
        if new_corpus_name is not None:
            corpus_dict["name"] = new_corpus_name
        session.add(Corpus(**corpus_dict))
        counts["corpus"] = 1
        session.flush()

        # 2) Books — content-addressed
        for row in books:
            d = _coerce_dict(row, "book")
            d["corpus_id"] = new_corpus_id
            session.add(Book(**d))
        counts["books"] = len(books)
        session.flush()

        # 3) Pages — UUID remapped
        for row in pages:
            d = _coerce_dict(row, "page")
            d["id"] = page_ids[row["id"]]
            session.add(Page(**d))
        counts["pages"] = len(pages)
        session.flush()

        # 4) TocEntries — UUID remapped, parent_id self-FK rewritten
        for row in toc_entries:
            d = _coerce_dict(row, "toc_entry")
            d["id"] = toc_ids[row["id"]]
            d["parent_id"] = _remap("parent_id", d.get("parent_id"))
            session.add(TocEntry(**d))
        counts["toc_entries"] = len(toc_entries)

        # 5) Blocks — UUID remapped, page_id rewritten
        for row in blocks:
            d = _coerce_dict(row, "block")
            d["id"] = block_ids[row["id"]]
            d["page_id"] = _remap("page_id", d.get("page_id"))
            session.add(Block(**d))
        counts["blocks"] = len(blocks)

        # 6) Transcripts — content-addressed
        for row in transcripts:
            d = _coerce_dict(row, "transcript")
            d["corpus_id"] = new_corpus_id
            session.add(Transcript(**d))
        counts["transcripts"] = len(transcripts)
        session.flush()

        # 7) Segments — UUID kept (no incoming FK references in current schema)
        # but we still allocate a fresh id for safety against id collisions
        for row in segments:
            d = _coerce_dict(row, "transcript_segment")
            d["id"] = _new_id()
            session.add(TranscriptSegment(**d))
        counts["transcript_segments"] = len(segments)

        # 8) EmphasisItems — UUID remapped
        for row in emphasis:
            d = _coerce_dict(row, "emphasis_item")
            d["id"] = emphasis_ids[row["id"]]
            session.add(EmphasisItem(**d))
        counts["emphasis_items"] = len(emphasis)

        # 9) Syllabi — UUID remapped
        for row in syllabi:
            d = _coerce_dict(row, "syllabus")
            d["id"] = syllabus_ids[row["id"]]
            d["corpus_id"] = new_corpus_id
            session.add(Syllabus(**d))
        counts["syllabi"] = len(syllabi)
        session.flush()

        # 10) SyllabusEntries — UUID remapped, syllabus_id rewritten
        for row in syllabus_entries:
            d = _coerce_dict(row, "syllabus_entry")
            d["id"] = sye_ids[row["id"]]
            d["syllabus_id"] = _remap("syllabus_id", d.get("syllabus_id"))
            session.add(SyllabusEntry(**d))
        counts["syllabus_entries"] = len(syllabus_entries)

        # 11) ProfessorProfiles — UUID remapped
        for row in professor_profiles:
            d = _coerce_dict(row, "professor_profile")
            d["id"] = profile_ids[row["id"]]
            d["corpus_id"] = new_corpus_id
            session.add(ProfessorProfile(**d))
        counts["professor_profiles"] = len(professor_profiles)

        # 12) Artifacts — UUID remapped, parent_artifact_id self-FK rewritten.
        # Insert in order so a child's parent has already landed (prevents
        # FK violation on self-FK against not-yet-inserted rows). The export
        # already serializes in the order rows existed in the DB, which is
        # generally creation-time, so parents come first.
        for row in artifacts:
            d = _coerce_dict(row, "artifact")
            d["id"] = artifact_ids[row["id"]]
            d["corpus_id"] = new_corpus_id
            d["parent_artifact_id"] = _remap(
                "parent_artifact_id", d.get("parent_artifact_id")
            )
            session.add(Artifact(**d))
            session.flush()  # cheap; lets later child rows resolve FK
        counts["artifacts"] = len(artifacts)

        # 13) FlashcardReviews — UUID remapped, flashcard_set_id (an artifact) rewritten
        skipped_fr = 0
        for row in flashcard_reviews:
            d = _coerce_dict(row, "flashcard_review")
            d["id"] = review_ids[row["id"]]
            d["corpus_id"] = new_corpus_id
            d["flashcard_set_id"] = _remap(
                "flashcard_set_id", d.get("flashcard_set_id")
            )
            try:
                session.add(FlashcardReview(**d))
            except Exception as exc:  # noqa: BLE001
                skipped_fr += 1
                log.warning("flashcard_review_skipped", error=str(exc))
        counts["flashcard_reviews"] = len(flashcard_reviews) - skipped_fr
        if skipped_fr:
            skipped["flashcard_reviews"] = skipped_fr

        # 14) CostEvents — UUID remapped, artifact_id rewritten
        for row in cost_events:
            d = _coerce_dict(row, "cost_event")
            d["id"] = cost_ids[row["id"]]
            d["artifact_id"] = _remap("artifact_id", d.get("artifact_id"))
            session.add(CostEvent(**d))
        counts["cost_events"] = len(cost_events)

    return RestoreResult(
        new_corpus_id=new_corpus_id,
        table_counts=counts,
        skipped=skipped,
    )


__all__ = [
    "CorpusRestoreError",
    "RestoreResult",
    "restore_corpus",
]
