"""Pre-flight cost estimator (spec §7.7.5 C).

Feature orchestrators call `estimate_feature_cost(feature_name, inputs)` before
dispatching expensive batches. If the expected cost exceeds the user's
configured threshold (`config/models.toml[thresholds].preflight_cost_usd`,
default $0.50), the orchestrator raises `PreflightRequired` so the UI can
open the confirmation modal.

The estimates are intentionally approximate — the spec labels them
`"estimated"` and displays a ± range. Real costs are always tracked via
`CostEvent`; this module is just a forecast.

Assumption model (tunable via `config/estimator.toml`):

- **book_ingestion**: Marker's `--use_llm` pass fires the LLM on ~10% of
  printed pages with ~2000 input / 1000 output tokens each. The default model
  for this phase is Sonnet 4.6 (good cost/quality tradeoff for OCR cleanup).
  For the user's Property casebook (~1400 pages), estimate ≈ $3–$5.
- **case_brief**: single case opinion + ~6 notes + professor profile ≈
  ~1500 input tokens; output ~1200 tokens. At Opus 4.7 → ≈ $0.15–$0.30.
- **bulk_brief_generation**: linear in case count.
- **outline_regeneration**: proportional to artifact count across the corpus.
- **rubric_from_memo**: single memo ingestion → ~4000 input tokens, ~2000
  output. Opus 4.7 → ~$0.20–$0.25.

Ranges carry a ±30% margin by default; specific features may widen the band
(e.g., book ingestion is ±50% because LLM-call density varies).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from costs.pricing import get_pricing_book


@dataclass(frozen=True)
class CostEstimate:
    """Result of a pre-flight cost estimate. `label` is the UI-facing summary
    (e.g. `"~$2.40 (±30%)"`); raw decimals support threshold arithmetic."""

    low_usd: Decimal
    expected_usd: Decimal
    high_usd: Decimal
    label: str


class PreflightRequired(Exception):
    """Raised by a feature orchestrator when the estimate exceeds the user's
    threshold (spec §7.7.5 C). The UI catches this + shows the modal + re-
    dispatches only on explicit confirmation."""

    def __init__(self, feature: str, estimate: CostEstimate) -> None:
        super().__init__(
            f"Pre-flight confirmation required for {feature!r}: {estimate.label}"
        )
        self.feature = feature
        self.estimate = estimate


# ---------------------------------------------------------------------------
# Per-feature estimators
# ---------------------------------------------------------------------------


_CTX_PCT_DEFAULT = Decimal("0.30")   # ±30% band
_CTX_PCT_INGEST = Decimal("0.50")    # ±50% for ingestion (LLM density varies)


def _band(expected: Decimal, pct: Decimal = _CTX_PCT_DEFAULT) -> tuple[Decimal, Decimal, Decimal]:
    low = (expected * (Decimal("1") - pct)).quantize(Decimal("0.0001"))
    high = (expected * (Decimal("1") + pct)).quantize(Decimal("0.0001"))
    return low, expected.quantize(Decimal("0.0001")), high


def _format_label(expected: Decimal, pct: Decimal) -> str:
    pct_int = int(pct * 100)
    # Round to nearest cent for display.
    rounded = expected.quantize(Decimal("0.01"))
    return f"~${rounded} (±{pct_int}%)"


def _cost_of(model: str, input_tokens: int, output_tokens: int, *, provider: str = "anthropic") -> Decimal:
    book = get_pricing_book()
    _, _, total = book.compute_cost(provider, model, input_tokens, output_tokens)
    return total


def estimate_book_ingestion(inputs: dict[str, Any]) -> CostEstimate:
    """inputs: `{"page_count": int, "llm_call_density": float (optional, default 0.10)}`.

    Uses Sonnet 4.6 (a good OCR-cleanup model) by default; overridable via
    `inputs["model"]`.
    """
    pages = int(inputs.get("page_count", 0))
    density = Decimal(str(inputs.get("llm_call_density", 0.10)))
    input_tokens_per_call = int(inputs.get("input_tokens_per_call", 2000))
    output_tokens_per_call = int(inputs.get("output_tokens_per_call", 1000))
    model = str(inputs.get("model", "claude-sonnet-4-6"))

    n_calls_expected = Decimal(pages) * density
    cost_per_call = _cost_of(model, input_tokens_per_call, output_tokens_per_call)
    expected = n_calls_expected * cost_per_call

    low, exp, high = _band(expected, _CTX_PCT_INGEST)
    return CostEstimate(low, exp, high, _format_label(exp, _CTX_PCT_INGEST))


def estimate_case_brief(inputs: dict[str, Any]) -> CostEstimate:
    """inputs: `{"input_tokens": int (default ~1500), "output_tokens": int (default ~1200)}`.
    Default model Opus 4.7."""
    input_tokens = int(inputs.get("input_tokens", 1500))
    output_tokens = int(inputs.get("output_tokens", 1200))
    model = str(inputs.get("model", "claude-opus-4-7"))
    expected = _cost_of(model, input_tokens, output_tokens)
    low, exp, high = _band(expected)
    return CostEstimate(low, exp, high, _format_label(exp, _CTX_PCT_DEFAULT))


def estimate_bulk_brief_generation(inputs: dict[str, Any]) -> CostEstimate:
    """inputs: `{"case_count": int, "input_tokens_per_case": int, "output_tokens_per_case": int}`.
    Multiplies case_brief estimate by case_count."""
    cases = int(inputs.get("case_count", 0))
    per_case = estimate_case_brief(inputs)
    expected = per_case.expected_usd * Decimal(cases)
    low, exp, high = _band(expected)
    return CostEstimate(low, exp, high, _format_label(exp, _CTX_PCT_DEFAULT))


def estimate_outline_regeneration(inputs: dict[str, Any]) -> CostEstimate:
    """inputs: `{"artifact_count": int, "input_tokens_per_artifact": int (default 500)}`.
    One big Opus call with the corpus's briefs + flashcards as context."""
    count = int(inputs.get("artifact_count", 0))
    input_tokens_per_artifact = int(inputs.get("input_tokens_per_artifact", 500))
    output_tokens = int(inputs.get("output_tokens", 8000))
    model = str(inputs.get("model", "claude-opus-4-7"))
    total_input = count * input_tokens_per_artifact
    expected = _cost_of(model, total_input, output_tokens)
    low, exp, high = _band(expected)
    return CostEstimate(low, exp, high, _format_label(exp, _CTX_PCT_DEFAULT))


def estimate_rubric_from_memo(inputs: dict[str, Any]) -> CostEstimate:
    """inputs: `{"input_tokens": int (default 4000), "output_tokens": int (default 2000)}`.
    Opus 4.7 — high-stakes, one-time per memo."""
    input_tokens = int(inputs.get("input_tokens", 4000))
    output_tokens = int(inputs.get("output_tokens", 2000))
    model = str(inputs.get("model", "claude-opus-4-7"))
    expected = _cost_of(model, input_tokens, output_tokens)
    low, exp, high = _band(expected)
    return CostEstimate(low, exp, high, _format_label(exp, _CTX_PCT_DEFAULT))


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


_ESTIMATORS = {
    "book_ingestion": estimate_book_ingestion,
    "case_brief": estimate_case_brief,
    "bulk_brief_generation": estimate_bulk_brief_generation,
    "outline_regeneration": estimate_outline_regeneration,
    "rubric_from_memo": estimate_rubric_from_memo,
}


def estimate_feature_cost(feature_name: str, inputs: dict[str, Any]) -> CostEstimate:
    """Entry point called by feature orchestrators (spec §4.3, §7.7.5 C).

    Unknown features return a conservative "I don't know" estimate with a very
    wide band rather than raising — this lets new features surface with a
    flagged estimate rather than crashing the UI.
    """
    fn = _ESTIMATORS.get(feature_name)
    if fn is None:
        # Unknown feature: report an alarming-enough estimate to trigger the
        # preflight modal, with a label that says so.
        return CostEstimate(
            low_usd=Decimal("0.10"),
            expected_usd=Decimal("1.00"),
            high_usd=Decimal("10.00"),
            label=f"unknown feature {feature_name!r}: ~$1.00 (±900%)",
        )
    return fn(inputs)


__all__ = [
    "CostEstimate",
    "PreflightRequired",
    "estimate_book_ingestion",
    "estimate_bulk_brief_generation",
    "estimate_case_brief",
    "estimate_feature_cost",
    "estimate_outline_regeneration",
    "estimate_rubric_from_memo",
]
