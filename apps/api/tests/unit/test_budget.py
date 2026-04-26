"""Budget alerts tests (spec §7.7.5 D).

Monthly cap → amber at 80% → block at 100%. Cap is supplied via the
`LAWSCHOOL_MONTHLY_CAP_USD` env var until the settings UI lands.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session

from costs import tracker
from data import db
from data.models import CostEvent, Provider


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


def _seed_event(amount: Decimal, when: datetime | None = None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        event = CostEvent(
            session_id="s1",
            model="claude-opus-4-7",
            provider=Provider.ANTHROPIC,
            input_tokens=100,
            output_tokens=50,
            input_cost_usd=Decimal("0"),
            output_cost_usd=Decimal("0"),
            total_cost_usd=amount,
            feature="case_brief",
        )
        if when is not None:
            event.timestamp = when
        session.add(event)
        session.commit()


# ---------------------------------------------------------------------------
# get_monthly_budget_cap_usd
# ---------------------------------------------------------------------------


def test_cap_unset_returns_none(temp_env: None) -> None:
    assert tracker.get_monthly_budget_cap_usd() is None


def test_cap_set_via_env(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "25.00")
    assert tracker.get_monthly_budget_cap_usd() == Decimal("25.00")


def test_cap_zero_means_off(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "0")
    assert tracker.get_monthly_budget_cap_usd() is None


def test_cap_negative_means_off(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "-5.0")
    assert tracker.get_monthly_budget_cap_usd() is None


def test_cap_unparseable_returns_none(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "garbage-123")
    assert tracker.get_monthly_budget_cap_usd() is None


# ---------------------------------------------------------------------------
# current_month_total_usd
# ---------------------------------------------------------------------------


def test_current_month_aggregates_only_this_month(temp_env: None) -> None:
    now = datetime.now(tz=UTC)
    _seed_event(Decimal("1.25"), when=now)
    last_month = (now.replace(day=1) - timedelta(days=5))
    _seed_event(Decimal("99.00"), when=last_month)
    total = tracker.current_month_total_usd(now=now)
    assert total == Decimal("1.25")


def test_current_month_empty_is_zero(temp_env: None) -> None:
    total = tracker.current_month_total_usd()
    assert total == Decimal("0")


# ---------------------------------------------------------------------------
# get_budget_status
# ---------------------------------------------------------------------------


def test_budget_off_state(temp_env: None) -> None:
    status = tracker.get_budget_status()
    assert status.state == "off"
    assert status.cap_usd is None
    assert status.percent_used == 0.0


def test_budget_ok_state(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "10.00")
    _seed_event(Decimal("2.00"))   # 20% used
    status = tracker.get_budget_status()
    assert status.state == "ok"
    assert status.percent_used == pytest.approx(0.20)


def test_budget_warning_state_at_80_percent(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "10.00")
    _seed_event(Decimal("8.00"))
    status = tracker.get_budget_status()
    assert status.state == "warning"
    assert status.percent_used == pytest.approx(0.80)


def test_budget_exceeded_at_100_percent(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "10.00")
    _seed_event(Decimal("10.00"))
    status = tracker.get_budget_status()
    assert status.state == "exceeded"


def test_budget_exceeded_over_100_percent(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "5.00")
    _seed_event(Decimal("7.50"))
    status = tracker.get_budget_status()
    assert status.state == "exceeded"
    assert status.percent_used == pytest.approx(1.50)


# ---------------------------------------------------------------------------
# raise_if_over_budget (spec §7.7.8: "at 100% cap, next LLM call is blocked")
# ---------------------------------------------------------------------------


def test_raise_if_over_budget_does_nothing_when_off(temp_env: None) -> None:
    tracker.raise_if_over_budget()  # no exception


def test_raise_if_over_budget_does_nothing_when_under_cap(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "10.00")
    _seed_event(Decimal("5.00"))
    tracker.raise_if_over_budget()  # no exception


def test_raise_if_over_budget_does_nothing_at_warning_threshold(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "10.00")
    _seed_event(Decimal("8.50"))
    tracker.raise_if_over_budget()  # warnings don't block


def test_raise_if_over_budget_blocks_at_cap(
    temp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "10.00")
    _seed_event(Decimal("10.01"))
    with pytest.raises(tracker.BudgetExceededError) as excinfo:
        tracker.raise_if_over_budget()
    msg = str(excinfo.value)
    assert "10" in msg
    assert "Settings" in msg  # actionable message per spec §7.5
