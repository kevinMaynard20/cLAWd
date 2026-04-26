"""Integration tests for POST /features/emphasis-map (spec §5.7).

Same fake-Anthropic-client injection pattern as the other route tests. We
seed Transcript + TranscriptSegment rows directly; the mapper doesn't depend
on the sibling ingest feature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db
from data.models import (
    Corpus,
    Speaker,
    Transcript,
    TranscriptSegment,
    TranscriptSourceType,
)
from features import emphasis_mapper

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors anthropic.Anthropic surface used in mapper)
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
    model: str = "claude-opus-4-7"
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, payload_fn) -> None:
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        payload = self._payload_fn(len(self.calls))
        return _FakeResponse(
            content=[_FakeTextContent(text=payload)],
            usage=_FakeUsage(input_tokens=1200, output_tokens=600),
        )


class _FakeClient:
    def __init__(self, payload_fn) -> None:
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
    emphasis_mapper.set_anthropic_client_factory(None)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


@pytest.fixture
def seeded_transcript(temp_env: None) -> dict[str, str]:
    """Seed Corpus + Transcript + 3 segments (2 cases mentioned)."""
    engine = db.get_engine()
    ids: dict[str, str] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        transcript = Transcript(
            id="tx" + "a" * 60,
            corpus_id=corpus.id,
            source_type=TranscriptSourceType.TEXT,
            source_path="/uploads/lec.txt",
            topic="State action doctrine",
            raw_text="Lecture body discussing Shelley and River Heights.",
            cleaned_text="Lecture body discussing Shelley and River Heights.",
        )
        session.add(transcript)
        session.commit()
        session.refresh(transcript)
        ids["transcript_id"] = transcript.id

        segments = [
            TranscriptSegment(
                transcript_id=transcript.id,
                order_index=0,
                start_char=0,
                end_char=300,
                speaker=Speaker.PROFESSOR,
                content="Shelley v. Kraemer: state enforcement of racial covenants.",
                mentioned_cases=["Shelley v. Kraemer"],
                mentioned_concepts=["state action"],
                sentiment_flags=[],
            ),
            TranscriptSegment(
                transcript_id=transcript.id,
                order_index=1,
                start_char=300,
                end_char=700,
                speaker=Speaker.PROFESSOR,
                content="Hypo on Shelley with city easement; suppose the city records...",
                mentioned_cases=["Shelley v. Kraemer"],
                sentiment_flags=["professor_hypothetical"],
            ),
            TranscriptSegment(
                transcript_id=transcript.id,
                order_index=2,
                start_char=700,
                end_char=1000,
                speaker=Speaker.PROFESSOR,
                content="River Heights — HOA dispute.",
                mentioned_cases=["River Heights"],
                sentiment_flags=[],
            ),
        ]
        for s in segments:
            session.add(s)
        session.commit()

    return ids


# ---------------------------------------------------------------------------
# Payload factory
# ---------------------------------------------------------------------------


def _emphasis_payload() -> str:
    body = {
        "items": [
            {
                "subject_kind": "case",
                "subject_label": "Shelley v. Kraemer",
                "exam_signal_score": 0.85,
                "justification": "Professor returned 2 times and ran a hypo.",
            },
            {
                "subject_kind": "case",
                "subject_label": "River Heights",
                "exam_signal_score": 0.40,
                "justification": "Single mention; no hypos.",
            },
            {
                "subject_kind": "concept",
                "subject_label": "state action",
                "exam_signal_score": 0.55,
                "justification": "Core doctrinal framing tied to Shelley.",
            },
        ],
        "summary": "Shelley dominated; state action tied to it; River Heights secondary.",
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_emphasis_map_happy_path(
    client: TestClient, seeded_transcript: dict[str, str]
) -> None:
    """200 with persisted emphasis items ranked DESC by exam_signal_score."""
    emphasis_mapper.set_anthropic_client_factory(
        _fake_factory(lambda _n: _emphasis_payload())
    )

    r = client.post(
        "/features/emphasis-map",
        json={
            "corpus_id": seeded_transcript["corpus_id"],
            "transcript_id": seeded_transcript["transcript_id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    assert body["summary"] is not None
    items = body["items"]
    assert len(items) == 3
    # DESC order.
    scores = [it["exam_signal_score"] for it in items]
    assert scores == sorted(scores, reverse=True)
    # DTO shape check.
    top = items[0]
    for key in (
        "id",
        "subject_kind",
        "subject_label",
        "minutes_on",
        "return_count",
        "hypotheticals_run",
        "disclaimed",
        "engaged_questions",
        "exam_signal_score",
        "justification",
    ):
        assert key in top


def test_route_emphasis_map_emits_cost_event(
    client: TestClient, seeded_transcript: dict[str, str]
) -> None:
    """After POST, /costs/events?feature=emphasis_analysis shows the row."""
    emphasis_mapper.set_anthropic_client_factory(
        _fake_factory(lambda _n: _emphasis_payload())
    )

    r = client.post(
        "/features/emphasis-map",
        json={
            "corpus_id": seeded_transcript["corpus_id"],
            "transcript_id": seeded_transcript["transcript_id"],
        },
    )
    assert r.status_code == 200, r.text

    r2 = client.get("/costs/events", params={"feature": "emphasis_analysis"})
    assert r2.status_code == 200
    events = r2.json()["events"]
    assert len(events) >= 1
    assert events[0]["feature"] == "emphasis_analysis"
    assert events[0]["input_tokens"] == 1200
    assert events[0]["output_tokens"] == 600


def test_route_emphasis_map_404_on_missing_transcript(
    client: TestClient, seeded_transcript: dict[str, str]
) -> None:
    """Nonexistent transcript_id -> 404."""
    emphasis_mapper.set_anthropic_client_factory(
        _fake_factory(lambda _n: _emphasis_payload())
    )
    r = client.post(
        "/features/emphasis-map",
        json={
            "corpus_id": seeded_transcript["corpus_id"],
            "transcript_id": "definitely-does-not-exist",
        },
    )
    assert r.status_code == 404


def test_route_emphasis_map_cache_hit(
    client: TestClient, seeded_transcript: dict[str, str]
) -> None:
    """Second POST without force_regenerate returns cache_hit=True."""
    emphasis_mapper.set_anthropic_client_factory(
        _fake_factory(lambda _n: _emphasis_payload())
    )

    r1 = client.post(
        "/features/emphasis-map",
        json={
            "corpus_id": seeded_transcript["corpus_id"],
            "transcript_id": seeded_transcript["transcript_id"],
        },
    )
    assert r1.status_code == 200
    assert r1.json()["cache_hit"] is False

    r2 = client.post(
        "/features/emphasis-map",
        json={
            "corpus_id": seeded_transcript["corpus_id"],
            "transcript_id": seeded_transcript["transcript_id"],
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["cache_hit"] is True
    # Items still returned on cache hit.
    assert len(body["items"]) == 3
