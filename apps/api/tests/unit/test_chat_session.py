"""Unit tests for :mod:`features.chat_session`.

The chat-session helpers are pure persistence — no LLM calls — so these
tests only need a fresh DB. They cover:

- ``load_or_create_session`` creates one Artifact per call when no
  ``existing_session_id`` is provided, and returns the same row when one is.
- ``append_turn`` appends in order (history length grows by 1 per call).
- ``close_session`` sets ``ended_at`` and the second call updates it.
- Round-trip serialization: writing turns via ``append_turn`` and reading
  back via ``load_or_create_session(existing_session_id=...)`` yields an
  equivalent state.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlmodel import Session

from data import db
from data.models import ArtifactType, Corpus
from features import chat_session
from features.chat_session import (
    ChatSessionError,
    ChatSessionState,
    ChatTurn,
    append_turn,
    close_session,
    load_or_create_session,
)

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
    yield
    db.reset_engine()


def _seed_corpus(session: Session) -> str:
    corpus = Corpus(name="Property", course="Property")
    session.add(corpus)
    session.commit()
    session.refresh(corpus)
    return corpus.id


# ---------------------------------------------------------------------------
# Required tests
# ---------------------------------------------------------------------------


def test_chat_session_create_and_append(temp_env: None) -> None:
    """Create a session + 3 appends -> history length 3, in order."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id = _seed_corpus(session)

        artifact, state = load_or_create_session(
            session,
            corpus_id=corpus_id,
            case_block_id="block-id-shelley",
            mode="socratic",
        )
        assert artifact.type is ArtifactType.SOCRATIC_DRILL
        assert state.history == []

        # Three turns: prof opens, student answers, prof follows up.
        turns = [
            ChatTurn(role="professor", content="Material facts?", intent="open_facts"),
            ChatTurn(role="student", content="Restrictive covenant…"),
            ChatTurn(
                role="professor",
                content="What's the rule?",
                intent="probe_rule",
                escalation_level=2,
                mode="question",
            ),
        ]
        last_state: ChatSessionState | None = None
        for t in turns:
            last_state = append_turn(session, artifact.id, t)

        assert last_state is not None
        assert len(last_state.history) == 3
        roles = [str(t["role"]) for t in last_state.history]
        assert roles == ["professor", "student", "professor"]
        assert last_state.history[0]["intent"] == "open_facts"
        assert last_state.history[2]["escalation_level"] == 2

    # Re-load from a fresh session — proves persistence.
    with Session(engine) as session:
        _, reloaded = load_or_create_session(
            session,
            corpus_id=corpus_id,
            case_block_id="block-id-shelley",
            mode="socratic",
            existing_session_id=artifact.id,
        )
        assert len(reloaded.history) == 3
        assert reloaded.history[1]["content"] == "Restrictive covenant…"


def test_chat_session_close_sets_ended_at(temp_env: None) -> None:
    """close_session sets ended_at to a UTC datetime; second call updates it."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id = _seed_corpus(session)
        artifact, state = load_or_create_session(
            session,
            corpus_id=corpus_id,
            case_block_id="b",
            mode="cold_call",
        )
        assert state.ended_at is None

        closed = close_session(session, artifact.id)
        first_state = ChatSessionState.from_content(closed.content)
        assert first_state.ended_at is not None

    # Sleep a beat, close again — ended_at should advance.
    time.sleep(0.01)
    with Session(engine) as session:
        closed_again = close_session(session, artifact.id)
        second_state = ChatSessionState.from_content(closed_again.content)
        assert second_state.ended_at is not None
        assert second_state.ended_at >= first_state.ended_at


def test_chat_session_unknown_id_raises(temp_env: None) -> None:
    """Loading with a session_id that doesn't exist -> ChatSessionError."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id = _seed_corpus(session)
        with pytest.raises(ChatSessionError):
            load_or_create_session(
                session,
                corpus_id=corpus_id,
                case_block_id="b",
                mode="socratic",
                existing_session_id="does-not-exist-" + "0" * 30,
            )


def test_chat_session_cold_call_artifact_type(temp_env: None) -> None:
    """mode='cold_call' -> ArtifactType.COLD_CALL_SESSION."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus_id = _seed_corpus(session)
        artifact, _ = load_or_create_session(
            session,
            corpus_id=corpus_id,
            case_block_id="b",
            mode="cold_call",
        )
        assert artifact.type is ArtifactType.COLD_CALL_SESSION


def test_chat_session_history_to_prompt_dicts() -> None:
    """history_to_prompt_dicts strips professor metadata down to {role, content}."""
    state = ChatSessionState(
        case_block_id="b",
        mode="socratic",
        history=[
            {
                "role": "professor",
                "content": "Q1?",
                "intent": "open_facts",
                "escalation_level": 1,
                "mode": "question",
            },
            {"role": "student", "content": "Answer 1."},
        ],
    )
    out = chat_session.history_to_prompt_dicts(state)
    assert out == [
        {"role": "professor", "content": "Q1?"},
        {"role": "student", "content": "Answer 1."},
    ]
