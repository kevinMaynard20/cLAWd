"""Per-call cost computation driven by `config/pricing.toml` (spec §7.7.4).

Responsibilities:
- Parse `config/pricing.toml` once at startup into typed `ModelPricing` entries.
- Serve `ModelPricing` lookups by `(provider, model)` with a conservative
  fallback for anything we haven't explicitly priced.
- Multiply token counts by the per-mtok rate and return `Decimal` USD amounts.

Spec §7.7.4: "If the file is missing or malformed, the app surfaces a warning
and defaults to conservative (high) estimates to avoid under-reporting."

All money math is `Decimal`. Floats are never used for currency.
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """Rates for a specific (provider, model) pair, in USD per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


# Conservative ceiling used when (a) the pricing file is missing/malformed, or
# (b) a specific (provider, model) isn't listed. Better to over-report than
# under-report (spec §7.7.4).
_HARDCODED_CONSERVATIVE = ModelPricing(
    input_per_mtok=Decimal("20.00"),
    output_per_mtok=Decimal("100.00"),
)


_MTOK = Decimal("1000000")


# ---------------------------------------------------------------------------
# PricingBook
# ---------------------------------------------------------------------------


class PricingBook:
    """In-memory rates table. Build with `PricingBook.load()`.

    Stores prices keyed by `(provider, model)` (both lowercased). Unknown models
    fall back to `conservative_default`; a WARN is logged once per key so the
    log isn't spammed on every call.
    """

    def __init__(
        self,
        prices: dict[tuple[str, str], ModelPricing],
        conservative_default: ModelPricing,
    ) -> None:
        self._prices: dict[tuple[str, str], ModelPricing] = dict(prices)
        self.conservative_default: ModelPricing = conservative_default
        self._warned_unknown: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    # -- loading --------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> PricingBook:
        """Load pricing from a TOML file.

        If `path` is None, walk up from this module's directory looking for
        `spec.md` (the repo root marker used elsewhere in the codebase) and
        read `config/pricing.toml` from there.

        On any failure (missing file, malformed TOML, unreadable contents),
        emit a WARN and return a book containing only the hardcoded
        conservative default — callers still get a usable object.
        """
        resolved_path = path if path is not None else _default_pricing_path()

        try:
            raw = resolved_path.read_bytes()
        except FileNotFoundError:
            log.warning(
                "pricing_config_missing",
                path=str(resolved_path),
                fallback="conservative_default_all_models",
            )
            return cls({}, _HARDCODED_CONSERVATIVE)
        except OSError as exc:
            log.warning(
                "pricing_config_unreadable",
                path=str(resolved_path),
                error=str(exc),
                fallback="conservative_default_all_models",
            )
            return cls({}, _HARDCODED_CONSERVATIVE)

        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            log.warning(
                "pricing_config_malformed",
                path=str(resolved_path),
                error=str(exc),
                fallback="conservative_default_all_models",
            )
            return cls({}, _HARDCODED_CONSERVATIVE)

        try:
            prices, conservative = _parse_pricing_document(data)
        except (KeyError, TypeError, ValueError) as exc:
            # TOML was parseable but the shape is wrong (e.g., missing keys,
            # non-numeric rates). Same fallback policy.
            log.warning(
                "pricing_config_invalid_shape",
                path=str(resolved_path),
                error=str(exc),
                fallback="conservative_default_all_models",
            )
            return cls({}, _HARDCODED_CONSERVATIVE)

        log.info(
            "pricing_config_loaded",
            path=str(resolved_path),
            model_count=len(prices),
        )
        return cls(prices, conservative)

    # -- queries --------------------------------------------------------

    def get(self, provider: str, model: str) -> ModelPricing:
        """Return the rate for this `(provider, model)`, or the conservative
        default with a one-time WARN if the pair isn't priced."""
        key = (provider.lower(), model.lower())
        found = self._prices.get(key)
        if found is not None:
            return found

        # Log once per unknown key per process. Second+ lookups are silent.
        with self._lock:
            if key not in self._warned_unknown:
                self._warned_unknown.add(key)
                log.warning(
                    "pricing_unknown_model",
                    provider=provider,
                    model=model,
                    fallback_input_per_mtok=str(self.conservative_default.input_per_mtok),
                    fallback_output_per_mtok=str(self.conservative_default.output_per_mtok),
                )
        return self.conservative_default

    def compute_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return `(input_cost, output_cost, total_cost)` in USD.

        Rates are stored per million tokens; we divide by 1e6 as Decimal so no
        float ever enters the calculation.
        """
        pricing = self.get(provider, model)
        input_cost = pricing.input_per_mtok * Decimal(int(input_tokens)) / _MTOK
        output_cost = pricing.output_per_mtok * Decimal(int(output_tokens)) / _MTOK
        total = input_cost + output_cost
        return input_cost, output_cost, total


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def _coerce_rate(value: object, *, field: str) -> Decimal:
    """Convert a TOML scalar to Decimal without going through float.

    tomllib yields `int` / `float` for numeric values. Stringify the float
    first so `Decimal` gets an exact textual representation (e.g., `15.00`
    instead of `15.0` or a binary-float approximation).
    """
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        raise TypeError(f"{field} must be numeric, got bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"{field} must be numeric, got {type(value).__name__}")


def _parse_pricing_document(
    data: dict[str, object],
) -> tuple[dict[tuple[str, str], ModelPricing], ModelPricing]:
    """Walk a parsed TOML doc and pull out `(provider, model) -> ModelPricing`.

    Expected shape (per `config/pricing.toml`):

        [anthropic.claude-opus-4-7]
        input_per_mtok  = 15.00
        output_per_mtok = 75.00
        ...
        [conservative_default]
        input_per_mtok  = 20.00
        output_per_mtok = 100.00
    """
    prices: dict[tuple[str, str], ModelPricing] = {}
    conservative: ModelPricing | None = None

    for top_key, top_val in data.items():
        if not isinstance(top_val, dict):
            continue

        if top_key == "conservative_default":
            conservative = ModelPricing(
                input_per_mtok=_coerce_rate(
                    top_val["input_per_mtok"], field="conservative_default.input_per_mtok"
                ),
                output_per_mtok=_coerce_rate(
                    top_val["output_per_mtok"], field="conservative_default.output_per_mtok"
                ),
            )
            continue

        # top_key is a provider name; each sub-key is a model name
        provider = top_key.lower()
        for model_name, model_table in top_val.items():
            if not isinstance(model_table, dict):
                continue
            pricing = ModelPricing(
                input_per_mtok=_coerce_rate(
                    model_table["input_per_mtok"],
                    field=f"{provider}.{model_name}.input_per_mtok",
                ),
                output_per_mtok=_coerce_rate(
                    model_table["output_per_mtok"],
                    field=f"{provider}.{model_name}.output_per_mtok",
                ),
            )
            prices[(provider, model_name.lower())] = pricing

    if conservative is None:
        # File didn't declare one; use the hardcoded ceiling.
        conservative = _HARDCODED_CONSERVATIVE

    return prices, conservative


# ---------------------------------------------------------------------------
# Repo-root resolution + module singleton
# ---------------------------------------------------------------------------


def _default_pricing_path() -> Path:
    """Resolve `<root>/config/pricing.toml`.

    Uses ``paths.repo_root`` so the bundled .app finds the file in
    PyInstaller's ``_MEIPASS`` (where the spec ships ``config/`` as a
    data file) instead of falling back to ``Path.cwd() == /``.
    """
    from paths import repo_root

    return repo_root() / "config" / "pricing.toml"


_singleton: PricingBook | None = None
_singleton_lock = threading.Lock()


def get_pricing_book() -> PricingBook:
    """Lazy process-wide singleton. First call loads + caches; subsequent calls
    return the same instance. Tests that need a clean slate can call
    `reset_pricing_book()`.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = PricingBook.load()
    return _singleton


def reset_pricing_book() -> None:
    """Test hook: drop the cached singleton so the next `get_pricing_book()`
    rebuilds it (e.g., after a test swaps in a different config path)."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "ModelPricing",
    "PricingBook",
    "get_pricing_book",
    "reset_pricing_book",
]
