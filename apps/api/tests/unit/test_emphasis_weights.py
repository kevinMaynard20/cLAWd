"""Unit tests for :mod:`costs.emphasis_weights`.

Covers:
- Real-config load populates every field from ``config/emphasis_weights.toml``.
- Missing / malformed / shape-invalid config falls back to spec defaults and
  emits a WARN log.
- Singleton accessor is cached.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from costs.emphasis_weights import (
    EmphasisWeights,
    get_weights,
    load_weights,
    reset_weights,
)


@pytest.fixture(autouse=True)
def _reset_weights_singleton() -> None:
    """Clean slate per test so warn/cached state doesn't leak."""
    reset_weights()
    yield
    reset_weights()


# ---------------------------------------------------------------------------
# Load — real config
# ---------------------------------------------------------------------------


def test_load_weights_real_config_populates_every_field() -> None:
    """Values come from the repo's ``config/emphasis_weights.toml``. Each
    field lands on the dataclass with the right type."""
    weights = load_weights()

    assert isinstance(weights, EmphasisWeights)
    # Spot-check each weight matches the TOML. These are the values committed
    # to `config/emphasis_weights.toml` today.
    assert weights.minutes_on == Decimal("0.20")
    assert weights.return_count == Decimal("0.25")
    assert weights.hypotheticals_run == Decimal("0.25")
    assert weights.engaged_questions == Decimal("0.15")
    assert weights.not_disclaimed == Decimal("0.15")

    assert weights.disclaimed_penalty == Decimal("-0.50")

    assert weights.minutes_on_cap == 20.0
    assert weights.return_count_cap == 8
    assert weights.hypotheticals_run_cap == 5
    assert weights.engaged_questions_cap == 6


def test_load_weights_sum_of_weights_is_one() -> None:
    """Sanity check on the TOML: positive weights should sum to ~1.0.
    Guards against a future edit that adjusts one weight but not the others
    in a way that skews the mechanical score unexpectedly."""
    w = load_weights()
    total = (
        w.minutes_on
        + w.return_count
        + w.hypotheticals_run
        + w.engaged_questions
        + w.not_disclaimed
    )
    assert total == Decimal("1.00")


# ---------------------------------------------------------------------------
# Load — missing / malformed config
# ---------------------------------------------------------------------------


def test_load_weights_missing_file_falls_back(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.toml"
    with capture_logs() as cap:
        w = load_weights(missing)

    # Fallback values used.
    assert w.minutes_on == Decimal("0.20")
    assert w.disclaimed_penalty == Decimal("-0.50")

    warn_events = [
        e
        for e in cap
        if e.get("log_level") == "warning"
        and "emphasis_weights_config" in e.get("event", "")
    ]
    assert warn_events, f"expected an emphasis_weights_config_* warning; got {cap}"
    assert any("missing" in e["event"] for e in warn_events)


def test_load_weights_malformed_toml_falls_back(tmp_path: Path) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text(
        "[weights\n"
        "minutes_on = not a number\n",
        encoding="utf-8",
    )
    with capture_logs() as cap:
        w = load_weights(broken)

    # Fallback values used.
    assert w.minutes_on == Decimal("0.20")
    assert w.return_count_cap == 8

    warn_events = [
        e
        for e in cap
        if e.get("log_level") == "warning"
        and "emphasis_weights_config" in e.get("event", "")
    ]
    assert warn_events, cap
    assert any(
        "malformed" in e["event"] or "invalid_shape" in e["event"]
        for e in warn_events
    )


def test_load_weights_missing_keys_falls_back(tmp_path: Path) -> None:
    """A TOML that parses but lacks required keys should fall back + WARN."""
    partial = tmp_path / "partial.toml"
    partial.write_text(
        "[weights]\n"
        "minutes_on = 0.20\n"
        "# missing the other required keys\n",
        encoding="utf-8",
    )
    with capture_logs() as cap:
        w = load_weights(partial)

    assert w.minutes_on == Decimal("0.20")  # fallback value happens to match
    assert w.return_count == Decimal("0.25")  # definitely from fallback

    warn_events = [
        e
        for e in cap
        if e.get("log_level") == "warning"
        and "emphasis_weights_config" in e.get("event", "")
    ]
    assert warn_events, cap
    assert any("invalid_shape" in e["event"] for e in warn_events)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def test_get_weights_returns_cached_instance() -> None:
    a = get_weights()
    b = get_weights()
    assert a is b


def test_reset_weights_drops_cache() -> None:
    a = get_weights()
    reset_weights()
    b = get_weights()
    assert a is not b
    # But the content is the same.
    assert a == b
