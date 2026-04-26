"""Unit tests for the `costs` package (spec §7.7.4 / §7.7.5 / §7.7.8).

Covers:
- PricingBook load + lookup + cost arithmetic
- Missing/malformed config fallback to conservative defaults (+ warning)
- Unknown-model fallback with warn-once semantics
- Tracker: record_llm_call, cached events, session/lifetime totals,
  per-feature breakdown, session_id reset, token totals
- Estimator stub raises NotImplementedError with the feature name

Follows the `temp_db` fixture pattern from test_models.py: real SQLite, no
mocks at the SQLModel layer.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session, select
from structlog.testing import capture_logs

from costs import tracker as tracker_mod
from costs.estimator import CostEstimate, PreflightRequired, estimate_feature_cost
from costs.pricing import ModelPricing, PricingBook, get_pricing_book, reset_pricing_book
from data import db
from data.models import CostEvent, Provider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh SQLite file per test. Same pattern as test_models.py."""
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


@pytest.fixture(autouse=True)
def _reset_pricing_singleton():
    """Every test starts with a clean pricing singleton so warn-once state
    doesn't leak between tests."""
    reset_pricing_book()
    yield
    reset_pricing_book()


@pytest.fixture
def reset_session(temp_db: None):
    """Give each tracker test a fresh SESSION_ID so prior tests' events
    don't bleed into the session-total assertions."""
    tracker_mod.reset_session_id()
    yield


# ---------------------------------------------------------------------------
# PricingBook — loading from the real config
# ---------------------------------------------------------------------------


def test_pricing_loads_from_config() -> None:
    """`PricingBook.load()` with no arg must resolve the real
    `config/pricing.toml` by walking up from the module file to `spec.md`."""
    book = PricingBook.load()
    pricing = book.get("anthropic", "claude-opus-4-7")
    assert isinstance(pricing, ModelPricing)
    assert pricing.input_per_mtok == Decimal("15.00")
    assert pricing.output_per_mtok == Decimal("75.00")


def test_pricing_compute_cost_opus() -> None:
    """Pin the opus arithmetic to an explicitly re-derived expected total so
    the test traps any unit-conversion regression."""
    book = PricingBook.load()
    input_tokens = 1200
    output_tokens = 450

    expected_total = (
        Decimal("15.00") * Decimal(1200) / Decimal("1_000_000")
        + Decimal("75.00") * Decimal(450) / Decimal("1_000_000")
    )
    in_cost, out_cost, total = book.compute_cost(
        "anthropic", "claude-opus-4-7", input_tokens, output_tokens
    )
    assert in_cost == Decimal("15.00") * Decimal(1200) / Decimal("1_000_000")
    assert out_cost == Decimal("75.00") * Decimal(450) / Decimal("1_000_000")
    assert total == expected_total


def test_pricing_get_singleton_is_cached() -> None:
    """`get_pricing_book()` must return the same instance on repeat calls so
    warn-once state persists across lookups."""
    book_a = get_pricing_book()
    book_b = get_pricing_book()
    assert book_a is book_b


# ---------------------------------------------------------------------------
# PricingBook — missing / malformed config
# ---------------------------------------------------------------------------


def test_pricing_config_missing_fallback(tmp_path: Path) -> None:
    """Absent `pricing.toml` ⇒ conservative default for every model +
    single WARN surface (spec §7.7.4)."""
    missing = tmp_path / "nonexistent.toml"
    with capture_logs() as cap:
        book = PricingBook.load(missing)

    default = book.get("anthropic", "claude-opus-4-7")  # would normally be priced
    assert default.input_per_mtok == Decimal("20.00")
    assert default.output_per_mtok == Decimal("100.00")

    warn_events = [
        e for e in cap if e.get("log_level") == "warning" and "pricing_config" in e.get("event", "")
    ]
    assert warn_events, f"expected a pricing_config_* warning; got {cap}"
    assert any("missing" in e["event"] for e in warn_events)


def test_pricing_config_malformed_fallback(tmp_path: Path) -> None:
    """Malformed TOML ⇒ same fallback behavior + warning."""
    broken = tmp_path / "broken.toml"
    broken.write_text(
        "[anthropic.claude-opus-4-7\n"  # missing closing bracket on purpose
        "input_per_mtok = not a number\n",
        encoding="utf-8",
    )
    with capture_logs() as cap:
        book = PricingBook.load(broken)

    default = book.get("anthropic", "claude-opus-4-7")
    assert default.input_per_mtok == Decimal("20.00")
    assert default.output_per_mtok == Decimal("100.00")

    warn_events = [
        e for e in cap if e.get("log_level") == "warning" and "pricing_config" in e.get("event", "")
    ]
    assert warn_events, f"expected a pricing_config_* warning; got {cap}"
    assert any(
        "malformed" in e["event"] or "invalid_shape" in e["event"] for e in warn_events
    )


# ---------------------------------------------------------------------------
# Unknown-model fallback + warn-once
# ---------------------------------------------------------------------------


def test_pricing_unknown_model_conservative_default() -> None:
    """An unknown model logs one WARN and serves the conservative default;
    subsequent lookups are silent."""
    book = PricingBook.load()  # real config
    with capture_logs() as cap:
        first = book.get("anthropic", "claude-grover-9")
        second = book.get("anthropic", "claude-grover-9")

    assert first is second  # both fall through to the same default instance
    assert first.input_per_mtok == Decimal("20.00")
    assert first.output_per_mtok == Decimal("100.00")

    unknown_warns = [
        e
        for e in cap
        if e.get("log_level") == "warning" and e.get("event") == "pricing_unknown_model"
    ]
    assert len(unknown_warns) == 1, (
        f"expected exactly one pricing_unknown_model warning, got {len(unknown_warns)}: {cap}"
    )


# ---------------------------------------------------------------------------
# Tracker — record_llm_call
# ---------------------------------------------------------------------------


def test_tracker_record_llm_call(reset_session: None) -> None:
    """Live (non-cached) call persists a CostEvent with correctly-computed
    token-to-cost math."""
    event = tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1200,
        output_tokens=450,
        feature="case_brief",
    )

    expected_total = (
        Decimal("15.00") * Decimal(1200) / Decimal("1_000_000")
        + Decimal("75.00") * Decimal(450) / Decimal("1_000_000")
    )
    assert event.total_cost_usd == expected_total
    assert event.feature == "case_brief"
    assert event.model == "claude-opus-4-7"
    assert event.provider is Provider.ANTHROPIC
    assert event.cached is False
    assert event.session_id == tracker_mod.SESSION_ID

    # Confirm it really landed in the DB.
    with Session(db.get_engine()) as session:
        loaded = session.exec(select(CostEvent)).one()
        assert loaded.id == event.id
        assert loaded.total_cost_usd == expected_total


def test_tracker_cached_call_zero_cost(reset_session: None) -> None:
    """Cache hit: cached=True, all cost fields zero, tokens still persisted
    (§4.3)."""
    event = tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=0,
        output_tokens=0,
        feature="case_brief",
        cached=True,
    )
    assert event.cached is True
    assert event.total_cost_usd == Decimal("0")
    assert event.input_cost_usd == Decimal("0")
    assert event.output_cost_usd == Decimal("0")

    with Session(db.get_engine()) as session:
        loaded = session.exec(select(CostEvent)).one()
        assert loaded.cached is True
        assert loaded.total_cost_usd == Decimal("0")


# ---------------------------------------------------------------------------
# Tracker — aggregates
# ---------------------------------------------------------------------------


def test_session_total_across_calls(reset_session: None) -> None:
    """Three calls in the same session ⇒ session_total_usd sums them."""
    totals = []
    for _ in range(3):
        ev = tracker_mod.record_llm_call(
            model="claude-opus-4-7",
            provider="anthropic",
            input_tokens=1000,
            output_tokens=500,
            feature="case_brief",
        )
        totals.append(ev.total_cost_usd)

    expected = sum(totals, Decimal("0"))
    assert tracker_mod.session_total_usd() == expected
    assert tracker_mod.lifetime_total_usd() == expected


def test_per_feature_breakdown(reset_session: None) -> None:
    """Breakdown groups by the feature string."""
    # Two briefs + one grade. Compute by hand to pin the math.
    book = get_pricing_book()
    _, _, brief_cost = book.compute_cost("anthropic", "claude-opus-4-7", 1000, 500)
    _, _, grade_cost = book.compute_cost("anthropic", "claude-opus-4-7", 2000, 1000)

    tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=500,
        feature="case_brief",
    )
    tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=500,
        feature="case_brief",
    )
    tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=2000,
        output_tokens=1000,
        feature="irac_grade",
    )

    breakdown = tracker_mod.per_feature_breakdown()
    assert set(breakdown.keys()) == {"case_brief", "irac_grade"}
    assert breakdown["case_brief"] == brief_cost * 2
    assert breakdown["irac_grade"] == grade_cost


def test_reset_session_id_changes_id(reset_session: None) -> None:
    """Resetting the session id starts a new grouping; prior events persist
    but do not count toward the new session's total."""
    tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=500,
        feature="case_brief",
    )
    old_session_id = tracker_mod.SESSION_ID
    old_session_total = tracker_mod.session_total_usd(old_session_id)
    assert old_session_total > Decimal("0")

    new_id = tracker_mod.reset_session_id()
    assert new_id != old_session_id
    assert new_id == tracker_mod.SESSION_ID

    # New session starts empty…
    assert tracker_mod.session_total_usd() == Decimal("0")
    # …but the old session's events are still there.
    assert tracker_mod.session_total_usd(old_session_id) == old_session_total
    # …and lifetime includes them.
    assert tracker_mod.lifetime_total_usd() == old_session_total


def test_session_token_totals(reset_session: None) -> None:
    """session_token_total returns (sum input, sum output) for the session."""
    tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1200,
        output_tokens=450,
        feature="case_brief",
    )
    tracker_mod.record_llm_call(
        model="claude-sonnet-4-6",
        provider="anthropic",
        input_tokens=800,
        output_tokens=100,
        feature="flashcards",
    )
    in_total, out_total = tracker_mod.session_token_total()
    assert in_total == 2000
    assert out_total == 550


def test_recent_events_filters(reset_session: None) -> None:
    """Defensive test for the log-view query helper: filters and limit behave."""
    for _ in range(3):
        tracker_mod.record_llm_call(
            model="claude-opus-4-7",
            provider="anthropic",
            input_tokens=100,
            output_tokens=50,
            feature="case_brief",
        )
    tracker_mod.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=0,
        output_tokens=0,
        feature="case_brief",
        cached=True,
    )

    assert len(tracker_mod.recent_events(limit=10)) == 4
    assert len(tracker_mod.recent_events(limit=2)) == 2
    assert len(tracker_mod.recent_events(cached=True)) == 1
    assert len(tracker_mod.recent_events(cached=False)) == 3
    assert len(tracker_mod.recent_events(feature="case_brief")) == 4
    assert tracker_mod.recent_events(feature="nonexistent") == []


# ---------------------------------------------------------------------------
# Estimator — Phase-1 stub
# ---------------------------------------------------------------------------


def test_estimator_returns_estimate_for_known_feature() -> None:
    """Phase 2: estimator now returns a real CostEstimate for every known
    feature (spec §7.7.5 C)."""
    est = estimate_feature_cost("book_ingestion", {"page_count": 100})
    assert est.expected_usd > Decimal("0")
    assert est.low_usd < est.expected_usd < est.high_usd
    assert "$" in est.label and "±" in est.label


def test_estimator_unknown_feature_returns_wide_band() -> None:
    """Unknown features surface with a flagged wide-band estimate rather than
    crashing — lets new features land with a visible cost warning."""
    est = estimate_feature_cost("brand_new_feature", {})
    assert "unknown feature" in est.label.lower()
    assert est.high_usd > est.expected_usd


def test_estimator_book_ingestion_property_casebook_sanity() -> None:
    """~1400 source pages at Sonnet rates should land in a few dollars — NOT
    30× that. Sanity guard against regressions that silently inflate costs."""
    from costs.estimator import estimate_book_ingestion

    est = estimate_book_ingestion({"page_count": 1400})
    assert Decimal("0") < est.expected_usd < Decimal("50")


def test_estimator_bulk_brief_scales_linearly() -> None:
    """12 cases ≈ 12× single-case expected cost."""
    from costs.estimator import estimate_bulk_brief_generation, estimate_case_brief

    single = estimate_case_brief({})
    bulk = estimate_bulk_brief_generation({"case_count": 12})
    # Compare as floats since pytest.approx doesn't accept Decimal directly.
    assert float(bulk.expected_usd) == pytest.approx(
        float(single.expected_usd) * 12, rel=1e-6
    )


def test_estimator_types_exist() -> None:
    """CostEstimate + PreflightRequired exist and have the documented shape."""
    est = CostEstimate(
        low_usd=Decimal("1.50"),
        expected_usd=Decimal("2.40"),
        high_usd=Decimal("3.30"),
        label="~$2.40 (±30%)",
    )
    assert est.expected_usd == Decimal("2.40")

    err = PreflightRequired(feature="bulk_brief_generation", estimate=est)
    assert err.feature == "bulk_brief_generation"
    assert err.estimate is est
    assert "bulk_brief_generation" in str(err)
