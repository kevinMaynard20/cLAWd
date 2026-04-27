"""Transcript ingestion feature (spec §4.1.2 text path, §4.1.5 audio path).

Orchestrates raw-Gemini-text → cleaned ``Transcript`` + speaker-bounded
``TranscriptSegment`` rows. Steps per spec §4.1.2:

1. Hash the raw text → content-addressed Transcript id. If a transcript with
   that id already exists in the same corpus, return it as a cache hit.
2. Fetch the corpus's known canonical case names (via
   ``fuzzy_resolver.load_known_case_names_for_corpus``) to feed into the LLM
   cleanup prompt AND the post-LLM fuzzy resolver pass.
3. Render the ``transcript_cleanup`` prompt template and call Anthropic
   directly (NOT via ``primitives.generate``) — the generate() primitive's
   Artifact envelope is designed for student-facing outputs, and a cleaned
   transcript is not one of those (it lives on the ``Transcript`` table
   directly). We still reuse the prompt-loader / template-renderer /
   CostEvent infrastructure, just without the Artifact wrapper.
4. Parse the LLM JSON response; validate against
   ``schemas/transcript_cleanup.json``.
5. For every cleaned segment, run the fuzzy resolver on the raw segment text
   as a safety net for anything the LLM missed (the transcript_cleanup prompt
   asks the LLM to resolve cases but doesn't guarantee it — we rely on
   belt-and-suspenders to catch the "Shelly B Kramer"-style deformations).
6. Persist the ``Transcript`` + ``TranscriptSegment`` rows. Emit a CostEvent
   with real token counts.

Audio path (§4.1.5) is stubbed: faster-whisper is in the ``[audio]`` optional
extra and isn't currently installed. Wiring lands in a follow-up slice.

Test hook: ``set_client_factory`` accepts the same FakeClient pattern as
``primitives.generate.set_anthropic_client_factory``. Tests inject a mock
that returns a pre-baked transcript_cleanup JSON payload.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import jsonschema
import structlog
from sqlmodel import Session, select

from costs.tracker import raise_if_over_budget, record_llm_call
from credentials.keyring_backend import load_credentials
from data.models import (
    Corpus,
    Provider,
    Speaker,
    Transcript,
    TranscriptSegment,
    TranscriptSourceType,
)
from primitives.fuzzy_resolver import (
    load_known_case_names_for_corpus,
    resolve_case_names,
)
from primitives.prompt_loader import load_output_schema, load_template
from primitives.template_renderer import render_template

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class TranscriptIngestRequest:
    """Inputs for ingesting a text transcript (spec §4.1.2).

    ``raw_text`` is the Gemini rough output (what the user paste-s into the
    transcript-ingest UI). ``lecture_date`` / ``topic`` / ``assignment_code``
    / ``source_path`` are optional metadata — the user populates what they
    know and we pass ``None`` for the rest.
    """

    corpus_id: str
    raw_text: str
    lecture_date: datetime | None = None
    topic: str | None = None
    assignment_code: str | None = None
    source_path: str | None = None


@dataclass
class TranscriptIngestResult:
    """Outcome of a transcript ingest call.

    - ``transcript``: the persisted ``Transcript`` row (detached from the
      session so callers can read attributes after teardown).
    - ``segment_count``: number of ``TranscriptSegment`` rows created.
    - ``mentioned_cases``: deduplicated canonical case names mentioned
      anywhere in the transcript (the union of LLM mentions + fuzzy
      resolver hits).
    - ``unresolved_mentions``: raw case-like strings that didn't resolve
      against the corpus's known cases — surfaced for manual review.
    - ``cache_hit``: True when the raw_text's hash matched an existing
      ``Transcript`` and no LLM call was made.
    """

    transcript: Transcript
    segment_count: int
    mentioned_cases: list[str] = field(default_factory=list)
    unresolved_mentions: list[str] = field(default_factory=list)
    cache_hit: bool = False


class TranscriptIngestError(RuntimeError):
    """Feature-level ingest failure — raised for e.g. unknown corpus or
    malformed LLM response after all retries. Route layer maps to 4xx/5xx."""


# ---------------------------------------------------------------------------
# Client-factory injection (test hook)
# ---------------------------------------------------------------------------


_anthropic_client_factory: Callable[[str], Any] | None = None


def set_client_factory(factory: Callable[[str], Any] | None) -> None:
    """Tests monkey-patch this to inject a mock Anthropic-like client.

    Mirrors ``primitives.generate.set_anthropic_client_factory`` so test
    suites can reuse the same fake-client fixtures. Pass ``None`` to restore
    the default real-SDK factory.
    """
    global _anthropic_client_factory
    _anthropic_client_factory = factory


def _make_client(api_key: str) -> Any:
    """Build an Anthropic client, preferring the test factory when set."""
    if _anthropic_client_factory is not None:
        return _anthropic_client_factory(api_key)
    return anthropic.Anthropic(api_key=api_key)


# ---------------------------------------------------------------------------
# Template identifier (pinned to the version in the prompt frontmatter)
# ---------------------------------------------------------------------------


_TEMPLATE_NAME = "transcript_cleanup"
_FEATURE_LABEL = "transcript_cleanup"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(?P<body>.*?)\n?```", re.DOTALL | re.IGNORECASE
)


def _extract_text(response: Any) -> str:
    """Concatenate text fields from the response's content blocks.

    Mirrors the extraction logic in :mod:`primitives.generate` so tests that
    inject the same FakeClient shape work here without reshaping."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts)


def _parse_json_payload(text: str) -> dict[str, Any]:
    """Extract a JSON object from the LLM response.

    Tolerates fenced code blocks and a short prose preamble — the same
    tolerance policy ``primitives.generate`` uses."""
    stripped = text.strip()
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
                f"response contained a JSON fence but the body was invalid: {exc}"
            ) from exc

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = stripped[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"response did not contain valid JSON (parse error: {exc})"
            ) from exc

    raise ValueError("response did not contain a JSON object")


def _usage_tokens(response: Any) -> tuple[int, int]:
    """Read ``(input_tokens, output_tokens)`` off the response."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def ingest_transcript_text(
    session: Session,
    req: TranscriptIngestRequest,
) -> TranscriptIngestResult:
    """Full spec §4.1.2 pipeline. See module docstring for the step list.

    Raises:
        TranscriptIngestError: corpus not found, LLM response not parseable
            after retries, or schema validation exhausted.
        BudgetExceededError: from ``raise_if_over_budget`` — bubbles to the
            route for a 402 response.
    """
    # Sanity check the corpus exists up front — cheaper than finding out
    # after an LLM round-trip.
    corpus = session.exec(
        select(Corpus).where(Corpus.id == req.corpus_id)
    ).first()
    if corpus is None:
        raise TranscriptIngestError(
            f"Corpus {req.corpus_id!r} not found."
        )

    # 1. Content-addressed id.
    transcript_id = hashlib.sha256(req.raw_text.encode("utf-8")).hexdigest()

    # 2. Cache lookup — same raw_text in same corpus ⇒ no-op.
    existing = session.exec(
        select(Transcript).where(Transcript.id == transcript_id)
    ).first()
    if existing is not None and existing.corpus_id == req.corpus_id:
        segment_count = len(existing.segments or [])
        mentioned = _collect_mentioned(existing.segments or [])
        # Detach so callers can read after the session closes.
        session.expunge(existing)
        return TranscriptIngestResult(
            transcript=existing,
            segment_count=segment_count,
            mentioned_cases=mentioned,
            unresolved_mentions=[],
            cache_hit=True,
        )

    # 3. Budget gate before any LLM spend.
    raise_if_over_budget()

    # 4. Load known canonical case names for this corpus.
    known_case_names = load_known_case_names_for_corpus(session, req.corpus_id)

    # 5. Render the transcript_cleanup prompt + call Anthropic directly.
    cleaned_payload, input_tokens, output_tokens, model = _call_cleanup_llm(
        raw_text=req.raw_text,
        known_case_names=known_case_names,
        topic=req.topic,
    )

    # 6. Parse segments; run the fuzzy-resolver safety net against each
    #    segment's raw content to catch cases the LLM missed.
    cleaned_text = str(cleaned_payload.get("cleaned_text", ""))
    raw_segments = cleaned_payload.get("segments") or []
    llm_unresolved = list(cleaned_payload.get("unresolved_mentions") or [])

    # Collect all case names mentioned anywhere (dedup preserves the order
    # of first mention — stable outputs help test assertions).
    all_mentioned_cases: list[str] = []
    all_unresolved: list[str] = list(llm_unresolved)
    seen_mentioned: set[str] = set()

    enriched_segments: list[dict[str, Any]] = []
    for seg in raw_segments:
        if not isinstance(seg, dict):
            continue
        content = str(seg.get("content", ""))
        seg_mentioned = list(seg.get("mentioned_cases") or [])

        # Fuzzy-resolve the raw segment text. The LLM was instructed to do
        # this too, but it can miss mangled cases like "Shelly B Kramer" —
        # the resolver is deterministic and catches exactly that class of
        # bug (see fuzzy_resolver.py module docstring).
        resolved = resolve_case_names(content, known_case_names)
        for candidate in resolved.resolved:
            if candidate.matched_canonical not in seg_mentioned:
                seg_mentioned.append(candidate.matched_canonical)
        for raw_unresolved in resolved.unresolved:
            if raw_unresolved not in all_unresolved:
                all_unresolved.append(raw_unresolved)

        for name in seg_mentioned:
            if name not in seen_mentioned:
                seen_mentioned.add(name)
                all_mentioned_cases.append(name)

        seg_out = dict(seg)
        seg_out["mentioned_cases"] = seg_mentioned
        enriched_segments.append(seg_out)

    # 7. Persist Transcript + Segments.
    transcript = Transcript(
        id=transcript_id,
        corpus_id=req.corpus_id,
        source_type=TranscriptSourceType.TEXT,
        source_path=req.source_path,
        lecture_date=req.lecture_date,
        topic=req.topic,
        assignment_code=req.assignment_code,
        raw_text=req.raw_text,
        cleaned_text=cleaned_text,
    )
    session.add(transcript)
    session.flush()  # materialize FK for the segment rows

    for i, seg in enumerate(enriched_segments):
        speaker_str = str(seg.get("speaker", "unknown"))
        try:
            speaker_enum = Speaker(speaker_str)
        except ValueError:
            speaker_enum = Speaker.UNKNOWN
        row = TranscriptSegment(
            transcript_id=transcript.id,
            order_index=i,
            start_char=int(seg.get("start_char", 0)),
            end_char=int(seg.get("end_char", 0)),
            speaker=speaker_enum,
            content=str(seg.get("content", "")),
            mentioned_cases=list(seg.get("mentioned_cases") or []),
            mentioned_rules=list(seg.get("mentioned_rules") or []),
            mentioned_concepts=list(seg.get("mentioned_concepts") or []),
            sentiment_flags=list(seg.get("sentiment_flags") or []),
        )
        session.add(row)
    session.commit()
    session.refresh(transcript)

    # 8. Record CostEvent. Feature label matches the prompt name so the
    #    per-feature breakdown aggregation in ``costs.tracker`` groups
    #    cleanly.
    record_llm_call(
        model=model,
        provider=Provider.ANTHROPIC,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        feature=_FEATURE_LABEL,
        cached=False,
    )

    # Detach so callers (routes, tests) can read attributes after the
    # session closes. We refresh first so `.segments` is populated.
    session.expunge(transcript)

    return TranscriptIngestResult(
        transcript=transcript,
        segment_count=len(enriched_segments),
        mentioned_cases=all_mentioned_cases,
        unresolved_mentions=all_unresolved,
        cache_hit=False,
    )


def ingest_transcript_audio(
    _session: Session,
    _audio_path: Path,
    _metadata: dict[str, Any] | None = None,
) -> TranscriptIngestResult:
    """Spec §4.1.5 — stub for the audio path.

    Runs faster-whisper locally to produce text, then feeds into
    ``ingest_transcript_text``. Whisper isn't in the default install set —
    see ``pyproject.toml`` [audio] optional extra — and wiring this path is
    a follow-up slice.
    """
    raise NotImplementedError(
        "faster-whisper integration lands with the [audio] optional-deps "
        "path; see SPEC_QUESTIONS.md Q37"
    )


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------


def _call_cleanup_llm(
    *,
    raw_text: str,
    known_case_names: list[str],
    topic: str | None,
) -> tuple[dict[str, Any], int, int, str]:
    """Render the transcript_cleanup prompt and call Anthropic.

    Returns ``(parsed_json, input_tokens, output_tokens, model_used)``.

    The template's model/max_tokens/temperature defaults come from its
    frontmatter (``claude-haiku-4-5`` / 8000 / 0.1). We use those verbatim
    for Phase 4 — tuning the model per-user lives in ``config/models.toml``,
    not here.
    """
    template = load_template(_TEMPLATE_NAME)
    schema = load_output_schema(template)

    context: dict[str, Any] = {
        "raw_transcript": raw_text,
        "known_case_names": list(known_case_names),
        "lecture_topic": topic,
    }
    rendered = render_template(template, context)

    # Resolve model + generation params from the template frontmatter.
    model = _resolve_model(template)
    max_tokens = _resolve_int(template, "max_tokens", 8000)
    temperature = _resolve_float(template, "temperature", 0.1)

    creds = load_credentials()
    if creds.anthropic_api_key is None:
        raise TranscriptIngestError(
            "No Anthropic API key stored — Settings → API Key."
        )
    api_key = creds.anthropic_api_key.get_secret_value()

    from llm import create_message

    client = _make_client(api_key)
    system_prompt = f"Prompt template: {template.identifier}"

    try:
        response = create_message(
            client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": rendered}],
        )
    except anthropic.APIError as exc:
        raise TranscriptIngestError(
            f"Anthropic API call failed: {type(exc).__name__}: {exc}"
        ) from None
    except Exception as exc:
        raise TranscriptIngestError(
            f"Anthropic API call failed: {type(exc).__name__}: {exc}"
        ) from None

    input_tokens, output_tokens = _usage_tokens(response)
    raw_response_text = _extract_text(response)

    try:
        parsed = _parse_json_payload(raw_response_text)
    except ValueError as exc:
        # Mirror the diagnostic capture from `primitives.generate`: write the
        # raw response to ~/Library/Application Support/cLAWd/debug/ so a
        # parse failure in the bundled .app is recoverable. Without this we
        # only know "transcript_cleanup parse failed" — useless for fixing
        # whatever the LLM actually emitted.
        try:
            from datetime import UTC
            from paths import user_data_dir

            debug_dir = user_data_dir() / "debug" / "llm_parse_failures"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
            debug_path = (
                debug_dir
                / f"{stamp}_transcript_cleanup_v{template.version}.txt"
            )
            debug_path.write_text(
                f"# parse error: {exc}\n# template: {template.identifier}\n"
                f"# response length: {len(raw_response_text)} chars\n"
                f"# input tokens: {input_tokens}, output tokens: {output_tokens}\n\n"
                + raw_response_text,
                encoding="utf-8",
            )
        except Exception:
            pass
        raise TranscriptIngestError(
            f"transcript_cleanup response was not valid JSON: {exc}"
        ) from None

    # Coerce common Sonnet-vs-Haiku field-name drift before strict validation.
    # The schema names the segment text field `content`; Sonnet 4.6 has been
    # observed to emit `text` instead (the more common LLM convention). Both
    # carry identical semantics, so renaming preserves meaning. Apply to every
    # segment so a future template version that adds segments doesn't have
    # to re-implement the mapping.
    if isinstance(parsed, dict) and isinstance(parsed.get("segments"), list):
        for seg in parsed["segments"]:
            if isinstance(seg, dict) and "content" not in seg and "text" in seg:
                seg["content"] = seg.pop("text")

    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        # Capture the parsed-but-schema-failing response too — same dir as
        # parse failures, different prefix. We dump the *parsed* JSON
        # (post-coercion) plus the original raw bytes so we can see whether
        # the issue is a missed field-name drift, a structural drift, or
        # something at the JSON-source level the parser silently accepted.
        try:
            from datetime import UTC
            from paths import user_data_dir

            debug_dir = user_data_dir() / "debug" / "llm_parse_failures"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
            debug_path = (
                debug_dir
                / f"{stamp}_transcript_cleanup_v{template.version}_schema.txt"
            )
            debug_path.write_text(
                f"# schema error: {exc.message}\n"
                f"# path: {list(exc.absolute_path)}\n"
                f"# template: {template.identifier}\n"
                f"# response length: {len(raw_response_text)} chars\n"
                f"# input tokens: {input_tokens}, output tokens: {output_tokens}\n\n"
                f"# === parsed JSON (post-coercion) ===\n"
                + json.dumps(parsed, indent=2)[:50000]
                + "\n\n# === raw response ===\n"
                + raw_response_text,
                encoding="utf-8",
            )
        except Exception:
            pass
        raise TranscriptIngestError(
            f"transcript_cleanup response did not match schema: {exc.message}"
        ) from None

    return parsed, input_tokens, output_tokens, model


def _resolve_model(template: Any) -> str:
    """Get the effective model from template defaults, with a hardcoded
    fallback. We intentionally ignore ``config/models.toml`` here — the
    generate() primitive reads it, but feature-specific overrides for this
    one-off internal feature aren't worth the complexity.
    """
    default = template.model_defaults.get("model")
    if isinstance(default, str) and default:
        return default
    return "claude-haiku-4-5"


def _resolve_int(template: Any, key: str, default: int) -> int:
    val = template.model_defaults.get(key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _resolve_float(template: Any, key: str, default: float) -> float:
    val = template.model_defaults.get(key, default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _collect_mentioned(segments: list[TranscriptSegment]) -> list[str]:
    """Flatten per-segment mentioned_cases into a deduplicated, order-
    preserving list. Used on the cache-hit path so the return shape stays
    consistent with the fresh-ingest shape."""
    seen: set[str] = set()
    out: list[str] = []
    for seg in segments:
        for name in seg.mentioned_cases or []:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


__all__ = [
    "TranscriptIngestError",
    "TranscriptIngestRequest",
    "TranscriptIngestResult",
    "ingest_transcript_audio",
    "ingest_transcript_text",
    "set_client_factory",
]
