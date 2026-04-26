"""Transcript-to-emphasis mapper (spec §5.7).

Two-pass design:

1. Rule-based mechanical scoring from :class:`TranscriptSegment` features
   (minutes-on, return count, hypotheticals run, engaged student questions,
   disclaimers) weighted per ``config/emphasis_weights.toml``.
2. LLM justification pass that reads the cleaned transcript, starts from the
   mechanical ``provisional_score`` and produces human-readable justifications
   + a sanity-checked ``exam_signal_score`` (allowed to shift ≤0.1 up or
   ≤0.2 down from the provisional — see the prompt file's hard rules).

Persistence: one :class:`EmphasisItem` row per (transcript_id, subject_kind,
subject_label). Second call without ``force_regenerate`` is a no-op that
returns the cached rows.

Direct Anthropic SDK use — same pattern as ``features/syllabus_ingest.py`` and
the other agent's transcript cleanup — because an ``EmphasisMap`` is its own
first-class corpus entity (spec §3.10) not a generated Artifact envelope.

CostEvent: emitted via :func:`costs.tracker.record_llm_call` with
``feature="emphasis_analysis"``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anthropic
import jsonschema
from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from costs.emphasis_weights import EmphasisWeights, get_weights
from costs.tracker import raise_if_over_budget, record_llm_call
from credentials.keyring_backend import load_credentials
from data.models import (
    EmphasisItem,
    EmphasisSubjectKind,
    Provider,
    Transcript,
    TranscriptSegment,
)
from primitives.prompt_loader import load_output_schema, load_template
from primitives.template_renderer import render_template

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (design choices beyond the spec; see the report summary for why)
# ---------------------------------------------------------------------------

# Approximate average characters-per-minute of spoken English: ~150 words/min
# × ~5 chars/word ≈ 750 cpm. (Q42 fix: prior value of 150.0 was off by ~5×,
# which inflated `minutes_on`. The `minutes_on_cap` in emphasis_weights.toml
# clamped the worst of it; the raw signal is now calibrated.)
_CHARS_PER_MINUTE = 750.0

# The LLM prompt lets the model shift exam_signal_score by at most +0.1 / -0.2
# from the mechanical provisional. We don't enforce that bound here (the
# prompt is the contract) — but we do clamp the final score to [0, 1] in case
# the model emits a value outside that range.
_SCORE_MIN = 0.0
_SCORE_MAX = 1.0

# Length cap for hypothetical-summary strings stored on
# :class:`EmphasisSubjectFeatures.hypotheticals_run`. 80 chars roughly matches
# one line of terminal width and avoids ballooning the LLM prompt payload.
_HYPO_SUMMARY_MAX_CHARS = 80

# Truncation target for the cleaned-transcript excerpt we pass to the LLM.
# The prompt doesn't strictly need the whole lecture; too-large payloads
# inflate cost without improving justifications.
_TRANSCRIPT_EXCERPT_MAX_CHARS = 8000

# Sentiment flag strings (mirrors spec §3.9).
_FLAG_DISCLAIMED = "disclaimed_as_not_testable"
_FLAG_HYPOTHETICAL = "professor_hypothetical"
_FLAG_ENGAGED_QUESTION = "student_question_professor_engaged"


# ---------------------------------------------------------------------------
# Test hook — matches syllabus_ingest / transcript_ingest convention
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


@dataclass(frozen=True)
class EmphasisSubjectFeatures:
    """Raw features per-subject — derived entirely from TranscriptSegment
    data. These are the inputs to :func:`compute_provisional_score` and to
    the LLM-justification prompt."""

    kind: EmphasisSubjectKind
    label: str
    minutes_on: float
    return_count: int
    hypotheticals_run: list[str]
    disclaimed: bool
    engaged_questions: int


@dataclass
class EmphasisMapRequest:
    corpus_id: str
    transcript_id: str
    professor_profile_id: str | None = None
    force_regenerate: bool = False


@dataclass
class EmphasisMapResult:
    items: list[EmphasisItem]  # persisted rows, ordered by exam_signal_score DESC
    summary: str | None
    cache_hit: bool  # True when items already existed and not force_regenerate
    warnings: list[str] = field(default_factory=list)


class EmphasisMapError(RuntimeError):
    """Feature-level failure — missing transcript / no API key / LLM output
    doesn't match schema after retries / etc."""


# ---------------------------------------------------------------------------
# Feature computation
# ---------------------------------------------------------------------------


def compute_subject_features(
    segments: list[TranscriptSegment],
) -> list[EmphasisSubjectFeatures]:
    """Aggregate per-subject features from ``segments``.

    Per the spec comment in §5.7 / §3.10:
    - ``minutes_on``: sum of ``(end_char - start_char) / 150.0`` across
      segments mentioning this subject.
    - ``return_count``: count of distinct segments mentioning this subject.
    - ``hypotheticals_run``: short strings summarizing each segment tagged
      ``professor_hypothetical`` that mentions this subject (first 80 chars).
    - ``disclaimed``: True if ANY segment mentioning this subject has the
      sentiment flag ``disclaimed_as_not_testable``.
    - ``engaged_questions``: count of segments with the flag
      ``student_question_professor_engaged`` that mention this subject.

    Subjects are the union of all segments' ``mentioned_cases`` +
    ``mentioned_rules`` + ``mentioned_concepts``. The same label appearing
    under multiple kinds is NOT collapsed — e.g., a CASE named "Penn Central"
    and a CONCEPT of the same label would produce two separate features
    (same (kind, label) tuple is what the DB unique index keys on).
    """
    # (kind, label) -> mutable accumulator
    accumulators: dict[tuple[EmphasisSubjectKind, str], _Accumulator] = {}

    for segment in segments:
        flags = set(segment.sentiment_flags or [])
        is_hypo = _FLAG_HYPOTHETICAL in flags
        is_disclaimed = _FLAG_DISCLAIMED in flags
        is_engaged_q = _FLAG_ENGAGED_QUESTION in flags

        length_minutes = _segment_length_minutes(segment)
        hypo_summary = _hypo_summary(segment) if is_hypo else None

        subjects_in_segment: list[tuple[EmphasisSubjectKind, str]] = []
        for case in segment.mentioned_cases or []:
            if case:
                subjects_in_segment.append((EmphasisSubjectKind.CASE, str(case)))
        for rule in segment.mentioned_rules or []:
            if rule:
                subjects_in_segment.append((EmphasisSubjectKind.RULE, str(rule)))
        for concept in segment.mentioned_concepts or []:
            if concept:
                subjects_in_segment.append(
                    (EmphasisSubjectKind.CONCEPT, str(concept))
                )

        # Deduplicate within this segment so return_count bumps by 1 per
        # segment regardless of duplicate mentions inside the same segment.
        seen_in_segment: set[tuple[EmphasisSubjectKind, str]] = set()
        for key in subjects_in_segment:
            if key in seen_in_segment:
                continue
            seen_in_segment.add(key)

            acc = accumulators.setdefault(key, _Accumulator())
            acc.minutes_on += length_minutes
            acc.return_count += 1
            if is_disclaimed:
                acc.disclaimed = True
            if is_engaged_q:
                acc.engaged_questions += 1
            if hypo_summary is not None:
                acc.hypotheticals_run.append(hypo_summary)

    # Stable ordering: by kind, then label. Makes the feature-list output
    # deterministic for tests and caches.
    ordered_keys = sorted(
        accumulators.keys(),
        key=lambda k: (k[0].value, k[1]),
    )
    return [
        EmphasisSubjectFeatures(
            kind=kind,
            label=label,
            minutes_on=accumulators[(kind, label)].minutes_on,
            return_count=accumulators[(kind, label)].return_count,
            hypotheticals_run=list(accumulators[(kind, label)].hypotheticals_run),
            disclaimed=accumulators[(kind, label)].disclaimed,
            engaged_questions=accumulators[(kind, label)].engaged_questions,
        )
        for kind, label in ordered_keys
    ]


@dataclass
class _Accumulator:
    minutes_on: float = 0.0
    return_count: int = 0
    hypotheticals_run: list[str] = field(default_factory=list)
    disclaimed: bool = False
    engaged_questions: int = 0


def _segment_length_minutes(segment: TranscriptSegment) -> float:
    """Estimate speech duration for a segment from its character span.

    Uses ``(end_char - start_char) / _CHARS_PER_MINUTE``. When ``end_char`` is
    somehow <= ``start_char`` we return 0.0 rather than a negative number so
    the sum stays well-defined.
    """
    if segment.end_char <= segment.start_char:
        return 0.0
    return (segment.end_char - segment.start_char) / _CHARS_PER_MINUTE


def _hypo_summary(segment: TranscriptSegment) -> str:
    """First ``_HYPO_SUMMARY_MAX_CHARS`` chars of the segment content."""
    content = segment.content or ""
    return content[:_HYPO_SUMMARY_MAX_CHARS]


def compute_provisional_score(
    features: EmphasisSubjectFeatures, weights: EmphasisWeights
) -> float:
    """Mechanical composite per spec §3.10. Returns a value in ``[0, 1]``.

    Formula:

    - Normalize each raw feature by its cap -> ``[0, 1]``.
    - ``weighted_sum`` = ``w.minutes_on * norm(minutes_on)`` +
      ``w.return_count * norm(return_count)`` +
      ``w.hypotheticals_run * norm(len(hypotheticals_run))`` +
      ``w.engaged_questions * norm(engaged_questions)`` +
      ``w.not_disclaimed * (0 if disclaimed else 1)``.
    - If ``features.disclaimed`` is True, add ``w.disclaimed_penalty`` (a
      negative number per ``config/emphasis_weights.toml``).
    - Clamp the result to ``[0, 1]``.
    """
    norm_minutes = _normalize_float(features.minutes_on, weights.minutes_on_cap)
    norm_returns = _normalize_int(features.return_count, weights.return_count_cap)
    norm_hypos = _normalize_int(
        len(features.hypotheticals_run), weights.hypotheticals_run_cap
    )
    norm_engaged = _normalize_int(
        features.engaged_questions, weights.engaged_questions_cap
    )
    not_disclaimed_flag = 0.0 if features.disclaimed else 1.0

    weighted_sum = (
        float(weights.minutes_on) * norm_minutes
        + float(weights.return_count) * norm_returns
        + float(weights.hypotheticals_run) * norm_hypos
        + float(weights.engaged_questions) * norm_engaged
        + float(weights.not_disclaimed) * not_disclaimed_flag
    )

    if features.disclaimed:
        weighted_sum += float(weights.disclaimed_penalty)

    return _clamp(weighted_sum, _SCORE_MIN, _SCORE_MAX)


def _normalize_float(value: float, cap: float) -> float:
    """Normalize a raw feature value to ``[0, 1]`` using ``cap`` as the
    saturation point. Values >= cap map to 1.0; negative inputs map to 0.0."""
    if cap <= 0:
        return 0.0
    if value <= 0:
        return 0.0
    return min(value / cap, 1.0)


def _normalize_int(value: int, cap: int) -> float:
    if cap <= 0:
        return 0.0
    if value <= 0:
        return 0.0
    return min(value / float(cap), 1.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def build_emphasis_map(
    session: Session, req: EmphasisMapRequest
) -> EmphasisMapResult:
    """Build / refresh the EmphasisMap for ``req.transcript_id``.

    Orchestration per spec §5.7:

    1. Fetch :class:`Transcript` + :class:`TranscriptSegment` rows.
    2. If EmphasisItems already exist for this transcript AND
       ``force_regenerate`` is False: return them with ``cache_hit=True``.
    3. :func:`compute_subject_features` ->
       :func:`compute_provisional_score` for each subject.
    4. Call the ``emphasis_analysis`` prompt via
       :mod:`primitives.prompt_loader` + :mod:`primitives.template_renderer`
       + the Anthropic SDK (direct, not via ``primitives.generate`` — same
       pattern as other ingest/feature modules that don't produce Artifact
       envelopes).
    5. Upsert an :class:`EmphasisItem` row per returned subject.
    6. Emit a :class:`CostEvent` via
       :func:`costs.tracker.record_llm_call` with
       ``feature="emphasis_analysis"``.
    7. Return items sorted by ``exam_signal_score`` DESC + the model's
       ``summary`` field.
    """
    # 1. Budget gate (bubbles BudgetExceededError to the route layer).
    raise_if_over_budget()

    # 2. Fetch transcript + segments.
    transcript = session.exec(
        select(Transcript).where(Transcript.id == req.transcript_id)
    ).first()
    if transcript is None:
        raise EmphasisMapError(
            f"Transcript {req.transcript_id!r} not found. "
            "Ingest the transcript before building its emphasis map."
        )

    # 3. Cache-hit check.
    existing_items = session.exec(
        select(EmphasisItem).where(
            EmphasisItem.transcript_id == req.transcript_id
        )
    ).all()
    if existing_items and not req.force_regenerate:
        # Detach so callers can read after the session closes.
        items_sorted = sorted(
            existing_items, key=lambda it: it.exam_signal_score, reverse=True
        )
        for it in items_sorted:
            session.expunge(it)
        return EmphasisMapResult(
            items=list(items_sorted),
            summary=None,
            cache_hit=True,
            warnings=[],
        )

    # Force-regenerate path: clear existing rows in a single statement so the
    # upsert step doesn't trip the unique index on
    # (transcript_id, subject_kind, subject_label).
    if existing_items and req.force_regenerate:
        session.exec(  # type: ignore[call-overload]
            sa_delete(EmphasisItem).where(
                EmphasisItem.transcript_id == req.transcript_id
            )
        )
        session.commit()

    # 4. Aggregate features.
    segments = list(
        session.exec(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == req.transcript_id)
            .order_by(TranscriptSegment.order_index)  # type: ignore[arg-type]
        ).all()
    )
    features_list = compute_subject_features(segments)

    # Empty-transcript short-circuit: no subjects -> no items and no LLM call.
    if not features_list:
        return EmphasisMapResult(
            items=[],
            summary=None,
            cache_hit=False,
            warnings=[
                "Transcript has no mentioned cases / rules / concepts; "
                "skipping LLM call.",
            ],
        )

    weights = get_weights()
    provisional_scores = {
        (f.kind, f.label): compute_provisional_score(f, weights)
        for f in features_list
    }

    # 5. Call the LLM.
    creds = load_credentials()
    if creds.anthropic_api_key is None:
        raise EmphasisMapError(
            "No Anthropic API key stored — Settings -> API Key."
        )

    template = load_template("emphasis_analysis")
    schema = load_output_schema(template)

    excerpt = (transcript.cleaned_text or transcript.raw_text or "")[
        :_TRANSCRIPT_EXCERPT_MAX_CHARS
    ]
    context = {
        "transcript_topic": transcript.topic,
        "cleaned_text_excerpt": excerpt,
        "subjects": [
            {
                "kind": f.kind.value,
                "label": f.label,
                "minutes_on": round(f.minutes_on, 2),
                "return_count": f.return_count,
                "hypotheticals_run": list(f.hypotheticals_run),
                "disclaimed": f.disclaimed,
                "engaged_questions": f.engaged_questions,
                "provisional_score": round(
                    provisional_scores[(f.kind, f.label)], 2
                ),
            }
            for f in features_list
        ],
    }
    rendered = render_template(template, context)

    model = str(template.model_defaults.get("model", "claude-opus-4-7"))
    max_tokens = int(template.model_defaults.get("max_tokens", 5000))
    temperature = float(template.model_defaults.get("temperature", 0.2))

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
        raise EmphasisMapError(
            f"Anthropic call failed during emphasis_analysis "
            f"({type(exc).__name__}): {detail}"
        ) from exc

    raw_text = response.content[0].text
    payload = _parse_json(raw_text)

    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise EmphasisMapError(
            f"emphasis_analysis output did not match schema: {exc.message}"
        ) from exc

    # 6. Cost event.
    input_tokens = int(getattr(response.usage, "input_tokens", 0))
    output_tokens = int(getattr(response.usage, "output_tokens", 0))
    record_llm_call(
        model=model,
        provider=Provider.ANTHROPIC,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        feature="emphasis_analysis",
    )

    # 7. Persist the items.
    persisted: list[EmphasisItem] = []
    warnings: list[str] = []

    # Index features by (kind, label) so we can re-attach the mechanical
    # values to the LLM-returned row in case the prompt omitted them.
    features_by_key: dict[tuple[EmphasisSubjectKind, str], EmphasisSubjectFeatures] = {
        (f.kind, f.label): f for f in features_list
    }

    for item_payload in payload.get("items", []):
        try:
            kind = EmphasisSubjectKind(item_payload["subject_kind"])
        except ValueError:
            warnings.append(
                f"Dropped emphasis item with unknown kind: "
                f"{item_payload.get('subject_kind')!r}"
            )
            continue
        label = str(item_payload["subject_label"])
        mechanical = features_by_key.get((kind, label))
        if mechanical is None:
            warnings.append(
                f"LLM returned subject {kind.value}:{label!r} not in input set; skipping."
            )
            continue

        raw_score = item_payload.get("exam_signal_score", 0.0)
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 0.0
            warnings.append(
                f"Non-numeric exam_signal_score for {label!r} — coerced to 0.0."
            )
        score = _clamp(score, _SCORE_MIN, _SCORE_MAX)

        justification = str(item_payload.get("justification", "")).strip()

        row = EmphasisItem(
            transcript_id=req.transcript_id,
            subject_kind=kind,
            subject_label=label,
            minutes_on=float(mechanical.minutes_on),
            return_count=int(mechanical.return_count),
            hypotheticals_run=list(mechanical.hypotheticals_run),
            disclaimed=bool(mechanical.disclaimed),
            engaged_questions=int(mechanical.engaged_questions),
            exam_signal_score=score,
            justification=justification,
        )
        session.add(row)
        persisted.append(row)

    session.commit()
    for row in persisted:
        session.refresh(row)

    persisted_sorted = sorted(
        persisted, key=lambda r: r.exam_signal_score, reverse=True
    )
    for row in persisted_sorted:
        session.expunge(row)

    summary_val = payload.get("summary")
    summary: str | None = (
        str(summary_val) if isinstance(summary_val, str) and summary_val else None
    )

    return EmphasisMapResult(
        items=persisted_sorted,
        summary=summary,
        cache_hit=False,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json(raw: str) -> dict[str, Any]:
    """Tolerant parse: accept bare JSON, ```json fences, or prose-prefix JSON.

    Mirrors the loose parser used by :mod:`features.syllabus_ingest` and the
    generate primitive so response-shape tolerance is consistent across
    features.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline + 1 :]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-ditch recovery: grab the first '{' through the last '}'.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise


# Keep the unused-import guard explicit so ruff doesn't strip Decimal if we
# later move back to Decimal arithmetic in the score pipeline.
_ = Decimal


__all__ = [
    "EmphasisMapError",
    "EmphasisMapRequest",
    "EmphasisMapResult",
    "EmphasisSubjectFeatures",
    "build_emphasis_map",
    "compute_provisional_score",
    "compute_subject_features",
    "set_anthropic_client_factory",
]
