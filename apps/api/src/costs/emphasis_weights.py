"""Emphasis-scoring weights loaded from ``config/emphasis_weights.toml``.

Mirrors the lazy-singleton pattern in :mod:`costs.pricing`:

- :func:`load_weights` parses the TOML file once and returns a frozen
  :class:`EmphasisWeights` dataclass. On any failure (missing file, malformed
  TOML, missing keys) the loader WARN-logs and falls back to sensible
  defaults drawn from spec §3.10 / §5.7 so callers always get a usable object.
- :func:`get_weights` is the lazy process-wide singleton accessor; tests can
  call :func:`reset_weights` to drop the cached instance.

Spec §7.3: "All configurable weights, model choices, and prompt-level knobs
live in ``config/*.toml``. No magic numbers in code." The defaults defined
here are the conservative fallback for the rare case the config file goes
missing — they mirror the production TOML values so behavior does not swing
wildly when config breaks.
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmphasisWeights:
    """Resolved weights + normalization caps for computing
    ``EmphasisItem.exam_signal_score`` (spec §3.10).

    Weights / penalty are :class:`~decimal.Decimal` so that configuration
    arithmetic stays exact when we eventually want to feed these values into
    other Decimal-money-style calculations. Caps are plain floats/ints —
    they are used as normalizers, not amounts.
    """

    # [weights] table — each is a multiplier against the normalized raw feature.
    minutes_on: Decimal
    return_count: Decimal
    hypotheticals_run: Decimal
    engaged_questions: Decimal
    not_disclaimed: Decimal

    # [penalties] table — applied additively when disclaimed=True.
    disclaimed_penalty: Decimal

    # [normalization] table — cap values at or above which the raw feature
    # saturates to 1.0. Caps are expressed in their native units (minutes /
    # count) not Decimals.
    minutes_on_cap: float
    return_count_cap: int
    hypotheticals_run_cap: int
    engaged_questions_cap: int


# Fallback values — kept in sync with `config/emphasis_weights.toml`. Used
# only when the TOML file is missing / malformed so behavior doesn't swing
# wildly just because a config file got renamed.
_FALLBACK_WEIGHTS = EmphasisWeights(
    minutes_on=Decimal("0.20"),
    return_count=Decimal("0.25"),
    hypotheticals_run=Decimal("0.25"),
    engaged_questions=Decimal("0.15"),
    not_disclaimed=Decimal("0.15"),
    disclaimed_penalty=Decimal("-0.50"),
    minutes_on_cap=20.0,
    return_count_cap=8,
    hypotheticals_run_cap=5,
    engaged_questions_cap=6,
)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_decimal(value: Any, *, field: str) -> Decimal:
    """Convert a TOML scalar to :class:`Decimal` without going through float.

    Same posture as :func:`costs.pricing._coerce_rate`: stringify floats so
    the Decimal representation is exact.
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


def _coerce_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric, got bool")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"{field} must be numeric, got {type(value).__name__}")


def _coerce_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"{field} must be numeric, got {type(value).__name__}")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Walk up to the repo root (the directory containing ``spec.md``).

    Matches the convention used by :mod:`data.db`, :mod:`costs.pricing`, and
    :mod:`primitives.prompt_loader` so every module agrees on "the repo."
    """
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "spec.md").exists():
            return candidate
    return Path.cwd()


def _default_weights_path() -> Path:
    return _repo_root() / "config" / "emphasis_weights.toml"


def _parse_weights_document(data: dict[str, Any]) -> EmphasisWeights:
    """Walk a parsed TOML doc and pull out an :class:`EmphasisWeights` instance.

    Expected shape (see ``config/emphasis_weights.toml``)::

        [weights]
        minutes_on        = 0.20
        return_count      = 0.25
        hypotheticals_run = 0.25
        engaged_questions = 0.15
        not_disclaimed    = 0.15

        [penalties]
        disclaimed = -0.50

        [normalization]
        minutes_on_cap        = 20.0
        return_count_cap      = 8
        hypotheticals_run_cap = 5
        engaged_questions_cap = 6
    """
    weights_tbl = data.get("weights") or {}
    penalties_tbl = data.get("penalties") or {}
    norm_tbl = data.get("normalization") or {}

    if not isinstance(weights_tbl, dict):
        raise TypeError("[weights] must be a table")
    if not isinstance(penalties_tbl, dict):
        raise TypeError("[penalties] must be a table")
    if not isinstance(norm_tbl, dict):
        raise TypeError("[normalization] must be a table")

    return EmphasisWeights(
        minutes_on=_coerce_decimal(
            weights_tbl["minutes_on"], field="weights.minutes_on"
        ),
        return_count=_coerce_decimal(
            weights_tbl["return_count"], field="weights.return_count"
        ),
        hypotheticals_run=_coerce_decimal(
            weights_tbl["hypotheticals_run"], field="weights.hypotheticals_run"
        ),
        engaged_questions=_coerce_decimal(
            weights_tbl["engaged_questions"], field="weights.engaged_questions"
        ),
        not_disclaimed=_coerce_decimal(
            weights_tbl["not_disclaimed"], field="weights.not_disclaimed"
        ),
        disclaimed_penalty=_coerce_decimal(
            penalties_tbl["disclaimed"], field="penalties.disclaimed"
        ),
        minutes_on_cap=_coerce_float(
            norm_tbl["minutes_on_cap"], field="normalization.minutes_on_cap"
        ),
        return_count_cap=_coerce_int(
            norm_tbl["return_count_cap"], field="normalization.return_count_cap"
        ),
        hypotheticals_run_cap=_coerce_int(
            norm_tbl["hypotheticals_run_cap"],
            field="normalization.hypotheticals_run_cap",
        ),
        engaged_questions_cap=_coerce_int(
            norm_tbl["engaged_questions_cap"],
            field="normalization.engaged_questions_cap",
        ),
    )


def load_weights(path: Path | None = None) -> EmphasisWeights:
    """Load emphasis-scoring weights from ``config/emphasis_weights.toml``.

    On any failure (missing file, malformed TOML, missing required keys)
    emits a WARN log and returns :data:`_FALLBACK_WEIGHTS` — callers always
    get a usable object. The fallback values mirror the current TOML so
    swings in behavior are minimized.
    """
    resolved_path = path if path is not None else _default_weights_path()

    try:
        raw = resolved_path.read_bytes()
    except FileNotFoundError:
        log.warning(
            "emphasis_weights_config_missing",
            path=str(resolved_path),
            fallback="spec_defaults",
        )
        return _FALLBACK_WEIGHTS
    except OSError as exc:
        log.warning(
            "emphasis_weights_config_unreadable",
            path=str(resolved_path),
            error=str(exc),
            fallback="spec_defaults",
        )
        return _FALLBACK_WEIGHTS

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        log.warning(
            "emphasis_weights_config_malformed",
            path=str(resolved_path),
            error=str(exc),
            fallback="spec_defaults",
        )
        return _FALLBACK_WEIGHTS

    try:
        weights = _parse_weights_document(data)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning(
            "emphasis_weights_config_invalid_shape",
            path=str(resolved_path),
            error=str(exc),
            fallback="spec_defaults",
        )
        return _FALLBACK_WEIGHTS

    log.info(
        "emphasis_weights_config_loaded",
        path=str(resolved_path),
    )
    return weights


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_singleton: EmphasisWeights | None = None
_singleton_lock = threading.Lock()


def get_weights() -> EmphasisWeights:
    """Lazy process-wide singleton accessor. First call loads + caches; later
    calls return the same instance. Tests can reset via :func:`reset_weights`.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = load_weights()
    return _singleton


def reset_weights() -> None:
    """Drop the cached singleton so the next :func:`get_weights` rebuilds it.
    Used by tests that need to swap in a different config path."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "EmphasisWeights",
    "get_weights",
    "load_weights",
    "reset_weights",
]
