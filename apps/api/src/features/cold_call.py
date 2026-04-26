"""Cold-call simulator (spec §5.6).

Mirror of :mod:`features.socratic_drill` with three differences:

1. Mode is ``cold_call`` — sessions go under
   :class:`ArtifactType.COLD_CALL_SESSION`.
2. The ``cold_call`` prompt template gets ``elapsed_seconds`` (since
   ``started_at`` on the session) plus a ``mode`` field
   (``"question"`` or ``"debrief"``) for time-pressure cues.
3. Two endpoints: :func:`cold_call_next_turn` for normal Q&A turns,
   :func:`cold_call_debrief` for the final summary turn that also closes
   the session.

Direct-Anthropic rationale: same as socratic_drill — see that module's
docstring.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import anthropic
from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget, record_llm_call
from credentials.keyring_backend import load_credentials
from data.models import Artifact, Provider
from features.chat_session import (
    ChatSessionError,
    ChatSessionState,
    ChatTurn,
    append_turn,
    close_session,
    history_to_prompt_dicts,
    load_or_create_session,
)
from features.socratic_drill import (
    SocraticTurnRequest,
    SocraticTurnResult,
)
from primitives.prompt_loader import load_output_schema, load_template
from primitives.template_renderer import render_template

# We deliberately do NOT reuse `_call_socratic_llm` from socratic_drill since
# it pins the template name. Cold call has its own template + extra inputs.

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test hook — independent factory so a single test can mock cold_call without
# affecting socratic_drill.
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
# Constants
# ---------------------------------------------------------------------------


_TEMPLATE_NAME = "cold_call"
_FEATURE_LABEL = "cold_call"


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ColdCallError(RuntimeError):
    """Feature-level failure — wraps the underlying SocraticDrillError /
    chat_session error so routes only need to catch one type."""


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def cold_call_next_turn(
    session: Session, req: SocraticTurnRequest
) -> SocraticTurnResult:
    """One question turn in a cold-call session. Same request/response shape
    as :func:`socratic_drill.socratic_next_turn` so the two endpoints share
    a UI."""
    return _next_turn(session, req, mode_label="question")


def cold_call_debrief(
    session: Session, session_id: str
) -> SocraticTurnResult:
    """Final call — render the cold_call prompt with ``mode="debrief"`` so
    the LLM produces a summary turn referencing earlier turns by index, then
    :func:`close_session` to set ``ended_at``.

    Cold-call debrief is *one* additional LLM call after the last student
    answer; it isn't a multi-turn loop. Calling debrief twice is allowed
    (the second call gets a fresh debrief and re-stamps ``ended_at``).
    """
    raise_if_over_budget()

    artifact_row = session.exec(
        select(Artifact).where(Artifact.id == session_id)
    ).first()
    if artifact_row is None:
        raise ColdCallError(f"Cold-call session {session_id!r} not found.")

    case_block_id = str((artifact_row.content or {}).get("case_block_id", ""))
    if not case_block_id:
        raise ColdCallError(
            f"Cold-call session {session_id!r} has no case_block_id; "
            "cannot run debrief."
        )

    debrief_req = SocraticTurnRequest(
        corpus_id=artifact_row.corpus_id,
        case_block_id=case_block_id,
        session_id=session_id,
        user_answer=None,
        professor_profile_id=None,
    )
    result = _next_turn(session, debrief_req, mode_label="debrief")

    # Mark the session ended.
    close_session(session, session_id)
    return result


# ---------------------------------------------------------------------------
# Shared logic
# ---------------------------------------------------------------------------


def _next_turn(
    session: Session,
    req: SocraticTurnRequest,
    *,
    mode_label: str,
) -> SocraticTurnResult:
    """Internal: shared turn-emission logic for both question and debrief."""
    raise_if_over_budget()

    try:
        artifact, state = load_or_create_session(
            session,
            corpus_id=req.corpus_id,
            case_block_id=req.case_block_id,
            mode="cold_call",
            existing_session_id=req.session_id,
        )
    except ChatSessionError as exc:
        raise ColdCallError(str(exc)) from exc

    # If we have a user answer (question mode), append before the LLM call.
    if req.user_answer is not None:
        student_turn = ChatTurn(
            role="student",
            content=req.user_answer,
            timestamp_offset_s=_elapsed_seconds_local(state.started_at),
        )
        state = append_turn(session, artifact.id, student_turn)
        artifact = session.exec(
            select(Artifact).where(Artifact.id == artifact.id)
        ).first()  # type: ignore[assignment]
        if artifact is None:
            raise ColdCallError(
                "Session artifact disappeared between turns; aborting."
            )

    case_opinion, following_notes = _fetch_case_blocks(session, req.case_block_id)
    profile_dict = _load_profile_dict(session, req.professor_profile_id)

    prompt_history = history_to_prompt_dicts(state)
    turn_index = _next_turn_index(state)
    elapsed_seconds = int(_elapsed_seconds_local(state.started_at))

    parsed_turn, input_tokens, output_tokens, model, template_id = _call_cold_call_llm(
        case_opinion=case_opinion,
        following_notes=following_notes,
        professor_profile=profile_dict,
        history=prompt_history,
        turn_index=turn_index,
        elapsed_seconds=elapsed_seconds,
        mode=mode_label,
    )

    # Defensive defaults — `intent`/`escalation_level`/`mode` are no longer
    # required by the schema (the LLM sometimes drops them despite the prompt
    # asking for them). The chat session continues; downstream grading just
    # treats missing intent as "open_facts" which is the safe neutral default.
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
        # The prompt's mode is the authoritative value when present; fall back
        # to mode_label so debrief sessions are still tagged correctly.
        mode=str(parsed_turn.get("mode") or mode_label),
        timestamp_offset_s=_elapsed_seconds_local(state.started_at),
    )
    final_state = append_turn(session, artifact.id, professor_turn)

    if not artifact.prompt_template or not artifact.llm_model:
        artifact = session.exec(
            select(Artifact).where(Artifact.id == artifact.id)
        ).first()  # type: ignore[assignment]
        if artifact is not None and (
            not artifact.prompt_template or not artifact.llm_model
        ):
            artifact.prompt_template = template_id
            artifact.llm_model = model
            session.add(artifact)
            session.commit()
            session.refresh(artifact)

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
# LLM plumbing — uses the cold_call template (different inputs than socratic)
# ---------------------------------------------------------------------------


def _call_cold_call_llm(
    *,
    case_opinion: dict[str, Any],
    following_notes: list[dict[str, Any]],
    professor_profile: dict[str, Any] | None,
    history: list[dict[str, Any]],
    turn_index: int,
    elapsed_seconds: int,
    mode: str,
) -> tuple[dict[str, Any], int, int, str, str]:
    """Render :file:`cold_call.prompt.md` + call Anthropic. Returns
    ``(parsed_turn, input_tokens, output_tokens, model, template_id)``."""
    import json as _json
    import re as _re

    import jsonschema

    template = load_template(_TEMPLATE_NAME)
    schema = load_output_schema(template)

    context: dict[str, Any] = {
        "case_opinion": case_opinion,
        "following_notes": following_notes,
        "professor_profile": professor_profile,
        "history": history,
        "turn_index": turn_index,
        "elapsed_seconds": elapsed_seconds,
        "mode": mode,
    }
    rendered = render_template(template, context)

    model = str(template.model_defaults.get("model", "claude-opus-4-7"))
    max_tokens = int(template.model_defaults.get("max_tokens", 1400))
    temperature = float(template.model_defaults.get("temperature", 0.3))

    creds = load_credentials()
    if creds.anthropic_api_key is None:
        raise ColdCallError(
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
        # Surface the actual Anthropic error message — `BadRequestError` alone
        # tells the user nothing useful (it could be model name, max_tokens,
        # empty messages, etc.). The SDK exposes the server's body via .message.
        detail = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        raise ColdCallError(
            f"Anthropic call failed during {_TEMPLATE_NAME} "
            f"({type(exc).__name__}): {detail}"
        ) from exc

    raw_text = _extract_text(response)
    parsed = _parse_json_local(raw_text, _re=_re, _json=_json)
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise ColdCallError(
            f"{_TEMPLATE_NAME} response did not match schema: {exc.message}"
        ) from None

    input_tokens, output_tokens = _usage_tokens(response)
    return parsed, input_tokens, output_tokens, model, template.identifier


# ---------------------------------------------------------------------------
# Local helpers — duplicated narrowly to avoid taking a private dep on
# socratic_drill's internals (the two features may diverge in the future).
# ---------------------------------------------------------------------------


def _fetch_case_blocks(
    session: Session, block_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from data.models import Block, BlockType
    from primitives.retrieve import CaseReferenceQuery, retrieve

    opinion = session.exec(
        select(Block).where(Block.id == block_id)
    ).first()
    if opinion is None:
        raise ColdCallError(f"Case block {block_id!r} not found.")
    if opinion.type is not BlockType.CASE_OPINION:
        raise ColdCallError(
            f"Block {block_id!r} is type {opinion.type.value}; "
            "expected case_opinion."
        )

    case_name = opinion.block_metadata.get("case_name")
    trailing_blocks: list[Any] = []
    if case_name:
        result = retrieve(
            session,
            CaseReferenceQuery(case_name=str(case_name), book_id=opinion.book_id),
        )
        if result.blocks:
            trailing_blocks = list(result.blocks[1:])

    return _block_to_dict(opinion), [_block_to_dict(b) for b in trailing_blocks]


def _block_to_dict(b: Any) -> dict[str, Any]:
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
    from data.models import ProfessorProfile

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


def _next_turn_index(state: ChatSessionState) -> int:
    professor_count = sum(
        1 for t in state.history if str(t.get("role", "")) == "professor"
    )
    return professor_count + 1


def _elapsed_seconds_local(started_at: Any) -> float:
    from datetime import UTC
    from datetime import datetime as _datetime

    sa = started_at
    if sa.tzinfo is None:
        sa = sa.replace(tzinfo=UTC)
    return max(0.0, (_datetime.now(tz=UTC) - sa).total_seconds())


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts)


def _usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _parse_json_local(raw: str, *, _re: Any, _json: Any) -> dict[str, Any]:
    """Tolerant parse — bare JSON, json fences, or prose-prefixed JSON."""
    stripped = raw.strip()
    if not stripped:
        raise ColdCallError("response was empty")
    try:
        parsed = _json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except _json.JSONDecodeError:
        pass

    fence_re = _re.compile(
        r"```(?:json)?\s*\n?(?P<body>.*?)\n?```", _re.DOTALL | _re.IGNORECASE
    )
    match = fence_re.search(stripped)
    if match:
        try:
            parsed = _json.loads(match.group("body").strip())
            if isinstance(parsed, dict):
                return parsed
        except _json.JSONDecodeError as exc:
            raise ColdCallError(
                f"fenced body was invalid JSON: {exc}"
            ) from None

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = _json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except _json.JSONDecodeError as exc:
            raise ColdCallError(
                f"could not recover JSON object: {exc}"
            ) from None
    raise ColdCallError("response did not contain a JSON object")


__all__ = [
    "ColdCallError",
    "SocraticTurnRequest",  # re-exported for caller convenience
    "SocraticTurnResult",  # re-exported for caller convenience
    "cold_call_debrief",
    "cold_call_next_turn",
    "set_anthropic_client_factory",
]
