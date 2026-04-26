"""Unit tests for :mod:`features.cold_call` (spec §5.6).

The cold-call surface mirrors socratic_drill, with two extras worth
specific tests:

- ``elapsed_seconds`` must be rendered into the prompt — we inspect the
  fake client's captured kwargs to confirm.
- ``cold_call_debrief`` calls the prompt with ``mode='debrief'`` and then
  closes the session (sets ``ended_at``).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from costs import tracker as tracker_mod
from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    CostEvent,
    Page,
)
from features import cold_call
from features.chat_session import ChatSessionState
from features.cold_call import (
    ColdCallError,
    cold_call_debrief,
    cold_call_next_turn,
)
from features.socratic_drill import SocraticTurnRequest

# ---------------------------------------------------------------------------
# Fake Anthropic client (same pattern as test_socratic_drill)
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
            usage=_FakeUsage(input_tokens=500, output_tokens=300),
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
# Fixture
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
    cold_call.set_anthropic_client_factory(None)
    db.reset_engine()


def _seed_case(session: Session) -> tuple[str, str]:
    corpus = Corpus(name="Property", course="Property")
    session.add(corpus)
    session.commit()
    session.refresh(corpus)

    book = Book(
        id="c" * 64,
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
        markdown="Held: youngstown holding…",
        block_metadata={
            "case_name": "Youngstown Sheet & Tube Co. v. Sawyer",
            "court": "U.S. Supreme Court",
            "year": 1952,
        },
    )
    session.add(opinion)
    session.commit()
    session.refresh(opinion)
    return corpus.id, opinion.id


def _turn_payload(
    *,
    question: str = "What's the holding?",
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


def _debrief_payload() -> str:
    """Valid SocraticTurn JSON for a debrief — uses intent='cold_debrief'."""
    return json.dumps(
        {
            "question": (
                "Debrief: turn-1 facts answer was tight; turn-3 holding hedged. "
                "Strong on procedural posture; weak on the dissent."
            ),
            "intent": "cold_debrief",
            "mode": "debrief",
            "escalation_level": 5,
            "react_to_previous": None,
        }
    )


# ---------------------------------------------------------------------------
# Required tests
# ---------------------------------------------------------------------------


def test_cold_call_has_elapsed_seconds_in_prompt(temp_env: None) -> None:
    """Inspect the fake client's captured `messages` kwarg to confirm
    elapsed_seconds was rendered. We backdate started_at to make the
    elapsed value > 0 and easy to assert on."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    factory = _fake_factory(lambda _n: _turn_payload(intent="open_facts"))
    cold_call.set_anthropic_client_factory(factory)

    # First turn — creates the session.
    with Session(engine) as session:
        first = cold_call_next_turn(
            session,
            SocraticTurnRequest(
                corpus_id=corpus_id, case_block_id=block_id
            ),
        )
    session_id = first.session_id

    # Backdate started_at by 5 minutes so elapsed_seconds is recognizable.
    with Session(engine) as session:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == session_id)
        ).first()
        assert artifact is not None
        state = ChatSessionState.from_content(artifact.content)
        state.started_at = datetime.now(tz=UTC) - timedelta(minutes=5)
        artifact.content = state.to_content()
        session.add(artifact)
        session.commit()

    # Second turn — should render elapsed_seconds ≈ 300.
    with Session(engine) as session:
        cold_call_next_turn(
            session,
            SocraticTurnRequest(
                corpus_id=corpus_id,
                case_block_id=block_id,
                session_id=session_id,
                user_answer="Sawyer was the steel-mill seizure case.",
            ),
        )

    second_call = factory.holder["client"].messages.calls[1]
    rendered_user = second_call["messages"][0]["content"]
    # The exact integer depends on test wall-clock; allow a small range.
    found_elapsed = False
    for candidate in range(295, 320):
        if f"Elapsed: {candidate}s" in rendered_user:
            found_elapsed = True
            break
    assert found_elapsed, (
        "Expected 'Elapsed: ~300s' in the rendered cold_call prompt; got: "
        + rendered_user[-400:]
    )
    assert "Mode: question" in rendered_user


def test_cold_call_debrief_sets_mode_and_closes(temp_env: None) -> None:
    """Debrief call ends the session (ended_at populated) and the LLM payload
    has mode='debrief' in the rendered prompt + the persisted turn."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    def payload_fn(call_n: int) -> str:
        # 1st call: opening question. 2nd call: debrief.
        if call_n == 1:
            return _turn_payload(intent="open_facts")
        return _debrief_payload()

    factory = _fake_factory(payload_fn)
    cold_call.set_anthropic_client_factory(factory)

    # Create the session.
    with Session(engine) as session:
        first = cold_call_next_turn(
            session,
            SocraticTurnRequest(corpus_id=corpus_id, case_block_id=block_id),
        )
    session_id = first.session_id

    # Tiny delay so close_session's ended_at is strictly after started_at.
    time.sleep(0.005)

    with Session(engine) as session:
        debrief = cold_call_debrief(session, session_id)

    # Debrief result carries mode='debrief'.
    assert debrief.professor_turn["mode"] == "debrief"
    assert debrief.professor_turn["intent"] == "cold_debrief"

    # Mode='debrief' rendered into the second prompt.
    second_call_kwargs = factory.holder["client"].messages.calls[1]
    rendered_user = second_call_kwargs["messages"][0]["content"]
    assert "Mode: debrief" in rendered_user

    # Session is closed.
    with Session(engine) as session:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == session_id)
        ).first()
        assert artifact is not None
        state = ChatSessionState.from_content(artifact.content)
        assert state.ended_at is not None
        # Last history entry is the debrief turn.
        assert state.history[-1]["mode"] == "debrief"


def test_cold_call_uses_cold_call_session_artifact_type(temp_env: None) -> None:
    """The created session is an ArtifactType.COLD_CALL_SESSION row."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    cold_call.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_payload())
    )

    with Session(engine) as session:
        result = cold_call_next_turn(
            session,
            SocraticTurnRequest(corpus_id=corpus_id, case_block_id=block_id),
        )

    with Session(engine) as session:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == result.session_id)
        ).first()
        assert artifact is not None
        assert artifact.type is ArtifactType.COLD_CALL_SESSION
        assert artifact.prompt_template.startswith("cold_call@")


def test_cold_call_emits_cost_event_per_turn(temp_env: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id, block_id = _seed_case(session)

    cold_call.set_anthropic_client_factory(
        _fake_factory(lambda _n: _turn_payload())
    )

    with Session(engine) as session:
        cold_call_next_turn(
            session,
            SocraticTurnRequest(corpus_id=corpus_id, case_block_id=block_id),
        )

    with Session(engine) as session:
        rows = list(
            session.exec(
                select(CostEvent).where(CostEvent.feature == "cold_call")
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].input_tokens == 500
        assert rows[0].output_tokens == 300


def test_cold_call_debrief_unknown_session_raises(temp_env: None) -> None:
    """Debrief on a missing session id -> ColdCallError."""
    engine = db.get_engine()
    db.init_schema()

    with Session(engine) as session:
        with pytest.raises(ColdCallError):
            cold_call_debrief(session, "no-such-session-id-" + "0" * 30)
