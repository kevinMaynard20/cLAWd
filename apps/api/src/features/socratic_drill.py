"""Socratic drill feature (spec §5.4).

Per-turn orchestration of an interactive Socratic drill:

1. Budget gate.
2. Load (or create) the session Artifact via :mod:`features.chat_session`.
3. If the request carries ``user_answer``, append a *student* turn to the
   history first.
4. Retrieve the case opinion + trailing notes via
   :class:`primitives.retrieve.CaseReferenceQuery`.
5. Render the ``socratic_drill`` prompt and call Anthropic *directly*
   (NOT through :func:`primitives.generate.generate`) so the session
   Artifact is the only artifact that grows: each turn would otherwise
   create a new sibling Artifact, ballooning the table.
6. Parse the response into a :class:`ChatTurn`, append to history, persist.
7. Return the latest professor turn + the full history.

Direct-Anthropic rationale (mirrors :mod:`features.transcript_ingest` and
:mod:`features.emphasis_mapper`): the generate() primitive is built around
the assumption that a feature produces *one* Artifact per call — a typed
case brief, a rubric, etc. A Socratic-drill turn is just one step inside a
longer-lived envelope, so we reuse the prompt-loader / template-renderer /
record_llm_call machinery without the wrapping Artifact.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic
import jsonschema
from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget, record_llm_call
from credentials.keyring_backend import load_credentials
from data.models import (
    Artifact,
    Block,
    BlockType,
    ProfessorProfile,
    Provider,
)
from features.chat_session import (
    ChatSessionError,
    ChatSessionState,
    ChatTurn,
    append_turn,
    history_to_prompt_dicts,
    load_or_create_session,
)
from features.chat_session import (
    close_session as _close_session,  # re-exported for symmetry with cold_call
)
from primitives.prompt_loader import load_output_schema, load_template
from primitives.retrieve import CaseReferenceQuery, retrieve
from primitives.template_renderer import render_template

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test hook (matches the convention from transcript_ingest / emphasis_mapper)
# ---------------------------------------------------------------------------


_client_factory: Callable[[str], Any] | None = None


def set_anthropic_client_factory(factory: Callable[[str], Any] | None) -> None:
    """Tests inject a fake client; pass ``None`` to restore the real SDK."""
    global _client_factory
    _client_factory = factory


def _make_client(api_key: str) -> Any:
    if _client_factory is not None:
        return _client_factory(api_key)
    return anthropic.Anthropic(api_key=api_key)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class SocraticTurnRequest:
    """Per-turn input.

    On the very first turn of a session the caller sets ``session_id=None``
    AND ``user_answer=None`` — the feature creates the session Artifact and
    asks the LLM for an opener (typically ``intent="open_facts"``).
    Subsequent turns pass the returned ``session_id`` plus the student's
    last answer.
    """

    corpus_id: str
    case_block_id: str
    session_id: str | None = None
    user_answer: str | None = None
    professor_profile_id: str | None = None


@dataclass
class SocraticTurnResult:
    session_id: str
    turn_index: int
    professor_turn: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)


class SocraticDrillError(RuntimeError):
    """Feature-level failure — block not found / wrong type / API key
    missing / LLM response invalid after retries. Routes map to 404/503."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_TEMPLATE_NAME = "socratic_drill"
_FEATURE_LABEL = "socratic_drill"


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def socratic_next_turn(
    session: Session, req: SocraticTurnRequest
) -> SocraticTurnResult:
    """Compute the next professor turn for a Socratic drill session."""
    raise_if_over_budget()

    # 1. Load / create the session envelope.
    try:
        artifact, state = load_or_create_session(
            session,
            corpus_id=req.corpus_id,
            case_block_id=req.case_block_id,
            mode="socratic",
            existing_session_id=req.session_id,
        )
    except ChatSessionError as exc:
        raise SocraticDrillError(str(exc)) from exc

    # 2. If we have a user answer, append it before asking the LLM.
    if req.user_answer is not None:
        student_turn = ChatTurn(
            role="student",
            content=req.user_answer,
            timestamp_offset_s=_elapsed_seconds(state.started_at),
        )
        state = append_turn(session, artifact.id, student_turn)
        # Re-load the artifact so subsequent operations see the freshest copy.
        artifact = session.exec(
            select(Artifact).where(Artifact.id == artifact.id)
        ).first()  # type: ignore[assignment]
        if artifact is None:
            raise SocraticDrillError(
                "Session artifact disappeared between turns; aborting."
            )

    # 3. Retrieve case opinion + trailing notes.
    case_opinion, following_notes = _fetch_case_blocks(session, req.case_block_id)

    # 4. Optional professor profile lookup.
    profile_dict = _load_profile_dict(session, req.professor_profile_id)

    # 5. Render the prompt and call Anthropic.
    prompt_history = history_to_prompt_dicts(state)
    turn_index = _next_turn_index(state)

    parsed_turn, input_tokens, output_tokens, model, template_id = _call_socratic_llm(
        case_opinion=case_opinion,
        following_notes=following_notes,
        professor_profile=profile_dict,
        history=prompt_history,
        turn_index=turn_index,
    )

    # 6. Append the professor turn to the session.
    # Defensive defaults — `intent`/`escalation_level` are no longer required
    # by the schema (the LLM sometimes drops them despite the prompt asking).
    # Treating a missing intent as "open_facts" is the neutral default.
    raw_intent = parsed_turn.get("intent")
    intent_value = str(raw_intent) if raw_intent else "open_facts"
    raw_esc = parsed_turn.get("escalation_level")
    escalation_value: int | None
    try:
        escalation_value = int(raw_esc) if raw_esc is not None else None
    except (TypeError, ValueError):
        escalation_value = None

    professor_turn = ChatTurn(
        role="professor",
        content=str(parsed_turn.get("question", "")),
        intent=intent_value,
        escalation_level=escalation_value,
        mode=str(parsed_turn.get("mode", "question")),
        timestamp_offset_s=_elapsed_seconds(state.started_at),
    )
    final_state = append_turn(session, artifact.id, professor_turn)

    # 7. Stamp prompt_template / llm_model on the artifact (for provenance);
    #    these are set on the first turn and left alone on subsequent ones.
    if not artifact.prompt_template or not artifact.llm_model:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == artifact.id)
        ).first()  # type: ignore[assignment]
        if artifact is not None and (not artifact.prompt_template or not artifact.llm_model):
            artifact.prompt_template = template_id
            artifact.llm_model = model
            session.add(artifact)
            session.commit()
            session.refresh(artifact)

    # 8. CostEvent — cost is tracked per turn, attributed to the session
    #    artifact so the cost-detail panel rolls up cleanly.
    record_llm_call(
        model=model,
        provider=Provider.ANTHROPIC,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        feature=_FEATURE_LABEL,
        artifact_id=artifact.id if artifact is not None else None,
    )

    return SocraticTurnResult(
        session_id=artifact.id if artifact is not None else "",
        turn_index=turn_index,
        professor_turn=dict(parsed_turn),
        history=list(final_state.history),
    )


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(?P<body>.*?)\n?```", re.DOTALL | re.IGNORECASE
)


def _call_socratic_llm(
    *,
    case_opinion: dict[str, Any],
    following_notes: list[dict[str, Any]],
    professor_profile: dict[str, Any] | None,
    history: list[dict[str, Any]],
    turn_index: int,
) -> tuple[dict[str, Any], int, int, str, str]:
    """Render :file:`socratic_drill.prompt.md` + call Anthropic.

    Returns ``(parsed_turn, input_tokens, output_tokens, model, template_id)``.
    Raises :class:`SocraticDrillError` on API or schema failures so the
    feature stays the only error type the caller has to catch.
    """
    template = load_template(_TEMPLATE_NAME)
    schema = load_output_schema(template)

    context: dict[str, Any] = {
        "case_opinion": case_opinion,
        "following_notes": following_notes,
        "professor_profile": professor_profile,
        "history": history,
        "turn_index": turn_index,
    }
    rendered = render_template(template, context)

    model = str(template.model_defaults.get("model", "claude-opus-4-7"))
    max_tokens = int(template.model_defaults.get("max_tokens", 1200))
    temperature = float(template.model_defaults.get("temperature", 0.3))

    creds = load_credentials()
    if creds.anthropic_api_key is None:
        raise SocraticDrillError(
            "No Anthropic API key stored — Settings → API Key."
        )
    api_key = creds.anthropic_api_key.get_secret_value()

    from llm import create_message

    client = _make_client(api_key)
    try:
        response = create_message(
            client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=f"Prompt template: {template.identifier}",
            messages=[{"role": "user", "content": rendered}],
        )
    except Exception as exc:
        detail = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        raise SocraticDrillError(
            f"Anthropic call failed during {_TEMPLATE_NAME} "
            f"({type(exc).__name__}): {detail}"
        ) from exc

    raw_text = _extract_text(response)
    try:
        parsed = _parse_json(raw_text)
    except ValueError as exc:
        raise SocraticDrillError(
            f"{_TEMPLATE_NAME} response was not valid JSON: {exc}"
        ) from None

    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise SocraticDrillError(
            f"{_TEMPLATE_NAME} response did not match schema: {exc.message}"
        ) from None

    input_tokens, output_tokens = _usage_tokens(response)
    return parsed, input_tokens, output_tokens, model, template.identifier


# ---------------------------------------------------------------------------
# Block / profile helpers
# ---------------------------------------------------------------------------


def _fetch_case_blocks(
    session: Session, block_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load the case opinion block + trailing notes, returning prompt-friendly
    dicts. Raises :class:`SocraticDrillError` if the block is missing or
    isn't a case_opinion."""
    opinion = session.exec(
        select(Block).where(Block.id == block_id)
    ).first()
    if opinion is None:
        raise SocraticDrillError(f"Case block {block_id!r} not found.")
    if opinion.type is not BlockType.CASE_OPINION:
        raise SocraticDrillError(
            f"Block {block_id!r} is type {opinion.type.value}; "
            "expected case_opinion."
        )

    case_name = opinion.block_metadata.get("case_name")
    trailing: list[Block] = []
    if case_name:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name=str(case_name), book_id=opinion.book_id),
        )
        # retrieve puts the matching opinion first, then trailing blocks.
        if result.blocks:
            trailing = list(result.blocks[1:])

    return _block_to_dict(opinion), [_block_to_dict(b) for b in trailing]


def _block_to_dict(b: Block) -> dict[str, Any]:
    """Project a :class:`Block` into the dict shape the prompt templates
    expect (case_name, court, year, citation, markdown, …)."""
    return {
        "id": b.id,
        "type": b.type.value,
        "source_page": b.source_page,
        "markdown": b.markdown,
        "block_metadata": dict(b.block_metadata or {}),
    }


def _load_profile_dict(
    session: Session, profile_id: str | None
) -> dict[str, Any] | None:
    """Optional ProfessorProfile lookup. Returns None when ``profile_id`` is
    None or no row matches — Socratic drill works without a profile (the
    prompt has a fallback persona)."""
    if profile_id is None:
        return None
    row = session.exec(
        select(ProfessorProfile).where(ProfessorProfile.id == profile_id)
    ).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "professor_name": row.professor_name,
        "course": row.course,
        "school": row.school,
        "exam_format": row.exam_format,
        "pet_peeves": row.pet_peeves,
        "favored_framings": row.favored_framings,
        "stable_traps": row.stable_traps,
        "voice_conventions": row.voice_conventions,
        "commonly_tested": row.commonly_tested,
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _next_turn_index(state: ChatSessionState) -> int:
    """1-based index of the *upcoming* professor turn — counts only
    ``role='professor'`` entries already in history and adds 1."""
    professor_count = sum(
        1 for t in state.history if str(t.get("role", "")) == "professor"
    )
    return professor_count + 1


def _elapsed_seconds(started_at: datetime) -> float:
    """Seconds since ``started_at``, clamped to >= 0."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    delta = datetime.now(tz=UTC) - started_at
    return max(0.0, delta.total_seconds())


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts)


def _parse_json(raw: str) -> dict[str, Any]:
    """Tolerant JSON parse — accepts bare JSON, ``json`` fences, or prose-
    prefixed JSON. Mirrors the shared parsing convention from emphasis_mapper
    and transcript_ingest."""
    stripped = raw.strip()
    if not stripped:
        raise ValueError("response was empty")

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = _FENCE_RE.search(stripped)
    if match:
        inner = match.group("body").strip()
        try:
            parsed = json.loads(inner)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"fenced body was invalid JSON: {exc}"
            ) from exc

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise ValueError(f"could not recover JSON object: {exc}") from exc

    raise ValueError("response did not contain a JSON object")


def _usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


# Re-exported for callers that want one-stop import.
close_session = _close_session


__all__ = [
    "SocraticDrillError",
    "SocraticTurnRequest",
    "SocraticTurnResult",
    "close_session",
    "set_anthropic_client_factory",
    "socratic_next_turn",
]
