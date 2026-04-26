"""SQLModel / pydantic definitions for the data model in spec §3.

Scope: Phase 1 entities only. Artifact (§3.11), Syllabus (§3.6), ProfessorProfile
(§3.7), Transcript/TranscriptSegment (§3.8-3.9), and EmphasisMap (§3.10) land in
later phases — stubbed here only where a ForeignKey is strictly necessary.

IDs:
- Books and Transcripts are content-addressed (SHA-256 hex of source bytes).
- Other entities use UUID v4.

JSON columns: lists and per-type metadata use SQLAlchemy JSON column type so
SQLite stores them as TEXT and pydantic roundtrips cleanly.

Note: this module intentionally does NOT use `from __future__ import annotations`.
SQLModel / SQLAlchemy need eager type evaluation at class-creation time to wire
up Relationship targets. Python 3.14's lazy-annotation behavior breaks the
mapper otherwise.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import ConfigDict, SecretStr
from sqlalchemy import JSON, Column, Index, Numeric
from sqlmodel import Field, Relationship, SQLModel


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BlockType(str, Enum):
    """Spec §3.5. Every block has one of these types. Enforced at the DB layer
    via string enum — keeps SQLite schema simple and makes raw SQL readable."""

    NARRATIVE_TEXT = "narrative_text"
    CASE_OPINION = "case_opinion"
    CASE_HEADER = "case_header"
    NUMBERED_NOTE = "numbered_note"
    PROBLEM = "problem"
    FOOTNOTE = "footnote"
    BLOCK_QUOTE = "block_quote"
    HEADER = "header"
    FIGURE = "figure"
    TABLE = "table"


class IngestionMethod(str, Enum):
    """Spec §3.2 — captured so we can re-ingest when we improve the pipeline."""

    MARKER = "marker"
    MARKER_LLM = "marker+llm"
    PYMUPDF4LLM = "pymupdf4llm"


class Provider(str, Enum):
    """Spec §3.12 — who made the LLM call."""

    ANTHROPIC = "anthropic"
    VOYAGE = "voyage"


class ArtifactType(str, Enum):
    """Spec §3.11 — every generated (or user-authored) artifact has one of these
    types. Phase 2 primarily produces `case_brief`; the other types land
    alongside their features in Phases 3–5."""

    CASE_BRIEF = "case_brief"
    FLASHCARD_SET = "flashcard_set"
    HYPO = "hypo"
    RUBRIC = "rubric"
    PRACTICE_ANSWER = "practice_answer"
    GRADE = "grade"
    SYNTHESIS = "synthesis"
    ATTACK_SHEET = "attack_sheet"
    OUTLINE = "outline"
    SOCRATIC_DRILL = "socratic_drill"
    COLD_CALL_SESSION = "cold_call_session"
    MC_QUESTION_SET = "mc_question_set"
    PAST_EXAM = "past_exam"
    GRADER_MEMO = "grader_memo"
    PROFESSOR_PROFILE = "professor_profile"   # extraction output (spec §5.13)


class CreatedBy(str, Enum):
    """Spec §3.11 `created_by` discriminator."""

    SYSTEM = "system"
    USER = "user"


# ---------------------------------------------------------------------------
# Corpus (spec §3.1)
# ---------------------------------------------------------------------------


class Corpus(SQLModel, table=True):
    """Top-level container. One corpus per course."""

    __tablename__ = "corpus"

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    name: str = Field(index=True)  # e.g., "Property – Pollack – Spring 2026"
    course: str
    professor_name: str | None = None
    school: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    books: list["Book"] = Relationship(back_populates="corpus")


# ---------------------------------------------------------------------------
# Book (spec §3.2)
# ---------------------------------------------------------------------------


class Book(SQLModel, table=True):
    """An ingested textbook.

    `id` is the SHA-256 hex of the concatenated source PDF bytes in user-specified
    batch order. Ingesting the same PDF twice is a no-op (spec §4.1.1 step 1).
    """

    __tablename__ = "book"

    id: str = Field(primary_key=True)  # content-hash; not auto-generated
    corpus_id: str = Field(foreign_key="corpus.id", index=True)

    title: str
    edition: str | None = None
    authors: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    source_pdf_path: str  # user-facing display path; storage is content-addressed
    batch_hashes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # ^ per-batch SHA-256 hexes, in order. Used to detect re-upload of a single batch.

    ingested_at: datetime = Field(default_factory=_utcnow)
    source_page_min: int
    source_page_max: int

    ingestion_method: IngestionMethod = Field(default=IngestionMethod.MARKER_LLM)
    ingestion_version: int = Field(default=1)

    corpus: Corpus | None = Relationship(back_populates="books")
    pages: list["Page"] = Relationship(
        back_populates="book",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    toc_entries: list["TocEntry"] = Relationship(
        back_populates="book",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


# ---------------------------------------------------------------------------
# Page (spec §3.3)
# ---------------------------------------------------------------------------


class Page(SQLModel, table=True):
    """A single printed page of a casebook — addressed by source_page, NEVER
    by pdf-page index (spec §2.3)."""

    __tablename__ = "page"
    __table_args__ = (
        # Source-page lookup is the hottest query path (spec §4.2 PageRange).
        Index("ix_page_book_source", "book_id", "source_page", unique=True),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    book_id: str = Field(foreign_key="book.id", index=True)

    source_page: int = Field(index=True)
    batch_pdf: str  # which batch this came from
    pdf_page_start: int  # range of pdf-page indices this source page spans
    pdf_page_end: int

    markdown: str  # clean Marker output
    raw_text: str  # plain-text fallback

    book: Book | None = Relationship(back_populates="pages")
    blocks: list["Block"] = Relationship(
        back_populates="page",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


# ---------------------------------------------------------------------------
# Block (spec §3.5)
# ---------------------------------------------------------------------------


class Block(SQLModel, table=True):
    """A typed content segment inside a Page. Case-brief generation, note parsing,
    and outline generation all consume these (see §4.1.3)."""

    __tablename__ = "block"
    __table_args__ = (
        Index("ix_block_page_order", "page_id", "order_index"),
        Index("ix_block_type", "type"),
        Index("ix_block_book_page", "book_id", "source_page"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    page_id: str = Field(foreign_key="page.id", index=True)
    book_id: str = Field(foreign_key="book.id", index=True)  # denormalized for range queries

    order_index: int  # position within the page, 0-based
    type: BlockType
    source_page: int  # denormalized from Page for range queries

    markdown: str
    # Per-type metadata (§3.5):
    #   case_opinion:  {court, year, citation, judge, case_name}
    #   numbered_note: {number, has_problem: bool}
    #   footnote:      {footnote_number, parent_block_id}
    # Kept in JSON rather than typed columns so new block types don't require migrations.
    block_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    page: Page | None = Relationship(back_populates="blocks")


# ---------------------------------------------------------------------------
# TableOfContents (spec §3.4)
# ---------------------------------------------------------------------------


class TocEntry(SQLModel, table=True):
    """Nested TOC entries — adjacency list via parent_id."""

    __tablename__ = "toc_entry"
    __table_args__ = (
        Index("ix_toc_book_order", "book_id", "order_index"),
        Index("ix_toc_parent", "parent_id"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    book_id: str = Field(foreign_key="book.id", index=True)
    parent_id: str | None = Field(default=None, foreign_key="toc_entry.id")

    level: int  # 1..6 (Part, Chapter, Section, Subsection, ...)
    title: str
    source_page: int
    order_index: int  # sibling ordering

    book: Book | None = Relationship(back_populates="toc_entries")


# ---------------------------------------------------------------------------
# Transcript source type enum (spec §3.8)
# ---------------------------------------------------------------------------


class TranscriptSourceType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"


class Speaker(str, Enum):
    PROFESSOR = "professor"
    STUDENT = "student"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Transcript (spec §3.8)
# ---------------------------------------------------------------------------


class Transcript(SQLModel, table=True):
    """An ingested lecture transcript (text or audio-derived).

    Content-addressed: `id` is the SHA-256 of the raw text bytes so re-upload
    of the same Gemini transcription is a no-op. Audio transcriptions hash
    the audio file's bytes (captured before whisper runs) so the same audio
    → the same id even if whisper is re-run.
    """

    __tablename__ = "transcript"
    __table_args__ = (
        Index("ix_transcript_corpus", "corpus_id"),
        Index("ix_transcript_assignment_code", "assignment_code"),
    )

    id: str = Field(primary_key=True)  # content-hash; not auto-generated
    corpus_id: str = Field(foreign_key="corpus.id", index=True)

    source_type: TranscriptSourceType = Field(default=TranscriptSourceType.TEXT)
    source_path: str | None = None  # original file path for provenance

    lecture_date: datetime | None = None
    topic: str | None = None  # user-provided or inferred
    assignment_code: str | None = None  # links to SyllabusEntry (§3.6)

    raw_text: str  # verbatim uploaded text (Gemini rough output)
    cleaned_text: str = ""  # post-cleanup (§4.1.2 step 2) — set after ingest

    ingested_at: datetime = Field(default_factory=_utcnow)

    segments: list["TranscriptSegment"] = Relationship(
        back_populates="transcript",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


# ---------------------------------------------------------------------------
# TranscriptSegment (spec §3.9)
# ---------------------------------------------------------------------------


class TranscriptSegment(SQLModel, table=True):
    """A speaker-turn-bounded slice of a cleaned transcript, with resolved
    mentions (cases, rules, concepts) and sentiment flags."""

    __tablename__ = "transcript_segment"
    __table_args__ = (
        Index("ix_segment_transcript_order", "transcript_id", "order_index"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    transcript_id: str = Field(foreign_key="transcript.id", index=True)

    order_index: int  # 0-based position within the transcript
    start_char: int  # offset in cleaned_text
    end_char: int

    speaker: Speaker = Field(default=Speaker.UNKNOWN)
    content: str

    # Resolved mentions — see spec §3.9 / §4.3.4 fuzzy resolver.
    mentioned_cases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    mentioned_rules: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    mentioned_concepts: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # Labels like "disclaimed_as_not_testable", "returned_to_multiple_times",
    # "professor_hypothetical", "student_question_professor_engaged".
    sentiment_flags: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    transcript: Transcript | None = Relationship(back_populates="segments")


# ---------------------------------------------------------------------------
# EmphasisItem (spec §3.10)
# ---------------------------------------------------------------------------


class EmphasisSubjectKind(str, Enum):
    CASE = "case"
    RULE = "rule"
    CONCEPT = "concept"


class EmphasisItem(SQLModel, table=True):
    """Per-subject emphasis scoring within one transcript's EmphasisMap.

    Each (transcript_id, subject_kind, subject_label) is unique — one row per
    subject the mapper identified, carrying its composite exam_signal_score
    plus the raw features that fed it so the UI can show the justification
    without re-running the analysis.
    """

    __tablename__ = "emphasis_item"
    __table_args__ = (
        Index(
            "ix_emphasis_item_unique",
            "transcript_id",
            "subject_kind",
            "subject_label",
            unique=True,
        ),
        Index("ix_emphasis_item_score", "transcript_id", "exam_signal_score"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    transcript_id: str = Field(foreign_key="transcript.id", index=True)

    subject_kind: EmphasisSubjectKind
    subject_label: str  # the resolved case name / rule name / concept phrase

    minutes_on: float = 0.0
    return_count: int = 0
    hypotheticals_run: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    disclaimed: bool = False
    engaged_questions: int = 0

    exam_signal_score: float  # 0..1 composite, computed per config/emphasis_weights.toml
    justification: str = ""  # human-readable "why we think this is important"

    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Syllabus (spec §3.6)
# ---------------------------------------------------------------------------


class Syllabus(SQLModel, table=True):
    """One syllabus per (corpus, course-version). Unique per corpus for now —
    mid-semester syllabus updates would get a new Syllabus row; older ones
    stay as history."""

    __tablename__ = "syllabus"
    __table_args__ = (Index("ix_syllabus_corpus", "corpus_id"),)

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    corpus_id: str = Field(foreign_key="corpus.id", index=True)

    title: str | None = None
    source_path: str | None = None  # original upload path
    created_at: datetime = Field(default_factory=_utcnow)

    entries: list["SyllabusEntry"] = Relationship(
        back_populates="syllabus",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class SyllabusEntry(SQLModel, table=True):
    """A single row on a syllabus: assignment code → page ranges + cases assigned."""

    __tablename__ = "syllabus_entry"
    __table_args__ = (
        Index("ix_syllabus_entry_code", "syllabus_id", "code", unique=True),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    syllabus_id: str = Field(foreign_key="syllabus.id", index=True)

    code: str  # "PROP-C5" / "Class 14"
    assignment_date: datetime | None = None
    title: str  # "Easements I"

    # list[[start, end]] — stored as JSON, validated at ingest.
    page_ranges: list[list[int]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    cases_assigned: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    topic_tags: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )

    syllabus: Syllabus | None = Relationship(back_populates="entries")


# ---------------------------------------------------------------------------
# CostEvent (spec §3.12, §7.7.4)
# ---------------------------------------------------------------------------


class CostEvent(SQLModel, table=True):
    """Every LLM call — generation, embedding, validation ping — is logged.

    Stored prices are computed at call time (not at query time) so that if
    `config/pricing.toml` is updated later, historical events retain the
    price-at-the-time.
    """

    __tablename__ = "cost_event"
    __table_args__ = (
        Index("ix_cost_session", "session_id"),
        Index("ix_cost_feature", "feature"),
        Index("ix_cost_artifact", "artifact_id"),
        Index("ix_cost_timestamp", "timestamp"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    timestamp: datetime = Field(default_factory=_utcnow)
    session_id: str = Field(index=True)  # refreshes on app launch

    model: str  # e.g., "claude-opus-4-7"
    provider: Provider

    input_tokens: int = 0
    output_tokens: int = 0

    # Money stored as fixed-precision numeric. SQLAlchemy Numeric over SQLite
    # preserves Decimal precision by storing as TEXT.
    input_cost_usd: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(precision=20, scale=10), nullable=False, default=0),
    )
    output_cost_usd: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(precision=20, scale=10), nullable=False, default=0),
    )
    total_cost_usd: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(precision=20, scale=10), nullable=False, default=0),
    )

    feature: str  # e.g., "case_brief", "irac_grade", "ingest.block_segmentation_fallback"
    artifact_id: str | None = Field(default=None)  # FK only when Artifact table exists (Phase 2)

    cached: bool = False  # true if replay-cache hit; total_cost_usd will be 0


# ---------------------------------------------------------------------------
# Credentials envelope (spec §3.13)  — NOT a table; lives in memory only.
# ---------------------------------------------------------------------------


class Credentials(SQLModel):
    """In-memory representation of stored API keys. The actual keys live in the
    OS keyring (§7.7.2); this struct is what the app's credential layer passes
    around at runtime.

    `SecretStr` masks the key in logs, error responses, and `repr()` output.
    Render as `sk-ant-…XXXX` (last 4 chars) when the UI needs a display hint.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    anthropic_api_key: SecretStr | None = None
    voyage_api_key: SecretStr | None = None
    last_validated_at: datetime | None = None
    last_validation_ok: bool | None = None

    def anthropic_display(self) -> str | None:
        """Return `sk-ant-…XXXX` for the UI, or None if unset."""
        if self.anthropic_api_key is None:
            return None
        raw = self.anthropic_api_key.get_secret_value()
        if len(raw) <= 4:
            return "…" + raw  # pathological; shouldn't happen with real keys
        return f"{raw[:7]}…{raw[-4:]}"

    def voyage_display(self) -> str | None:
        if self.voyage_api_key is None:
            return None
        raw = self.voyage_api_key.get_secret_value()
        if len(raw) <= 4:
            return "…" + raw
        return f"{raw[:4]}…{raw[-4:]}"


# ---------------------------------------------------------------------------
# ProfessorProfile (spec §3.7) — a first-class corpus entity.
#
# Kept separate from Artifact because (a) spec §3.1 lists it as its own
# Corpus-level category (alongside books/transcripts/syllabus), (b) it's
# referenced by every downstream generate call so lookup by corpus_id must be
# cheap, (c) the user edits it in the structured editor UI (§5.13) and we
# want Pydantic/SQLModel field validation per edit.
# ---------------------------------------------------------------------------


class ProfessorProfile(SQLModel, table=True):
    """The voice + grading profile of a single professor for a single course.

    Detail shape matches Appendix A. Structured fields are kept in JSON
    columns because each list item has nested typed sub-structure (`PetPeeve`
    has `name, pattern, severity, quote, source`) and normalizing into sibling
    tables is premature — the profile is edited as a whole, not row-by-row.

    Profile uniqueness: one profile per (corpus, professor_name). The same
    professor teaching a different course gets a new profile.
    """

    __tablename__ = "professor_profile"
    __table_args__ = (
        Index(
            "ix_professor_profile_corpus_name",
            "corpus_id",
            "professor_name",
            unique=True,
        ),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    corpus_id: str = Field(foreign_key="corpus.id", index=True)

    professor_name: str = Field(index=True)
    course: str
    school: str | None = None

    # Exam-format dict: {duration_hours, word_limit, open_book, structure: [...], prompt_conventions: [...]}
    exam_format: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    # Each list item is a typed dict per Appendix A. Kept in JSON for simplicity.
    pet_peeves: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    favored_framings: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    stable_traps: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    voice_conventions: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    commonly_tested: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )

    # Paths to the memo / syllabus files this profile was extracted from — so
    # re-extraction when new artifacts arrive knows what it already processed.
    source_artifact_paths: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Artifact (spec §3.11) — the envelope every generated output lives inside.
# ---------------------------------------------------------------------------


class Artifact(SQLModel, table=True):
    """Every generated case brief, flashcard set, rubric, grade, etc. is an
    Artifact. The schema is type-agnostic — per-type payload lives in the
    `content` JSON column and is validated against a type-specific JSON schema
    at generation time (`primitives.generate`).

    Anti-hallucination (spec §2.8): `sources` lists the Block ids / Page ids /
    Transcript segment ids this artifact's claims trace to. The verify primitive
    (§4.4) cross-checks that every citation in `content` appears in `sources`.
    """

    __tablename__ = "artifact"
    __table_args__ = (
        Index("ix_artifact_corpus_type", "corpus_id", "type"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    corpus_id: str = Field(foreign_key="corpus.id", index=True)

    type: ArtifactType
    created_at: datetime = Field(default_factory=_utcnow)
    created_by: CreatedBy = Field(default=CreatedBy.SYSTEM)

    # Spec §3.11: list[Source] — a heterogeneous list. We store as JSON with
    # the shape `[{"kind": "block"|"page"|"transcript_segment", "id": "..."}]`.
    sources: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    # Per-type payload. case_brief follows schemas/case_brief.json; other types
    # follow their own schemas.
    content: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    parent_artifact_id: str | None = Field(
        default=None, foreign_key="artifact.id", index=True
    )

    # Reproducibility (spec §3.11): which template+version + which model produced this.
    prompt_template: str = Field(default="")  # e.g., "case_brief@1.2.0"
    llm_model: str = Field(default="")  # e.g., "claude-opus-4-7"

    # Sum of all CostEvents tied to this artifact (spec §3.11).
    cost_usd: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(precision=20, scale=10), nullable=False, default=0),
    )

    # Spec §4.3 cache key — hash of (template name, template version, inputs,
    # retrieval content, model). Generate checks for an existing Artifact with
    # the same key before making a new LLM call.
    cache_key: str = Field(default="", index=True)

    regenerable: bool = Field(default=True)


# ---------------------------------------------------------------------------
# FlashcardReview (spec §5.3) — per-card SM-2 spaced-repetition state.
#
# Kept separate from Artifact because a FlashcardSet artifact is the immutable
# generation envelope (cards' fronts/backs are content-addressed by the prompt
# cache_key), whereas the SM-2 state mutates after every review. The two are
# joined on (flashcard_set_id, card_id): the artifact's content carries the
# card payload; the FlashcardReview row carries the schedule.
# ---------------------------------------------------------------------------


class FlashcardReview(SQLModel, table=True):
    """One row per (flashcard_set, card) pair carrying the SM-2 schedule.

    Uniqueness is enforced on ``(flashcard_set_id, card_id)`` so regenerating a
    set with the same slug ids preserves the user's review history; renaming a
    card produces a new row (and the old row is orphaned, which is the expected
    behavior — historical state shouldn't migrate to a "different" card).
    """

    __tablename__ = "flashcard_review"
    __table_args__ = (
        Index(
            "ix_flashcard_review_set_card",
            "flashcard_set_id",
            "card_id",
            unique=True,
        ),
        Index("ix_flashcard_review_due", "due_at"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    flashcard_set_id: str = Field(foreign_key="artifact.id", index=True)
    card_id: str  # slug id from the card inside the set
    corpus_id: str = Field(index=True)  # denormalized for fast filter

    # SM-2 state (https://en.wikipedia.org/wiki/SuperMemo#Description_of_SM-2_algorithm).
    ease_factor: float = 2.5  # SM-2 default
    interval_days: int = 0
    repetitions: int = 0
    due_at: datetime | None = None
    last_reviewed_at: datetime | None = None
    last_grade: int | None = None  # 0..5 per SM-2


# ---------------------------------------------------------------------------
# BackgroundTask — async work tracking (book ingestion, transcript cleanup,
# anything that runs longer than an HTTP request can wait for).
# ---------------------------------------------------------------------------


class TaskKind(str, Enum):
    BOOK_INGESTION = "book_ingestion"
    TRANSCRIPT_INGESTION = "transcript_ingestion"
    CORPUS_RESTORE = "corpus_restore"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackgroundTask(SQLModel, table=True):
    """A long-running operation tracked across HTTP polls.

    The UI POSTs to start a task, then polls `GET /tasks/{id}` for status +
    progress. `progress_step` is the human-readable step label; `progress_pct`
    is 0..1 cumulative.
    """

    __tablename__ = "background_task"
    __table_args__ = (
        Index("ix_task_corpus_status", "corpus_id", "status"),
        Index("ix_task_kind_status", "kind", "status"),
    )

    id: str = Field(default_factory=_uuid_hex, primary_key=True)
    corpus_id: str | None = Field(default=None, foreign_key="corpus.id", index=True)

    kind: TaskKind
    status: TaskStatus = Field(default=TaskStatus.PENDING)

    progress_step: str = ""           # e.g., "marker", "block_segmentation"
    progress_pct: float = 0.0         # 0..1 cumulative

    inputs_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------


__all__ = [
    "Artifact",
    "ArtifactType",
    "BackgroundTask",
    "Block",
    "BlockType",
    "Book",
    "Corpus",
    "CostEvent",
    "CreatedBy",
    "Credentials",
    "EmphasisItem",
    "EmphasisSubjectKind",
    "FlashcardReview",
    "IngestionMethod",
    "Page",
    "ProfessorProfile",
    "Provider",
    "Speaker",
    "Syllabus",
    "SyllabusEntry",
    "TaskKind",
    "TaskStatus",
    "TocEntry",
    "Transcript",
    "TranscriptSegment",
    "TranscriptSourceType",
]
