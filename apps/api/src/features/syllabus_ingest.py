"""Syllabus ingestion feature (spec §4.1.4, §3.6).

Takes a syllabus document (uploaded as plain text from PDF/DOCX/Google Doc
export), parses it via LLM into `SyllabusEntry` rows, and validates the
page-range references against the book so the user gets a clear error when
the syllabus references pages that don't exist in the ingested casebook.

Direct Anthropic SDK use — same pattern as `features/transcript_ingest.py`
and `features/emphasis_mapper.py` — not routed through `primitives.generate`
because the artifact envelope doesn't fit: a Syllabus is its own first-class
entity per spec §3.1, not a generated Artifact.

Activation side effect: once a Syllabus exists in the DB,
`primitives.retrieve.AssignmentCodeQuery` (stubbed in Phase 1) resolves
properly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import anthropic
import jsonschema
from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget, record_llm_call
from credentials.keyring_backend import load_credentials
from data.models import (
    Book,
    Corpus,
    Provider,
    Syllabus,
    SyllabusEntry,
)
from primitives.prompt_loader import load_output_schema, load_template
from primitives.template_renderer import render_template

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test hook — matches pattern from transcript_ingest / emphasis_mapper
# ---------------------------------------------------------------------------


_client_factory: Callable[[str], Any] | None = None


def set_anthropic_client_factory(factory: Callable[[str], Any] | None) -> None:
    """Tests inject a fake client; pass None to restore the real SDK."""
    global _client_factory
    _client_factory = factory


def _make_client(api_key: str) -> Any:
    if _client_factory is not None:
        return _client_factory(api_key)
    return anthropic.Anthropic(api_key=api_key)


# ---------------------------------------------------------------------------
# Public request / response types
# ---------------------------------------------------------------------------


@dataclass
class SyllabusIngestRequest:
    corpus_id: str
    syllabus_markdown: str
    book_id: str | None = None  # validate page_ranges against this book when provided
    professor_name: str | None = None
    semester_hint: str | None = None
    source_path: str | None = None


@dataclass
class DiscrepancyNote:
    code: str
    page_range: tuple[int, int]
    book_min: int
    book_max: int
    message: str


@dataclass
class SyllabusIngestResult:
    syllabus: Syllabus
    entries: list[SyllabusEntry]
    discrepancies: list[DiscrepancyNote]  # pages referenced that don't exist in the book
    warnings: list[str]


class SyllabusIngestError(RuntimeError):
    """Feature-level failure — missing corpus, missing book, LLM output
    doesn't match schema after retries, etc."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_page_ranges_against_book(
    session: Session,
    book_id: str,
    entries_payload: list[dict[str, Any]],
) -> list[DiscrepancyNote]:
    """For each entry's page_ranges, check that the Book actually covers those
    pages. Generate a discrepancy note per out-of-range pair."""
    book = session.exec(select(Book).where(Book.id == book_id)).first()
    if book is None:
        return []
    book_min = book.source_page_min
    book_max = book.source_page_max

    notes: list[DiscrepancyNote] = []
    for entry in entries_payload:
        code = str(entry.get("code", ""))
        for pr in entry.get("page_ranges", []):
            if not (isinstance(pr, list) and len(pr) == 2):
                continue
            try:
                start, end = int(pr[0]), int(pr[1])
            except (TypeError, ValueError):
                continue
            if start < book_min or end > book_max:
                notes.append(
                    DiscrepancyNote(
                        code=code,
                        page_range=(start, end),
                        book_min=book_min,
                        book_max=book_max,
                        message=(
                            f"Assignment {code!r} references pages {start}–{end}, "
                            f"but book covers pages {book_min}–{book_max} only. "
                            "Did you upload all batches?"
                        ),
                    )
                )
    return notes


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def ingest_syllabus(
    session: Session, req: SyllabusIngestRequest
) -> SyllabusIngestResult:
    """Parse the syllabus markdown into SyllabusEntry rows. Validates page
    ranges against `req.book_id` when provided."""
    # 1. Budget gate.
    raise_if_over_budget()

    # 2. Validate corpus + book exist.
    corpus = session.exec(select(Corpus).where(Corpus.id == req.corpus_id)).first()
    if corpus is None:
        raise SyllabusIngestError(f"Corpus {req.corpus_id!r} not found.")

    if req.book_id is not None:
        book_exists = (
            session.exec(select(Book).where(Book.id == req.book_id)).first() is not None
        )
        if not book_exists:
            raise SyllabusIngestError(
                f"Book {req.book_id!r} not found. Ingest the casebook before "
                "the syllabus so page-range validation can run."
            )

    # 3. Load credentials + template.
    creds = load_credentials()
    if creds.anthropic_api_key is None:
        raise SyllabusIngestError(
            "No Anthropic API key stored — Settings → API Key."
        )

    template = load_template("syllabus_extraction")
    schema = load_output_schema(template)

    rendered = render_template(
        template,
        {
            "syllabus_markdown": req.syllabus_markdown,
            "course": corpus.course,
            "professor_name": req.professor_name,
            "semester_hint": req.semester_hint,
        },
    )

    # 4. Call Anthropic.
    model = str(template.model_defaults.get("model", "claude-sonnet-4-6"))
    max_tokens = int(template.model_defaults.get("max_tokens", 6000))
    temperature = float(template.model_defaults.get("temperature", 0.1))

    from llm import create_message

    client = _make_client(creds.anthropic_api_key.get_secret_value())
    try:
        response = create_message(
            client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=f"Prompt template: {template.name}@{template.version}",
            messages=[{"role": "user", "content": rendered}],
        )
    except Exception as exc:  # httpx, anthropic.APIError, etc.
        detail = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        raise SyllabusIngestError(
            f"Anthropic call failed during syllabus_extraction "
            f"({type(exc).__name__}): {detail}"
        ) from exc

    # 5. Parse + validate output.
    raw = response.content[0].text
    payload = _parse_json(raw)

    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise SyllabusIngestError(
            f"syllabus_extraction output did not match schema: {exc.message}"
        ) from exc

    # 6. Cost event.
    input_tokens = getattr(response.usage, "input_tokens", 0)
    output_tokens = getattr(response.usage, "output_tokens", 0)
    record_llm_call(
        model=model,
        provider=Provider.ANTHROPIC,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        feature="syllabus_extraction",
    )

    # 7. Validate page ranges against book.
    discrepancies: list[DiscrepancyNote] = []
    if req.book_id is not None:
        discrepancies = _validate_page_ranges_against_book(
            session, req.book_id, payload["entries"]
        )

    # 8. Persist.
    syllabus = Syllabus(
        corpus_id=req.corpus_id,
        title=payload.get("title", "Syllabus"),
        source_path=req.source_path,
    )
    session.add(syllabus)
    session.commit()
    session.refresh(syllabus)

    entries: list[SyllabusEntry] = []
    for entry_data in payload["entries"]:
        assign_date: datetime | None = None
        raw_date = entry_data.get("assignment_date")
        if isinstance(raw_date, str) and raw_date:
            try:
                assign_date = datetime.fromisoformat(raw_date)
            except ValueError:
                pass  # leave None; the schema already permits either

        entry = SyllabusEntry(
            syllabus_id=syllabus.id,
            code=str(entry_data["code"]),
            assignment_date=assign_date,
            title=str(entry_data["title"]),
            page_ranges=[
                [int(pr[0]), int(pr[1])]
                for pr in entry_data.get("page_ranges", [])
                if isinstance(pr, list) and len(pr) == 2
            ],
            cases_assigned=list(entry_data.get("cases_assigned", [])),
            topic_tags=list(entry_data.get("topic_tags", [])),
        )
        session.add(entry)
        entries.append(entry)
    session.commit()
    for e in entries:
        session.refresh(e)
        session.expunge(e)
    session.refresh(syllabus)
    session.expunge(syllabus)

    warnings = [d.message for d in discrepancies]

    return SyllabusIngestResult(
        syllabus=syllabus,
        entries=entries,
        discrepancies=discrepancies,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json(raw: str) -> dict[str, Any]:
    """Tolerant parse: accept bare JSON, ```json fences, or prose-prefix JSON.
    Mirrors the generate primitive's loose parser."""
    raw = raw.strip()
    if raw.startswith("```"):
        # strip fence
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline + 1 :]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Find first '{' and last '}' as a last-ditch recovery.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise


__all__ = [
    "DiscrepancyNote",
    "SyllabusIngestError",
    "SyllabusIngestRequest",
    "SyllabusIngestResult",
    "ingest_syllabus",
    "set_anthropic_client_factory",
]
