"""Integration tests for /features/socratic/turn, /features/cold-call/turn,
/features/cold-call/debrief (spec §5.4, §5.6).

Same fake-Anthropic client injection pattern the other route tests use. We
seed Corpus/Book/Page/Block directly; the chat features don't depend on any
ingestion pipeline.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db
from data.models import (
    Block,
    BlockType,
    Book,
    Corpus,
    Page,
)
from features import cold_call as cold_call_mod
from features import socratic_drill as socratic_drill_mod

# ---------------------------------------------------------------------------
# Fake Anthropic client
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
            usage=_FakeUsage(input_tokens=600, output_tokens=300),
        )


class _FakeClient:
    def __init__(self, payload_fn) -> None:
        self.messages = _FakeMessages(payload_fn)


def _fake_factory(payload_fn):
    holder: dict[str, _FakeClient] = {}

    def _factory(_api_key: str) -> _FakeClient:
        if "client" not in holder:
            holder["client"] = _FakeClient(payload_fn)
        return holder["client"]

    _factory.holder = holder  # type: ignore[attr-defined]
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
    socratic_drill_mod.set_anthropic_client_factory(None)
    cold_call_mod.set_anthropic_client_factory(None)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


@pytest.fixture
def seeded_case(temp_env: None) -> dict[str, str]:
    engine = db.get_engine()
    ids: dict[str, str] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        book = Book(
            id="r" * 64,
            corpus_id=corpus.id,
            title="Property",
            source_pdf_path="/p.pdf",
            source_page_min=1,
            source_page_max=10,
        )
        session.add(book)
        session.commit()
        session.refresh(book)

        page = Page(
            book_id=book.id,
            source_page=1,
            batch_pdf="b.pdf",
            pdf_page_start=0,
            pdf_page_end=1,
            markdown="# p1",
            raw_text="p1",
        )
        session.add(page)
        session.commit()
        session.refresh(page)

        opinion = Block(
            page_id=page.id,
            book_id=book.id,
            order_index=0,
            type=BlockType.CASE_OPINION,
            source_page=1,
            markdown="Held: state action.",
            block_metadata={
                "case_name": "Shelley v. Kraemer",
                "court": "U.S. Supreme Court",
                "year": 1948,
            },
        )
        session.add(opinion)
        session.commit()
        session.refresh(opinion)
        ids["block_id"] = opinion.id

    return ids


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _turn_json(
    *,
    question: str = "What are the facts?",
    intent: str = "open_facts",
    mode: str = "question",
    escalation_level: int = 1,
) -> str:
    return json.dumps(
        {
            "question": question,
            "intent": intent,
            "mode": mode,
            "escalation_level": escalation_level,
        }
    )


def _debrief_json() -> str:
    return json.dumps(
        {
            "question": "Debrief: solid facts; weak holding analysis.",
            "intent": "cold_debrief",
            "mode": "debrief",
            "escalation_level": 5,
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_socratic_turn_happy_path(
    client: TestClient, seeded_case: dict[str, str]
) -> None:
    """POST /features/socratic/turn opens a session and returns the first turn."""
    socratic_drill_mod.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_json(intent="open_facts"))
    )

    r = client.post(
        "/features/socratic/turn",
        json={
            "corpus_id": seeded_case["corpus_id"],
            "case_block_id": seeded_case["block_id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"]
    assert body["turn_index"] == 1
    assert body["professor_turn"]["intent"] == "open_facts"
    assert len(body["history"]) == 1
    assert body["history"][0]["role"] == "professor"

    # Second turn — feed an answer in.
    socratic_drill_mod.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_json(intent="probe_holding"))
    )
    r2 = client.post(
        "/features/socratic/turn",
        json={
            "corpus_id": seeded_case["corpus_id"],
            "case_block_id": seeded_case["block_id"],
            "session_id": body["session_id"],
            "user_answer": "Restrictive covenant barred sale.",
        },
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["session_id"] == body["session_id"]
    roles = [t["role"] for t in body2["history"]]
    assert roles == ["professor", "student", "professor"]


def test_route_socratic_turn_404_on_missing_block(
    client: TestClient, seeded_case: dict[str, str]
) -> None:
    """Nonexistent case_block_id -> 404."""
    socratic_drill_mod.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_json())
    )
    r = client.post(
        "/features/socratic/turn",
        json={
            "corpus_id": seeded_case["corpus_id"],
            "case_block_id": "definitely-not-a-block",
        },
    )
    assert r.status_code == 404


def test_route_cold_call_turn_and_debrief(
    client: TestClient, seeded_case: dict[str, str]
) -> None:
    """Open a cold-call session, take a turn, then debrief — debrief response
    has mode='debrief' and the session ends."""
    payloads = [
        _turn_json(intent="open_facts"),
        _turn_json(intent="probe_holding", question="Holding?"),
        _debrief_json(),
    ]

    def payload_fn(call_n: int) -> str:
        return payloads[(call_n - 1) % len(payloads)]

    cold_call_mod.set_anthropic_client_factory(_fake_factory(payload_fn))

    # 1. Open session.
    r1 = client.post(
        "/features/cold-call/turn",
        json={
            "corpus_id": seeded_case["corpus_id"],
            "case_block_id": seeded_case["block_id"],
        },
    )
    assert r1.status_code == 200, r1.text
    session_id = r1.json()["session_id"]
    assert session_id

    # 2. Continue with a student answer.
    r2 = client.post(
        "/features/cold-call/turn",
        json={
            "corpus_id": seeded_case["corpus_id"],
            "case_block_id": seeded_case["block_id"],
            "session_id": session_id,
            "user_answer": "Quickly: the steel-mill seizure case.",
        },
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["professor_turn"]["intent"] == "probe_holding"

    # 3. Debrief.
    time.sleep(0.005)
    r3 = client.post(
        "/features/cold-call/debrief",
        json={"session_id": session_id},
    )
    assert r3.status_code == 200, r3.text
    body3 = r3.json()
    assert body3["professor_turn"]["mode"] == "debrief"
    assert body3["professor_turn"]["intent"] == "cold_debrief"
    # Last entry of history is the debrief turn.
    assert body3["history"][-1]["mode"] == "debrief"


def test_route_cold_call_debrief_404_on_missing_session(
    client: TestClient, seeded_case: dict[str, str]
) -> None:
    cold_call_mod.set_anthropic_client_factory(
        _fake_factory(lambda _n: _debrief_json())
    )
    r = client.post(
        "/features/cold-call/debrief",
        json={"session_id": "not-a-real-session-id-" + "0" * 30},
    )
    assert r.status_code == 404


def test_route_socratic_emits_cost_event(
    client: TestClient, seeded_case: dict[str, str]
) -> None:
    socratic_drill_mod.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_json())
    )
    r = client.post(
        "/features/socratic/turn",
        json={
            "corpus_id": seeded_case["corpus_id"],
            "case_block_id": seeded_case["block_id"],
        },
    )
    assert r.status_code == 200, r.text

    r2 = client.get("/costs/events", params={"feature": "socratic_drill"})
    assert r2.status_code == 200
    events = r2.json()["events"]
    assert len(events) >= 1
    assert events[0]["feature"] == "socratic_drill"
