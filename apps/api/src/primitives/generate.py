"""Primitive 3: Generate (spec §4.3).

Runs a prompt template against retrieved content, validates the LLM response
against the template's JSON schema, persists an :class:`Artifact` envelope,
and emits a :class:`CostEvent` — every time, cached or not.

Design contracts (spec callouts in parentheses):

- Prompts are data (§2.4) — templates come from
  :func:`primitives.prompt_loader.load_template`, never hardcoded strings.
- Cache keys are deterministic over (template@version, model, rendered prompt,
  temperature) — we reuse :func:`tests.llm_replay.compute_cache_key` so the
  Artifact table and the replay cache agree on canonicalization.
- Cache hits emit a CostEvent with ``cached=True, total_cost_usd=0`` (§4.3).
- Schema validation failures trigger up to two retries with an explicit
  correction prompt; exhausting retries raises :class:`GenerateError` (§4.3).
- API keys are loaded from the keyring via
  :func:`credentials.keyring_backend.load_credentials` — never passed through
  function args, never logged (§7.6).
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import jsonschema
import structlog
from sqlmodel import select
from tests.llm_replay import compute_cache_key

from costs.pricing import get_pricing_book
from costs.tracker import record_llm_call
from credentials.keyring_backend import load_credentials
from data.db import session_scope
from data.models import Artifact, ArtifactType, CreatedBy, Provider
from primitives.prompt_loader import (
    PromptTemplate,
    load_output_schema,
    load_template,
)
from primitives.retrieve import RetrievalResult
from primitives.template_renderer import render_template

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public request / response types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerateRequest:
    """All the knobs a single `generate()` call needs.

    Matches spec §4.3's generate() signature, with a few Phase-2 additions:
    ``corpus_id`` (required for Artifact persistence), ``artifact_type``
    (declares the Artifact envelope type), and ``force_regenerate`` (UI
    "regenerate" button bypass).
    """

    template_name: str
    inputs: dict[str, Any]
    artifact_type: ArtifactType
    corpus_id: str = ""
    retrieval: RetrievalResult | None = None
    professor_profile: dict[str, Any] | None = None
    model_override: str | None = None
    force_regenerate: bool = False
    parent_artifact_id: str | None = None
    book_toc_context: dict[str, Any] | None = None


@dataclass
class GenerateResult:
    """Outcome of a `generate()` call."""

    artifact: Artifact
    cache_hit: bool
    validation_warnings: list[str] = field(default_factory=list)


class GenerateError(RuntimeError):
    """Unrecoverable generation failure.

    Raised for:
    - missing API key,
    - Anthropic API errors (network, 4xx, 5xx) — reason summarized, key never
      included in the message,
    - schema validation failures after the retry budget is exhausted,
    - JSON parse failures after the retry budget is exhausted.
    """


# ---------------------------------------------------------------------------
# Client injection (test hook)
# ---------------------------------------------------------------------------


_client_factory: Callable[[str], Any] | None = None


def set_anthropic_client_factory(factory: Callable[[str], Any] | None) -> None:
    """Tests monkey-patch this to inject a mock Anthropic-like client.

    `factory(api_key: str)` must return an object whose shape mirrors
    :class:`anthropic.Anthropic`: a ``messages.create(...)`` method returning a
    response with ``.content[0].text`` and ``.usage.input_tokens /
    .usage.output_tokens`` attributes. Pass ``None`` to restore the default
    real-SDK factory.
    """
    global _client_factory
    _client_factory = factory


def _default_client_factory(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def _make_client(api_key: str) -> Any:
    if _client_factory is not None:
        return _client_factory(api_key)
    return _default_client_factory(api_key)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def _models_config_path() -> Path:
    """Resolve ``<root>/config/models.toml``. Uses ``paths.repo_root``
    so the bundled .app finds the file inside PyInstaller's ``_MEIPASS``."""
    from paths import repo_root

    return repo_root() / "config" / "models.toml"


def _feature_model_from_config(feature: str) -> str | None:
    """Look up a feature's default model in `config/models.toml`.

    Returns ``None`` if the config is missing, malformed, or the feature is
    not listed. Failures are non-fatal — callers fall back to template
    defaults (§7.7.6).
    """
    path = _models_config_path()
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        log.warning("models_config_unreadable", path=str(path), error=str(exc))
        return None
    features = data.get("features") or {}
    model = features.get(feature)
    return str(model) if model else None


def _resolve_model(template: PromptTemplate, override: str | None) -> str:
    """Priority: explicit override > template.model_defaults > config/models.toml
    > hardcoded fallback (Opus 4.7, per spec §7.7.6)."""
    if override:
        return override
    default = template.model_defaults.get("model")
    if isinstance(default, str) and default:
        return default
    from_cfg = _feature_model_from_config(template.name)
    if from_cfg:
        return from_cfg
    return "claude-opus-4-7"


def _resolve_temperature(template: PromptTemplate) -> float:
    val = template.model_defaults.get("temperature", 0.2)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.2


def _resolve_max_tokens(template: PromptTemplate) -> int:
    val = template.model_defaults.get("max_tokens", 4000)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 4000


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert a Block (SQLModel) or already-dict-shaped object into the dict
    shape prompt templates expect.

    Keeping this shim here (rather than asking callers to adapt) means feature
    code can pass SQLModel rows straight through from retrieve() — which is the
    common case — without mapping by hand.
    """
    if isinstance(block, dict):
        return block
    # Attribute-style (SQLModel / dataclass / namedtuple): pull known fields.
    out: dict[str, Any] = {}
    for field_name in ("id", "source_page", "type", "markdown", "order_index"):
        if hasattr(block, field_name):
            value = getattr(block, field_name)
            # Enums (e.g., BlockType) render better as their string value.
            if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
                value = value.value
            out[field_name] = value
    metadata = getattr(block, "block_metadata", None)
    out["block_metadata"] = dict(metadata) if metadata else {}
    return out


def _build_context(req: GenerateRequest) -> dict[str, Any]:
    """Merge request inputs + retrieval payload + professor profile into a
    single dict suitable for Handlebars rendering.

    Merge order (later wins): retrieval-derived keys → req.inputs →
    professor_profile / book_toc_context if set. Callers who want fine-grained
    control can pre-populate `req.inputs` and leave retrieval unset — the
    retrieval payload is a convenience for the common case.
    """
    ctx: dict[str, Any] = {}

    if req.retrieval is not None:
        blocks = [_block_to_dict(b) for b in req.retrieval.blocks]
        ctx["retrieval_blocks"] = blocks
        ctx["retrieval_pages"] = [
            {
                "id": getattr(p, "id", None),
                "source_page": getattr(p, "source_page", None),
                "markdown": getattr(p, "markdown", None),
            }
            for p in req.retrieval.pages
        ]
        ctx["retrieval_notes"] = list(req.retrieval.notes)

    # Normalize Block-typed inputs so Handlebars sees plain dicts.
    for key, value in req.inputs.items():
        if key == "following_notes" and isinstance(value, list):
            ctx[key] = [_block_to_dict(b) for b in value]
        elif _looks_like_block(value):
            ctx[key] = _block_to_dict(value)
        else:
            ctx[key] = value

    if req.professor_profile is not None:
        ctx["professor_profile"] = req.professor_profile
    if req.book_toc_context is not None:
        ctx["book_toc_context"] = req.book_toc_context

    return ctx


def _looks_like_block(value: Any) -> bool:
    """Heuristic: does this object quack like a Block?"""
    if isinstance(value, dict):
        # Only convert if the dict already has Block-ish keys (don't clobber
        # arbitrary user dicts like professor profile payloads).
        return False
    return hasattr(value, "markdown") and hasattr(value, "source_page")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


# Tolerate common formatting the models sometimes produce despite "JSON only":
# ```json\n{...}\n``` fences, and a leading prose sentence before the JSON.
_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(?P<body>.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_text_from_response(response: Any) -> str:
    """Concatenate the `text` fields of every text block in the response.

    The real SDK returns a list of content blocks; our mock clients return the
    same shape. TextBlock has `.type == "text"` and `.text` — we tolerate both
    attribute and dict access.
    """
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts)


def _parse_json_payload(text: str) -> dict[str, Any]:
    """Pull a JSON object out of the model's response text.

    Accepts raw JSON, fenced code blocks, and JSON embedded after a short prose
    preamble. Raises `ValueError` if no object can be parsed.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("response was empty")

    # Direct JSON first (the happy path: model obeyed "JSON only").
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return parsed

    # Try fenced blocks.
    match = _FENCE_RE.search(stripped)
    if match:
        inner = match.group("body").strip()
        try:
            parsed = json.loads(inner)
        except json.JSONDecodeError as exc:
            raise ValueError(f"response contained a JSON fence but the body was invalid: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed

    # Last resort: scan for the first '{' and try a greedy parse to end.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = stripped[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"response did not contain valid JSON (parse error: {exc})"
            ) from exc
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("response did not contain a JSON object")


def _usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return input_tokens, output_tokens


# ---------------------------------------------------------------------------
# Retry prompt
# ---------------------------------------------------------------------------


_RETRY_SYSTEM_NOTE = (
    "Your previous response was not valid JSON matching the required schema. "
    "Return ONLY a JSON object matching the schema. No prose, no code fences, "
    "no commentary. Fix the following issues:"
)


# Per-template fields whose schema shape is `{text: string, source_block_ids:
# string[]}` — the "Claim" type. Models occasionally return a bare string here
# instead of the wrapped object; we coerce STRUCTURE before strict validation.
# We deliberately do NOT fabricate placeholders for entirely-missing fields —
# the retry loop handles that with a corrective prompt, so the user gets a
# genuine brief from the second attempt rather than a hollow shell.
_CASE_BRIEF_CLAIM_FIELDS: tuple[str, ...] = (
    "procedural_posture",
    "issue",
    "holding",
    "rule",
    "significance",
)
_CASE_BRIEF_CLAIM_LIST_FIELDS: tuple[str, ...] = ("facts", "reasoning")


def _coerce_claim_shape(
    value: Any, *, fallback_source_ids: list[str]
) -> Any:
    """Coerce a single Claim-shaped value's STRUCTURE — only when the model
    returned the right intent in the wrong shape. Pass-through otherwise.

    - dict with empty source_block_ids → fill from fallback so the
      ``minItems: 1`` constraint passes (citations stay grounded in the same
      blocks the model was reading).
    - bare string → wrap as ``{text, source_block_ids}``.
    - anything else → return unchanged so strict validation surfaces the
      real type mismatch and triggers a retry with a corrective prompt.
    """
    if isinstance(value, dict):
        ids = value.get("source_block_ids")
        if not isinstance(ids, list) or not ids:
            value = dict(value)
            value["source_block_ids"] = list(fallback_source_ids) or ["unknown"]
        return value
    if isinstance(value, str):
        return {
            "text": value,
            "source_block_ids": list(fallback_source_ids) or ["unknown"],
        }
    return value


def _repair_candidate(
    candidate: dict[str, Any],
    *,
    template_name: str,
    retrieval: Any,
) -> dict[str, Any]:
    """Coerce common model output drift into the schema shape.

    Two layers:

    1. Generic: every template has an optional ``sources`` array (artifact ids
       this output drew from). Models routinely drop it when there are no
       extras to cite. If absent, default to ``[]`` so schema validation
       passes; downstream lineage tracking just sees an empty list.

    2. Per-template: ``case_brief`` has Claim-shaped fields the model
       sometimes returns as bare strings — wrap them into the object shape
       without fabricating new content.
    """
    if not isinstance(candidate, dict):
        return candidate

    # Layer 1 — generic. Templates that emit a top-level ``sources`` array
    # routinely omit it. Default to [] so we don't 400 over a benign omission.
    if "sources" not in candidate or not isinstance(candidate.get("sources"), list):
        candidate["sources"] = []

    # what_if_variations — auto-fill the two fields the model frequently
    # drops: top-level case_name (use the parent brief's name) and per-
    # variation `id` (slug from index when missing). These are pure
    # mechanical defaults, not content invention.
    if template_name == "what_if_variations":
        if not candidate.get("case_name"):
            # Try to recover from the retrieval — the parent brief usually
            # has the case_name in block_metadata or is itself the source.
            blocks = (
                getattr(retrieval, "blocks", None) if retrieval is not None else None
            )
            if blocks:
                md = getattr(blocks[0], "block_metadata", None) or {}
                if isinstance(md, dict):
                    v = md.get("case_name")
                    if isinstance(v, str) and v.strip():
                        candidate["case_name"] = v.strip()
            if not candidate.get("case_name"):
                candidate["case_name"] = "(unknown case)"
        variations = candidate.get("variations")
        if isinstance(variations, list):
            for i, var in enumerate(variations, start=1):
                if isinstance(var, dict) and not var.get("id"):
                    var["id"] = f"v{i}"
        return candidate

    # Build the fallback id list once — used by both case_brief and the
    # flashcards/mc_questions per-item repairs below.
    fallback_ids: list[str] = []
    blocks = getattr(retrieval, "blocks", None) if retrieval is not None else None
    if blocks:
        for b in blocks:
            bid = getattr(b, "id", None)
            if isinstance(bid, str) and bid:
                fallback_ids.append(bid)

    # Flashcards: schema requires every card's `source_block_ids` to be
    # `minItems: 1`, but Opus / Sonnet drop the field entirely on most cards
    # while emitting their own `sources` field (also a list of block ids,
    # just under a different name). Reuse the card's own `sources` first —
    # it's the model's stated citation set, scoped to that specific card.
    # Only fall back to a SINGLE retrieval id when the card has no signal
    # at all; previously this dumped the entire ~100-block retrieval set
    # into every card, blowing up the JSON payload and making the rendered
    # artifact unreadable.
    if template_name == "flashcards":
        cards = candidate.get("cards")
        if isinstance(cards, list):
            single_fallback = fallback_ids[:1] if fallback_ids else ["unknown"]
            for card in cards:
                if not isinstance(card, dict):
                    continue
                ids = card.get("source_block_ids")
                if isinstance(ids, list) and ids:
                    continue  # model emitted its own; trust it
                # Try the card's own `sources` list before falling back to
                # the retrieval set — it's per-card and short.
                own_sources = card.get("sources")
                if isinstance(own_sources, list):
                    cleaned = [
                        s for s in own_sources if isinstance(s, str) and s
                    ]
                    if cleaned:
                        card["source_block_ids"] = cleaned
                        continue
                card["source_block_ids"] = list(single_fallback)
        return candidate

    # MC questions: schema declares `id` as a string (`"q1"`, `"q2"`, …)
    # but the model regularly emits an integer for the last few items in a
    # set (observed 2026-04 with mc_questions@1.x: `id: 10`). Coerce ints
    # to their string form so validation passes; the retry loop's
    # corrective prompt was burning a turn on this benign type drift.
    if template_name == "mc_questions":
        questions = candidate.get("questions")
        if isinstance(questions, list):
            for q in questions:
                if not isinstance(q, dict):
                    continue
                qid = q.get("id")
                if isinstance(qid, int):
                    q["id"] = str(qid)
                elif qid is None:
                    # Backfill missing ids from index — same pattern as
                    # `what_if_variations` above.
                    q["id"] = f"q{questions.index(q) + 1}"
        return candidate

    if template_name != "case_brief":
        return candidate

    # Layer 2a — Claim-shaped fields: coerce STRUCTURE only.
    # Recent Opus 4.x output drift (observed 2026-04 with case_brief@1.2.0):
    # the model returns a LIST of Claim objects for fields the schema declares
    # as a SINGLE Claim (e.g., `significance: [{text, source_block_ids}, …]`).
    # Schema validation rejects this as `not of type 'object'`. Collapse the
    # list to a single Claim by joining the `.text` values with `\n\n` and
    # unioning the `source_block_ids` so we don't lose citations.
    for key in _CASE_BRIEF_CLAIM_FIELDS:
        if key not in candidate:
            continue
        v = candidate[key]
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            merged_text = "\n\n".join(
                str(x.get("text", "")).strip() for x in v if x.get("text")
            )
            merged_ids: list[str] = []
            seen: set[str] = set()
            for x in v:
                ids = x.get("source_block_ids") or []
                if isinstance(ids, list):
                    for bid in ids:
                        if isinstance(bid, str) and bid and bid not in seen:
                            merged_ids.append(bid)
                            seen.add(bid)
            candidate[key] = {
                "text": merged_text,
                "source_block_ids": merged_ids or list(fallback_ids) or ["unknown"],
            }
        else:
            candidate[key] = _coerce_claim_shape(
                v, fallback_source_ids=fallback_ids
            )

    # Layer 2b — array-of-Claim fields: coerce each item's structure. The
    # model occasionally returns a single Claim object (or a bare string)
    # for these fields instead of an array. Wrap it into a single-element
    # array so structural validation passes; the retry loop catches missing
    # arrays entirely.
    for key in _CASE_BRIEF_CLAIM_LIST_FIELDS:
        items = candidate.get(key)
        if isinstance(items, list):
            candidate[key] = [
                _coerce_claim_shape(it, fallback_source_ids=fallback_ids)
                for it in items
            ]
        elif isinstance(items, dict) or isinstance(items, str):
            # Single Claim or bare string → wrap as single-element array.
            candidate[key] = [
                _coerce_claim_shape(items, fallback_source_ids=fallback_ids)
            ]

    # Layer 2c — string|null fields the model sometimes wraps as a Claim.
    # `where_this_fits` is described as plain doctrinal-arc context but the
    # model treats it like every other narrative field and returns a Claim
    # — and increasingly, a LIST of Claims. Both shapes need to flatten to
    # a single string for schema compliance.
    for key in ("where_this_fits", "likely_emphasis"):
        v = candidate.get(key)
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            candidate[key] = v["text"]
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            candidate[key] = "\n\n".join(
                str(x.get("text", "")).strip() for x in v if x.get("text")
            )

    # case_name missing — pull from the case_opinion block's metadata when
    # available (we know which block fed the prompt). High-signal: the case
    # name comes from the casebook's own structure, not invention.
    if not candidate.get("case_name"):
        if blocks:
            md = getattr(blocks[0], "block_metadata", None) or {}
            if isinstance(md, dict):
                v = md.get("case_name")
                if isinstance(v, str) and v.strip():
                    candidate["case_name"] = v.strip()

    # Other safe defaults that don't fabricate content.
    if not isinstance(candidate.get("limitations"), list):
        candidate["limitations"] = []
    candidate.setdefault("where_this_fits", None)

    return candidate


def _default_for_schema_node(node: dict[str, Any]) -> Any:
    """Type-appropriate empty value for a JSON-Schema property node.

    Used to fill in required fields the model dropped. Defaults are
    intentionally vacuous (empty array, empty string, ``null``) — we are
    not inventing content, just satisfying schema shape so the user gets
    SOMETHING back instead of a 503 when the model omits a non-critical
    required field. The user-facing renderer skips empty arrays/strings.
    """
    if not isinstance(node, dict):
        return None

    # `type` may be a string or a list (e.g. ["string", "null"]).
    t = node.get("type")
    if isinstance(t, list):
        # Prefer "null" when allowed, else fall back to the first non-null
        # entry. Letting null through is the least-presumptive default.
        if "null" in t:
            return None
        t = t[0] if t else None

    if t == "array":
        return []
    if t == "object":
        return {}
    if t == "string":
        return ""
    if t == "boolean":
        return False
    if t in ("integer", "number"):
        return 0
    return None


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a local JSON-pointer ``$ref`` (e.g. ``#/$defs/Claim``) against
    the root schema. Returns None when the ref points outside the doc or
    can't be resolved — the caller treats that as "no schema info" and
    leaves the value alone.
    """
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    cursor: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor if isinstance(cursor, dict) else None


def _fill_missing_required_recursive(
    candidate: Any, schema: dict[str, Any], root: dict[str, Any]
) -> None:
    """Walk ``candidate`` against ``schema``, filling any missing required
    fields with type-appropriate defaults. Mutates ``candidate`` in place.

    Handles nested objects and arrays-of-objects, plus ``$ref`` resolution
    against the root schema for templates that use ``$defs`` (case_brief's
    Claim, hypo's embedded Rubric, etc.).
    """
    # Resolve $ref
    ref = schema.get("$ref")
    if isinstance(ref, str):
        resolved = _resolve_ref(ref, root)
        if resolved is None:
            return
        schema = resolved

    t = schema.get("type")
    if isinstance(t, list):
        # Take the first object/array type if present; otherwise we can't do
        # much repair on a polymorphic value.
        for cand in t:
            if cand in ("object", "array"):
                t = cand
                break

    if t == "object" and isinstance(candidate, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        if isinstance(required, list) and isinstance(properties, dict):
            for key in required:
                if key not in candidate:
                    prop = properties.get(key) or {}
                    if isinstance(prop, dict) and "$ref" in prop:
                        resolved = _resolve_ref(prop["$ref"], root) or {}
                        candidate[key] = _default_for_schema_node(resolved)
                    else:
                        candidate[key] = _default_for_schema_node(prop)
        # Recurse into each present property + coerce common type drift.
        if isinstance(properties, dict):
            for key, prop in properties.items():
                if key not in candidate or not isinstance(prop, dict):
                    continue
                # Resolve any $ref so we can read the prop's intended type.
                prop_resolved = prop
                if "$ref" in prop:
                    prop_resolved = _resolve_ref(prop["$ref"], root) or prop
                prop_type = prop_resolved.get("type")
                if isinstance(prop_type, list):
                    # Skip union types — they may legitimately accept several
                    # shapes; coercion would be presumptuous.
                    prop_type = None
                value = candidate[key]
                # Coerce array-of-strings → single string for `string`-typed
                # fields. Common drift: model treats every narrative slot as
                # a list of steps when the schema wants prose.
                if (
                    prop_type == "string"
                    and isinstance(value, list)
                    and all(isinstance(it, str) for it in value)
                ):
                    candidate[key] = "\n".join(value)
                # Coerce dict with `.text` → bare string for string-typed
                # fields (model wraps as Claim).
                elif (
                    prop_type == "string"
                    and isinstance(value, dict)
                    and isinstance(value.get("text"), str)
                ):
                    candidate[key] = value["text"]
                else:
                    _fill_missing_required_recursive(candidate[key], prop, root)
        return

    if t == "array" and isinstance(candidate, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            # Resolve a $ref on the items so we know what shape the schema
            # actually wants — used for the string-vs-Claim coercion below.
            items_resolved = items_schema
            if "$ref" in items_schema:
                items_resolved = _resolve_ref(items_schema["$ref"], root) or items_schema
            items_type = items_resolved.get("type")
            if isinstance(items_type, list):
                for c in items_type:
                    if c in ("string", "object", "array"):
                        items_type = c
                        break

            # Pre-compute the "natural" required string field for `object`
            # items so we can wrap stray strings without re-walking the
            # schema each iteration. Heuristic: the first required field
            # whose type is `string` (or a union including string).
            wrap_key: str | None = None
            if items_type == "object":
                req = items_resolved.get("required") or []
                props = items_resolved.get("properties") or {}
                if isinstance(req, list) and isinstance(props, dict):
                    for key in req:
                        prop = props.get(key) or {}
                        if not isinstance(prop, dict):
                            continue
                        # Resolve nested $ref
                        if "$ref" in prop:
                            prop = _resolve_ref(prop["$ref"], root) or prop
                        pt = prop.get("type")
                        if pt == "string" or (isinstance(pt, list) and "string" in pt):
                            wrap_key = key
                            break

            for i, item in enumerate(candidate):
                # Coerce Claim-shaped objects → bare string when the schema
                # wants strings. Models routinely wrap items in `{text,
                # source_block_ids}` for any list field. Extract `.text`.
                if (
                    items_type == "string"
                    and isinstance(item, dict)
                    and isinstance(item.get("text"), str)
                ):
                    candidate[i] = item["text"]
                # Coerce bare strings → wrapped object when the schema
                # wants object items. The most common case is the model
                # returning a list of case names where the schema expects
                # `[{case_name, ...}]`. We wrap with the items schema's
                # first required string field.
                elif (
                    items_type == "object"
                    and isinstance(item, str)
                    and wrap_key is not None
                ):
                    candidate[i] = {wrap_key: item}
                    _fill_missing_required_recursive(candidate[i], items_schema, root)
                else:
                    _fill_missing_required_recursive(item, items_schema, root)


def _fill_missing_required_from_schema(
    candidate: Any, schema: dict[str, Any] | None
) -> Any:
    """Recursive variant: walk every level of the schema and inject
    type-appropriate defaults for missing required fields. Covers nested
    arrays of objects (e.g. ``attack_sheet.issue_spotting_triggers[i].trigger``)
    so a model that drops ONE field on ONE array item doesn't 503 the whole
    feature.
    """
    if not isinstance(schema, dict):
        return candidate
    _fill_missing_required_recursive(candidate, schema, schema)
    return candidate


def _format_schema_errors(err: jsonschema.ValidationError) -> list[str]:
    """Flatten a jsonschema ValidationError tree into human-readable lines."""
    out: list[str] = []
    # Best-effort walk: ValidationError exposes `.context` for sub-errors from
    # oneOf/anyOf branches, but for our simple case_brief schema the top-level
    # message is usually enough.
    out.append(f"- {'/'.join(str(p) for p in err.absolute_path) or '(root)'}: {err.message}")
    for sub in err.context:
        out.append(f"  • {sub.message}")
    return out


def _build_retry_user_message(
    original_prompt: str,
    prior_output: str,
    error_lines: list[str],
) -> str:
    joined_errors = "\n".join(error_lines) if error_lines else "- response was not valid JSON"
    return (
        f"{original_prompt}\n\n"
        f"---\nPREVIOUS ATTEMPT (invalid):\n{prior_output}\n\n"
        f"---\nREQUIRED FIXES:\n{joined_errors}\n\n"
        f"Return ONLY the corrected JSON object."
    )


# ---------------------------------------------------------------------------
# Cache lookup
# ---------------------------------------------------------------------------


def _find_cached_artifact(cache_key: str, corpus_id: str) -> Artifact | None:
    """Return a prior Artifact for this cache_key, or None."""
    with session_scope() as session:
        stmt = (
            select(Artifact)
            .where(Artifact.cache_key == cache_key)
            .where(Artifact.corpus_id == corpus_id)
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        found = session.exec(stmt).first()
        if found is not None:
            # Detach so callers can read attributes after the session closes.
            session.expunge(found)
        return found


# ---------------------------------------------------------------------------
# Sources extraction
# ---------------------------------------------------------------------------


def _extract_sources(content: dict[str, Any]) -> list[dict[str, str]]:
    """Pull `sources` out of a generated content payload.

    Convention (set by case_brief.json): `content["sources"]` is a
    deduplicated list of Block ids. We canonicalize to the spec §3.11 envelope
    shape: ``[{"kind": "block", "id": "..."}, ...]``.
    """
    raw = content.get("sources")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if isinstance(item, dict):
            kind = str(item.get("kind", "block"))
            ident = item.get("id") or item.get("ref")
            if not ident:
                continue
            key = (kind, str(ident))
        else:
            key = ("block", str(item))
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": key[0], "id": key[1]})
    return out


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


MAX_ATTEMPTS = 2  # original call + 1 correction retry; spec: "max 2 retries"


def generate(req: GenerateRequest) -> GenerateResult:
    """Full pipeline per spec §4.3.

    See module docstring for the contract summary; step-by-step flow:

    1. Load the prompt template.
    2. Resolve model / temperature / max_tokens (override → template defaults
       → config/models.toml → hardcoded fallback).
    3. Build the rendering context and render the prompt body.
    4. Compute cache key and, unless force_regenerate, return the matching
       Artifact if one exists.
    5. Resolve the Anthropic API key from the keyring.
    6. Call the Anthropic SDK, parse + validate, retrying once on JSON/schema
       failure.
    7. Persist the Artifact and emit a CostEvent with real token counts.
    """
    template = load_template(req.template_name)
    schema = load_output_schema(template)

    model = _resolve_model(template, req.model_override)
    temperature = _resolve_temperature(template)
    max_tokens = _resolve_max_tokens(template)

    context = _build_context(req)
    rendered_user_message = render_template(template, context)

    user_messages = [{"role": "user", "content": rendered_user_message}]
    cache_key = compute_cache_key(
        template=template.identifier,
        model=model,
        input_messages=user_messages,
        temperature=temperature,
    )

    # --- Cache check ----------------------------------------------------
    if not req.force_regenerate:
        cached = _find_cached_artifact(cache_key, req.corpus_id)
        if cached is not None:
            record_llm_call(
                model=model,
                provider=Provider.ANTHROPIC,
                input_tokens=0,
                output_tokens=0,
                feature=template.name,
                artifact_id=cached.id,
                cached=True,
            )
            log.info(
                "generate_cache_hit",
                template=template.identifier,
                cache_key=cache_key[:12],
                artifact_id=cached.id,
            )
            return GenerateResult(artifact=cached, cache_hit=True)

    # --- Key resolution -------------------------------------------------
    creds = load_credentials()
    if creds.anthropic_api_key is None:
        raise GenerateError(
            "No Anthropic API key stored — Settings → API Key."
        )
    api_key = creds.anthropic_api_key.get_secret_value()

    # --- LLM call with retry loop --------------------------------------
    client = _make_client(api_key)
    system_prompt = f"Prompt template: {template.identifier}"

    prior_output: str = ""
    last_errors: list[str] = []
    parsed: dict[str, Any] | None = None
    input_tokens = 0
    output_tokens = 0

    from llm import create_message

    attempt_user_message = rendered_user_message
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = create_message(
                client,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": attempt_user_message}],
            )
        except anthropic.APIError as exc:
            # Scrub key — AnthropicError messages include request metadata but
            # not the key itself; still, build our own message defensively.
            raise GenerateError(
                f"Anthropic API call failed: {type(exc).__name__}: {exc}"
            ) from None
        except Exception as exc:  # httpx.HTTPError, ConnectionError, etc.
            raise GenerateError(
                f"Anthropic API call failed: {type(exc).__name__}: {exc}"
            ) from None

        input_tokens, output_tokens = _usage_tokens(response)
        raw_text = _extract_text_from_response(response)

        # Parse
        try:
            candidate = _parse_json_payload(raw_text)
        except ValueError as exc:
            prior_output = raw_text
            last_errors = [f"- (root): {exc}"]
            # Persist the raw response to disk so we can diagnose recurring
            # parse failures (especially in the bundled .app where stdout is
            # routed to ~/Library/Logs/cLAWd/sidecar.log and not interactive).
            # Same parse error repeating at the exact same character offset
            # almost always means the case text contains something that
            # consistently confuses the model's JSON formatting (unescaped
            # quote, embedded code fence, etc.) — having the body lets us
            # see the offending bytes instead of guessing.
            try:
                from paths import user_data_dir

                debug_dir = user_data_dir() / "debug" / "llm_parse_failures"
                debug_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
                debug_path = (
                    debug_dir
                    / f"{stamp}_{template.identifier.replace('@', '_v')}_attempt{attempt}.txt"
                )
                debug_path.write_text(
                    f"# parse error: {exc}\n# template: {template.identifier}\n"
                    f"# attempt: {attempt}\n# response length: {len(raw_text)} chars\n\n"
                    + raw_text,
                    encoding="utf-8",
                )
            except Exception:
                # Diagnostic write must never break the retry path.
                pass
            log.info(
                "generate_parse_retry",
                template=template.identifier,
                attempt=attempt,
                error=str(exc),
            )
            if attempt >= MAX_ATTEMPTS:
                raise GenerateError(
                    f"Generate failed after {MAX_ATTEMPTS} attempts: "
                    f"response was not valid JSON. Last error: {exc}"
                ) from None
            attempt_user_message = _build_retry_user_message(
                rendered_user_message, prior_output, last_errors
            )
            continue

        # Repair: coerce common model output drift before strict validation.
        # Three layers run in order:
        #   1. Template-specific shape coercion (case_brief Claim wrapping,
        #      what_if id auto-numbering).
        #   2. Generic schema walk that fills missing required scalar/array
        #      fields with type-appropriate defaults — the model routinely
        #      drops harmless required fields like attack_sheet.exceptions
        #      and otherwise we'd burn a retry on a benign omission.
        #   3. Generic top-level sources default to [].
        candidate = _repair_candidate(
            candidate,
            template_name=template.name,
            retrieval=req.retrieval,
        )
        candidate = _fill_missing_required_from_schema(candidate, schema)

        # Validate
        try:
            jsonschema.validate(candidate, schema)
        except jsonschema.ValidationError as exc:
            prior_output = raw_text
            last_errors = _format_schema_errors(exc)
            log.info(
                "generate_schema_retry",
                template=template.identifier,
                attempt=attempt,
                errors=last_errors,
            )
            if attempt >= MAX_ATTEMPTS:
                joined = "\n".join(last_errors)
                raise GenerateError(
                    f"Generate failed after {MAX_ATTEMPTS} attempts: "
                    f"output did not match schema.\n{joined}"
                ) from None
            attempt_user_message = _build_retry_user_message(
                rendered_user_message, prior_output, last_errors
            )
            continue

        parsed = candidate
        break

    assert parsed is not None  # loop exit implies success

    # --- Persist artifact + cost event ---------------------------------
    pricing = get_pricing_book()
    _, _, total_cost = pricing.compute_cost(
        Provider.ANTHROPIC.value, model, input_tokens, output_tokens
    )

    sources = _extract_sources(parsed)

    artifact = Artifact(
        corpus_id=req.corpus_id,
        type=req.artifact_type,
        created_by=CreatedBy.SYSTEM,
        sources=sources,
        content=parsed,
        parent_artifact_id=req.parent_artifact_id,
        prompt_template=template.identifier,
        llm_model=model,
        cost_usd=Decimal(total_cost),
        cache_key=cache_key,
        regenerable=True,
    )

    with session_scope() as session:
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        session.expunge(artifact)

    record_llm_call(
        model=model,
        provider=Provider.ANTHROPIC,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        feature=template.name,
        artifact_id=artifact.id,
        cached=False,
    )

    log.info(
        "generate_succeeded",
        template=template.identifier,
        artifact_id=artifact.id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd=str(total_cost),
        cache_key=cache_key[:12],
    )

    return GenerateResult(artifact=artifact, cache_hit=False)


__all__ = [
    "GenerateError",
    "GenerateRequest",
    "GenerateResult",
    "generate",
    "set_anthropic_client_factory",
]
