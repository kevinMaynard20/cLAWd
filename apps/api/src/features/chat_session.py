"""Shared chat-session machinery for Socratic drill (spec §5.4) and cold call
(spec §5.6).

Both features are stateful — the server persists the full turn history, and
each turn is a separate LLM call. The full turn list lives under
:attr:`Artifact.content` of a :class:`ArtifactType.SOCRATIC_DRILL` or
:class:`ArtifactType.COLD_CALL_SESSION` row, and we rotate that envelope
in-place as turns accumulate.

Why the Artifact rather than a dedicated table:
    * Spec §3.11 already names these two ArtifactTypes — adding sibling tables
      would duplicate the ``content`` JSON column with no extra structure
      the UI / verifier needs.
    * The dict shape is read only by us, so we skip JSON-Schema validation
      (cheap; the parent feature owns the contract).
    * Persistence + cost-event linkage already work with Artifacts.

The session state is intentionally schema-lite (a typed dict-of-dicts):
each turn is just a serialized :class:`ChatTurn`. We keep the metadata
fields (``mode``, ``case_block_id``, ``started_at``, ``ended_at``) at the
top level so the UI / future analytics layer can index without parsing the
turn list.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlmodel import Session, select

from data.models import Artifact, ArtifactType, CreatedBy

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


ChatRole = Literal["professor", "student"]
ChatMode = Literal["socratic", "cold_call"]


@dataclass
class ChatTurn:
    """One turn in a chat session.

    On a *professor* turn, ``intent`` / ``escalation_level`` / ``mode`` are
    populated from the LLM's :file:`schemas/socratic_turn.json` output. On a
    *student* turn, those fields are ``None`` (the student doesn't have an
    intent) and ``content`` is the literal answer the user typed.

    ``timestamp_offset_s`` is the number of seconds since
    :attr:`ChatSessionState.started_at`. The cold-call feature uses it to
    feed ``elapsed_seconds`` into the prompt for time-pressure cues.
    """

    role: ChatRole
    content: str
    intent: str | None = None
    escalation_level: int | None = None
    mode: str | None = None
    timestamp_offset_s: float = 0.0


@dataclass
class ChatSessionState:
    """Persisted under :attr:`Artifact.content`.

    Schema-lite dict — no JSON-Schema validation since only this module reads
    it. We round-trip through :func:`asdict` / :func:`from_dict` so callers
    can mutate the dict directly when needed.
    """

    case_block_id: str
    history: list[dict[str, Any]] = field(default_factory=list)
    mode: ChatMode = "socratic"
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    ended_at: datetime | None = None

    def to_content(self) -> dict[str, Any]:
        """Serialize for storage in :attr:`Artifact.content`.

        Datetimes go to ISO-8601 strings (SQLite JSON column doesn't support
        native datetimes, and ISO is the project convention; see
        :class:`Artifact` route DTOs).
        """
        return {
            "case_block_id": self.case_block_id,
            "history": list(self.history),
            "mode": self.mode,
            "started_at": _to_iso(self.started_at),
            "ended_at": _to_iso(self.ended_at) if self.ended_at else None,
        }

    @classmethod
    def from_content(cls, content: dict[str, Any]) -> ChatSessionState:
        """Inverse of :meth:`to_content`. Tolerant to missing keys (treated
        as empty/None) so older artifacts still load."""
        started_at = content.get("started_at")
        ended_at = content.get("ended_at")
        return cls(
            case_block_id=str(content.get("case_block_id", "")),
            history=list(content.get("history") or []),
            mode=content.get("mode", "socratic"),  # type: ignore[arg-type]
            started_at=(
                _from_iso(started_at)
                if isinstance(started_at, str)
                else datetime.now(tz=UTC)
            ),
            ended_at=_from_iso(ended_at) if isinstance(ended_at, str) else None,
        )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def load_or_create_session(
    session: Session,
    *,
    corpus_id: str,
    case_block_id: str,
    mode: ChatMode,
    existing_session_id: str | None = None,
) -> tuple[Artifact, ChatSessionState]:
    """Return ``(artifact_row, parsed_state)``.

    When ``existing_session_id`` is None, create a new
    :class:`ArtifactType.SOCRATIC_DRILL` (or
    :class:`ArtifactType.COLD_CALL_SESSION`) artifact with an empty history
    and ``started_at = now``. When provided, load that artifact and parse
    its ``content`` into a :class:`ChatSessionState`.

    The returned artifact is *attached* to the passed-in session so the
    caller can mutate ``content`` and commit. Callers that need a detached
    copy (e.g., for cross-session reads in a route) should ``expunge`` after
    the final commit.
    """
    if existing_session_id is not None:
        existing = session.exec(
            select(Artifact).where(Artifact.id == existing_session_id)
        ).first()
        if existing is None:
            raise ChatSessionError(
                f"Chat session {existing_session_id!r} not found."
            )
        if existing.type not in (
            ArtifactType.SOCRATIC_DRILL,
            ArtifactType.COLD_CALL_SESSION,
        ):
            raise ChatSessionError(
                f"Artifact {existing_session_id!r} is not a chat-session "
                f"(type={existing.type.value})."
            )
        state = ChatSessionState.from_content(existing.content or {})
        return existing, state

    artifact_type = (
        ArtifactType.SOCRATIC_DRILL
        if mode == "socratic"
        else ArtifactType.COLD_CALL_SESSION
    )
    state = ChatSessionState(
        case_block_id=case_block_id,
        history=[],
        mode=mode,
        started_at=datetime.now(tz=UTC),
    )
    artifact = Artifact(
        corpus_id=corpus_id,
        type=artifact_type,
        created_by=CreatedBy.SYSTEM,
        sources=[{"kind": "block", "id": case_block_id}],
        content=state.to_content(),
        prompt_template="",  # set after the first LLM call (so we know which template)
        llm_model="",
        cost_usd=Decimal("0"),
        cache_key="",
        regenerable=False,
    )
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact, state


def append_turn(
    session: Session,
    artifact_id: str,
    turn: ChatTurn,
) -> ChatSessionState:
    """Load → append turn → commit → return new state.

    Reads the artifact, mutates its ``content["history"]``, commits, and
    returns the parsed :class:`ChatSessionState`. We re-serialize the whole
    content dict on each call rather than diffing — JSON columns don't
    support partial updates, and the history is bounded by session length
    (~30 turns max).
    """
    artifact = session.exec(
        select(Artifact).where(Artifact.id == artifact_id)
    ).first()
    if artifact is None:
        raise ChatSessionError(f"Chat session {artifact_id!r} not found.")

    state = ChatSessionState.from_content(artifact.content or {})
    state.history.append(asdict(turn))

    artifact.content = state.to_content()
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return state


def close_session(session: Session, artifact_id: str) -> Artifact:
    """Set ``ended_at = utcnow()`` in the content, commit, return artifact.

    Idempotent: calling on an already-closed session updates ``ended_at`` to
    the current time. The cold-call debrief flow calls this after persisting
    the debrief turn — the timestamp marks "session officially over."
    """
    artifact = session.exec(
        select(Artifact).where(Artifact.id == artifact_id)
    ).first()
    if artifact is None:
        raise ChatSessionError(f"Chat session {artifact_id!r} not found.")

    state = ChatSessionState.from_content(artifact.content or {})
    state.ended_at = datetime.now(tz=UTC)
    artifact.content = state.to_content()
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChatSessionError(RuntimeError):
    """Raised by chat_session helpers when an artifact lookup fails or the
    artifact has the wrong type. Feature-level routes map to 404."""


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _from_iso(s: str) -> datetime:
    """Tolerant ISO parser — handles trailing ``Z`` and naive strings."""
    raw = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Last-resort: drop fractional precision and retry.
        dt = datetime.fromisoformat(raw.split(".")[0])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Test/UI helpers
# ---------------------------------------------------------------------------


def history_to_prompt_dicts(state: ChatSessionState) -> list[dict[str, Any]]:
    """Project :attr:`ChatSessionState.history` into the minimal shape the
    prompt templates expect: ``{role, content}`` per turn.

    The full :class:`ChatTurn` dict carries professor-only metadata
    (``intent``, ``escalation_level``) that the templates don't render —
    keeping the prompt input minimal makes the rendered prompt smaller and
    more focused.
    """
    return [
        {"role": str(t.get("role", "")), "content": str(t.get("content", ""))}
        for t in state.history
    ]


# Round-trip JSON for tests that want to dump + reparse session content.
def state_to_json(state: ChatSessionState) -> str:
    return json.dumps(state.to_content(), default=str)


__all__ = [
    "ChatMode",
    "ChatRole",
    "ChatSessionError",
    "ChatSessionState",
    "ChatTurn",
    "append_turn",
    "close_session",
    "history_to_prompt_dicts",
    "load_or_create_session",
    "state_to_json",
]
