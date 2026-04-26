"""Unit tests for :mod:`features.socratic_drill` (spec §5.4).

Pattern: fake Anthropic client returns valid SocraticTurn JSON. We verify
that the feature persists the session correctly, appends turns in the
right order, and routes user_answer -> student turn before the LLM call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from costs import tracker as tracker_mod
from data import db
from data.models import (
    Artifact,
    Block,
    BlockType,
    Book,
    Corpus,
    CostEvent,
    Page,
)
from features import socratic_drill
from features.chat_session import ChatSessionState
from features.socratic_drill import (
    SocraticDrillError,
    SocraticTurnRequest,
    socratic_next_turn,
)

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors the surface emphasis_mapper / generate use)
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
            usage=_FakeUsage(input_tokens=400, output_tokens=200),
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
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "creds.enc"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()

    from credentials import keyring_backend

    tracker_mod.reset_session_id()
    keyring_backend.store_anthropic_key("sk-ant-test-FAKEKEY-1234567890-LAST")

    yield
    socratic_drill.set_anthropic_client_factory(None)
    db.reset_engine()


def _seed_case(session: Session) -> tuple[str, str]:
    """Seed a Corpus + Book + Page + a CASE_OPINION block.

    Returns (corpus_id, opinion_block_id).
    """
    corpus = Corpus(name="Property", course="Property")
    session.add(corpus)
    session.commit()
    session.refresh(corpus)

    book = Book(
        id="b" * 64,
        corpus_id=corpus.id,
        title="Property Casebook",
        source_pdf_path="/p.pdf",
        source_page_min=1,
        source_page_max=20,
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
        markdown="# page 1",
        raw_text="page 1",
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
        markdown=(
            "Held: Judicial enforcement of racially restrictive covenants is state action."
        ),
        block_metadata={
            "case_name": "Shelley v. Kraemer",
            "court": "Supreme Court of the United States",
            "year": 1948,
            "citation": "334 U.S. 1",
        },
    )
    session.add(opinion)
    session.commit()
    session.refresh(opinion)
    return corpus.id, opinion.id


def _turn_payload(
    *,
    question: str = "What are the material facts of this case?",
    intent: str = "open_facts",
    mode: str = "question",
    react_to_previous: str | None = None,
    escalation_level: int = 1,
) -> str:
    body: dict[str, Any] = {
        "question": question,
        "intent": intent,
        "mode": mode,
        "escalation_level": escalation_level,
    }
    if react_to_previous is not None:
        body["react_to_previous"] = react_to_previous
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Required tests
# ---------------------------------------------------------------------------


def test_socratic_first_turn_has_no_user_answer(temp_env: None) -> None:
    """Opening call — session_id=None, user_answer=None — produces a professor
    turn with intent='open_facts' (mocked)."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    socratic_drill.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_payload(intent="open_facts"))
    )

    with Session(engine) as session:
        result = socratic_next_turn(
            session,
            SocraticTurnRequest(
                corpus_id=corpus_id,
                case_block_id=block_id,
            ),
        )

    assert result.session_id != ""
    assert result.turn_index == 1
    assert result.professor_turn["intent"] == "open_facts"
    # History has only the one professor turn.
    assert len(result.history) == 1
    assert result.history[0]["role"] == "professor"
    assert result.history[0]["intent"] == "open_facts"


def test_socratic_subsequent_turn_appends_student_answer(temp_env: None) -> None:
    """Passing user_answer='facts are X' results in history ending with
    [prof, student, prof]."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    payloads = [
        _turn_payload(intent="open_facts"),
        _turn_payload(intent="probe_holding", question="What's the holding?"),
    ]

    def payload_fn(call_n: int) -> str:
        return payloads[(call_n - 1) % len(payloads)]

    socratic_drill.set_anthropic_client_factory(_fake_factory(payload_fn))

    with Session(engine) as session:
        first = socratic_next_turn(
            session,
            SocraticTurnRequest(corpus_id=corpus_id, case_block_id=block_id),
        )
    session_id = first.session_id

    with Session(engine) as session:
        second = socratic_next_turn(
            session,
            SocraticTurnRequest(
                corpus_id=corpus_id,
                case_block_id=block_id,
                session_id=session_id,
                user_answer="facts are X — covenant barred sale.",
            ),
        )

    assert len(second.history) == 3
    roles = [str(t["role"]) for t in second.history]
    assert roles == ["professor", "student", "professor"]
    assert second.history[1]["content"] == "facts are X — covenant barred sale."
    assert second.history[2]["intent"] == "probe_holding"
    assert second.turn_index == 2


def test_socratic_pushback_on_hedge(temp_env: None) -> None:
    """Fake client returns intent='push_back_on_hedge' when user_answer
    contains 'it depends'. Verify the turn gets persisted unchanged."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    def payload_fn(call_n: int) -> str:
        # Opening turn; second call in this test simulates the LLM seeing
        # the hedged answer in history and replying with push_back.
        if call_n == 1:
            return _turn_payload(intent="open_facts")
        return _turn_payload(
            question="'It depends' is not an answer. Commit.",
            intent="push_back_on_hedge",
            mode="pushback",
            react_to_previous="You hedged — that won't fly.",
            escalation_level=3,
        )

    socratic_drill.set_anthropic_client_factory(_fake_factory(payload_fn))

    with Session(engine) as session:
        first = socratic_next_turn(
            session,
            SocraticTurnRequest(corpus_id=corpus_id, case_block_id=block_id),
        )
    session_id = first.session_id

    with Session(engine) as session:
        second = socratic_next_turn(
            session,
            SocraticTurnRequest(
                corpus_id=corpus_id,
                case_block_id=block_id,
                session_id=session_id,
                user_answer="Well, it depends on the jurisdiction…",
            ),
        )

    last = second.history[-1]
    assert last["role"] == "professor"
    assert last["intent"] == "push_back_on_hedge"
    assert last["mode"] == "pushback"
    assert last["escalation_level"] == 3
    # Reaction-to-previous is not stored on the persisted ChatTurn (only
    # ``content`` / ``intent`` / ``escalation_level`` / ``mode``), but the
    # LLM payload itself must round-trip in the result.
    assert second.professor_turn.get("react_to_previous") == "You hedged — that won't fly."


def test_socratic_missing_block_raises(temp_env: None) -> None:
    """Nonexistent case_block_id -> SocraticDrillError."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="X", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)

    socratic_drill.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_payload())
    )

    with Session(engine) as session:
        with pytest.raises(SocraticDrillError):
            socratic_next_turn(
                session,
                SocraticTurnRequest(
                    corpus_id=corpus.id,
                    case_block_id="does-not-exist-block",
                ),
            )


def test_socratic_emits_cost_event_per_turn(temp_env: None) -> None:
    """Each LLM call records a CostEvent with feature='socratic_drill'."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    socratic_drill.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_payload())
    )

    with Session(engine) as session:
        socratic_next_turn(
            session,
            SocraticTurnRequest(corpus_id=corpus_id, case_block_id=block_id),
        )

    with Session(engine) as session:
        rows = list(
            session.exec(
                select(CostEvent).where(CostEvent.feature == "socratic_drill")
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].input_tokens == 400
        assert rows[0].output_tokens == 200


def test_socratic_session_id_round_trip_state(temp_env: None) -> None:
    """The persisted Artifact's content matches ChatSessionState.from_content."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    socratic_drill.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_payload())
    )

    with Session(engine) as session:
        result = socratic_next_turn(
            session,
            SocraticTurnRequest(corpus_id=corpus_id, case_block_id=block_id),
        )

    with Session(engine) as session:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == result.session_id)
        ).first()
        assert artifact is not None
        state = ChatSessionState.from_content(artifact.content)
        assert state.mode == "socratic"
        assert state.case_block_id == block_id
        assert len(state.history) == 1
        assert artifact.prompt_template.startswith("socratic_drill@")
