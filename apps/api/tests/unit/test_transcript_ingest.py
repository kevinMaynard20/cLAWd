"""Unit tests for ``features.transcript_ingest`` (spec §4.1.2).

Same fake-Anthropic-client pattern as ``test_case_brief.py`` — injected via
``transcript_ingest.set_client_factory`` so the real Anthropic SDK is never
touched. The mock returns a JSON payload that validates against
``packages/schemas/transcript_cleanup.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from data import db
from data.models import (
    Block,
    BlockType,
    Book,
    Corpus,
    Page,
    Transcript,
    TranscriptSegment,
)
from features import transcript_ingest
from features.transcript_ingest import (
    TranscriptIngestError,
    TranscriptIngestRequest,
    ingest_transcript_audio,
    ingest_transcript_text,
)

# ---------------------------------------------------------------------------
# Fake Anthropic client surface (mirrors generate() test pattern)
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeTextContent:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list[_FakeTextContent]
    usage: _FakeUsage
    model: str = "claude-haiku-4-5"
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, payload_fn):
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._payload_fn(len(self.calls))
        return _FakeResponse(
            content=[_FakeTextContent(text=payload)],
            usage=_FakeUsage(input_tokens=500, output_tokens=300),
        )


class _FakeClient:
    def __init__(self, payload_fn):
        self.messages = _FakeMessages(payload_fn)


def _fake_factory(payload_fn):
    def _factory(_api_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh DB + keyring fallback + cleared cost cap for each test."""
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "creds.enc"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()

    from costs import tracker
    from credentials import keyring_backend

    tracker.reset_session_id()
    keyring_backend.store_anthropic_key("sk-ant-test-FAKEKEY-1234567890-LAST")

    yield
    transcript_ingest.set_client_factory(None)
    db.reset_engine()


@pytest.fixture
def seeded_corpus(temp_env: None) -> dict[str, str]:
    """One corpus + one book + one Shelley case_opinion block.

    Returns a dict with corpus_id + book_id + opinion_block_id so tests can
    seed exactly the known_case_names they need."""
    engine = db.get_engine()
    ids: dict[str, str] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        book = Book(
            id="f" * 64,
            corpus_id=corpus.id,
            title="Property",
            source_pdf_path="/p.pdf",
            source_page_min=518,
            source_page_max=520,
        )
        session.add(book)
        session.commit()

        page = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="b.pdf",
            pdf_page_start=1000,
            pdf_page_end=1002,
            markdown="# page 518",
            raw_text="page 518",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        opinion = Block(
            page_id=page.id,
            book_id=book.id,
            order_index=0,
            type=BlockType.CASE_OPINION,
            source_page=518,
            markdown="Shelley opinion markdown.",
            block_metadata={"case_name": "Shelley v. Kraemer"},
        )
        session.add(opinion)
        session.commit()
        session.refresh(opinion)
        ids["opinion_id"] = opinion.id
        ids["book_id"] = book.id

    return ids


# ---------------------------------------------------------------------------
# JSON payload factories
# ---------------------------------------------------------------------------


def _cleanup_payload(
    cleaned_text: str = "Professor speaks. Student asks. Professor answers.",
    mentioned_cases_per_segment: list[list[str]] | None = None,
) -> str:
    """A JSON string that validates against transcript_cleanup.json.

    Three-segment default — professor/student/professor turn — with content
    spans that fit within cleaned_text's length. Used by the happy-path and
    cache-hit tests; tests that need specific content override by calling
    this with different args.
    """
    if mentioned_cases_per_segment is None:
        mentioned_cases_per_segment = [[], [], []]
    assert len(mentioned_cases_per_segment) == 3
    # Fabricate spans that fit within cleaned_text.
    n = len(cleaned_text)
    third = max(1, n // 3)
    segments = [
        {
            "start_char": 0,
            "end_char": third,
            "speaker": "professor",
            "content": cleaned_text[:third],
            "mentioned_cases": mentioned_cases_per_segment[0],
            "mentioned_rules": [],
            "mentioned_concepts": [],
            "sentiment_flags": [],
        },
        {
            "start_char": third,
            "end_char": 2 * third,
            "speaker": "student",
            "content": cleaned_text[third : 2 * third],
            "mentioned_cases": mentioned_cases_per_segment[1],
            "mentioned_rules": [],
            "mentioned_concepts": [],
            "sentiment_flags": [],
        },
        {
            "start_char": 2 * third,
            "end_char": n,
            "speaker": "professor",
            "content": cleaned_text[2 * third :],
            "mentioned_cases": mentioned_cases_per_segment[2],
            "mentioned_rules": [],
            "mentioned_concepts": [],
            "sentiment_flags": [],
        },
    ]
    return json.dumps(
        {
            "cleaned_text": cleaned_text,
            "segments": segments,
            "unresolved_mentions": [],
        }
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_ingest_text_happy_path(seeded_corpus: dict[str, str]) -> None:
    """End-to-end ingest: mock returns a valid cleanup payload, feature
    persists a Transcript row + segment rows, and the result surfaces the
    mentioned_cases list."""
    corpus_id = seeded_corpus["corpus_id"]

    transcript_ingest.set_client_factory(
        _fake_factory(
            lambda _n: _cleanup_payload(
                cleaned_text="We discussed Shelley v. Kraemer today.",
                mentioned_cases_per_segment=[
                    ["Shelley v. Kraemer"], [], [],
                ],
            )
        )
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = ingest_transcript_text(
            session,
            TranscriptIngestRequest(
                corpus_id=corpus_id,
                raw_text="raw gemini text about Shelley v. Kraemer",
                topic="Takings",
            ),
        )

    assert result.cache_hit is False
    assert result.transcript.corpus_id == corpus_id
    assert result.transcript.cleaned_text.startswith("We discussed")
    assert result.segment_count == 3
    assert "Shelley v. Kraemer" in result.mentioned_cases

    # Verify persistence.
    with Session(engine) as session:
        persisted = session.exec(
            select(Transcript).where(Transcript.id == result.transcript.id)
        ).first()
        assert persisted is not None
        segments = session.exec(
            select(TranscriptSegment).where(
                TranscriptSegment.transcript_id == result.transcript.id
            )
        ).all()
        assert len(segments) == 3


def test_ingest_text_dedup_cache_hit(seeded_corpus: dict[str, str]) -> None:
    """Same raw_text ingested twice in the same corpus → second call is a
    cache hit and makes no LLM call."""
    corpus_id = seeded_corpus["corpus_id"]

    calls = {"n": 0}

    def payload_fn(_call_n: int) -> str:
        calls["n"] += 1
        return _cleanup_payload()

    transcript_ingest.set_client_factory(_fake_factory(payload_fn))

    engine = db.get_engine()
    raw = "the rough gemini text for this lecture."
    with Session(engine) as session:
        r1 = ingest_transcript_text(
            session,
            TranscriptIngestRequest(corpus_id=corpus_id, raw_text=raw),
        )
    with Session(engine) as session:
        r2 = ingest_transcript_text(
            session,
            TranscriptIngestRequest(corpus_id=corpus_id, raw_text=raw),
        )

    assert r1.cache_hit is False
    assert r2.cache_hit is True
    assert r1.transcript.id == r2.transcript.id
    assert calls["n"] == 1


def test_ingest_runs_fuzzy_resolver_on_raw_text(
    seeded_corpus: dict[str, str],
) -> None:
    """Even if the LLM didn't surface a case, the fuzzy-resolver safety net
    catches it. Seed corpus has ``Shelley v. Kraemer``; pass text containing
    the Gemini-mangled form "Shelly B Kramer"; mock LLM omits the case from
    its mentioned_cases → final result.mentioned_cases still contains the
    canonical name.
    """
    corpus_id = seeded_corpus["corpus_id"]

    # LLM response: segment content has the mangled form, but
    # mentioned_cases is empty. The fuzzy resolver should catch it.
    cleaned = "The court in Shelly B Kramer held that state action applies."
    payload = {
        "cleaned_text": cleaned,
        "segments": [
            {
                "start_char": 0,
                "end_char": len(cleaned),
                "speaker": "professor",
                "content": cleaned,
                "mentioned_cases": [],  # LLM missed it
                "mentioned_rules": [],
                "mentioned_concepts": [],
                "sentiment_flags": [],
            }
        ],
        "unresolved_mentions": [],
    }
    transcript_ingest.set_client_factory(
        _fake_factory(lambda _n: json.dumps(payload))
    )

    engine = db.get_engine()
    with Session(engine) as session:
        result = ingest_transcript_text(
            session,
            TranscriptIngestRequest(
                corpus_id=corpus_id,
                raw_text="the rough transcript text.",
            ),
        )

    assert "Shelley v. Kraemer" in result.mentioned_cases, (
        "fuzzy resolver should have caught 'Shelly B Kramer' and mapped it "
        "to the canonical 'Shelley v. Kraemer'"
    )


def test_ingest_missing_corpus_raises(temp_env: None) -> None:
    """Nonexistent corpus_id raises TranscriptIngestError before any LLM
    call happens."""
    transcript_ingest.set_client_factory(_fake_factory(lambda _n: _cleanup_payload()))

    engine = db.get_engine()
    with Session(engine) as session:
        with pytest.raises(TranscriptIngestError):
            ingest_transcript_text(
                session,
                TranscriptIngestRequest(
                    corpus_id="does-not-exist",
                    raw_text="some text",
                ),
            )


def test_ingest_audio_stub_raises_not_implemented(temp_env: None) -> None:
    """The audio path is stubbed pending faster-whisper integration —
    calling it raises NotImplementedError with a pointer to SPEC_QUESTIONS."""
    engine = db.get_engine()
    with Session(engine) as session:
        with pytest.raises(NotImplementedError) as excinfo:
            ingest_transcript_audio(session, Path("/tmp/audio.mp3"), {})
        assert "faster-whisper" in str(excinfo.value)
