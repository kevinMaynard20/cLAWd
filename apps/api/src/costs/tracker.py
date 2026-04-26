"""CostEvent persistence + aggregate reporting (spec §7.7.4, §7.7.5).

Responsibilities:
- Generate a `SESSION_ID` at import time (refreshes on app launch, per spec).
- Persist a CostEvent for every LLM call via `record_llm_call(...)`.
- Serve the aggregates the UI needs: session total, lifetime total, per-feature
  breakdown, recent-events log, per-session token totals.

This module does NOT redefine `CostEvent`, `Provider`, or `session_scope` — it
imports them from `data.models` / `data.db`.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import structlog
from sqlalchemy import func
from sqlmodel import select

from data.db import session_scope
from data.models import CostEvent, Provider

from .pricing import get_pricing_book

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Session id
# ---------------------------------------------------------------------------

# One uuid per process. The process is new on every app launch, so this
# naturally refreshes then (spec §7.7.4: "refreshes on app launch").
SESSION_ID: str = uuid.uuid4().hex


def reset_session_id() -> str:
    """Rotate the module-level SESSION_ID. Drives the Cost Details panel's
    "Reset session counter" button (spec §7.7.5 B). Historical events are
    untouched; they just fall outside the new session's grouping."""
    global SESSION_ID
    SESSION_ID = uuid.uuid4().hex
    log.info("session_id_reset", new_session_id=SESSION_ID)
    return SESSION_ID


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _coerce_provider(provider: str | Provider) -> Provider:
    """Accept either the enum or a string; store the enum. Normalize the
    string to lowercase so "Anthropic" and "anthropic" both land on the
    same enum member."""
    if isinstance(provider, Provider):
        return provider
    try:
        return Provider(provider.lower())
    except ValueError as exc:
        raise ValueError(
            f"Unknown provider {provider!r}; expected one of "
            f"{[p.value for p in Provider]}"
        ) from exc


def record_llm_call(
    *,
    model: str,
    provider: str | Provider,
    input_tokens: int,
    output_tokens: int,
    feature: str,
    artifact_id: str | None = None,
    cached: bool = False,
    timestamp: datetime | None = None,
) -> CostEvent:
    """Persist a CostEvent for one LLM call and return the saved row.

    Cost is computed via `PricingBook`. Cache hits are explicitly zeroed out
    for bookkeeping per spec §4.3: "emit a CostEvent with `cached=true` and
    `total_cost_usd=0`." Token counts are passed through as-is — the caller
    decides whether a cached call meaningfully has tokens (usually 0).
    """
    provider_enum = _coerce_provider(provider)

    if cached:
        input_cost = Decimal("0")
        output_cost = Decimal("0")
        total_cost = Decimal("0")
    else:
        book = get_pricing_book()
        input_cost, output_cost, total_cost = book.compute_cost(
            provider_enum.value, model, input_tokens, output_tokens
        )

    event = CostEvent(
        session_id=SESSION_ID,
        model=model,
        provider=provider_enum,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        total_cost_usd=total_cost,
        feature=feature,
        artifact_id=artifact_id,
        cached=cached,
    )
    if timestamp is not None:
        event.timestamp = timestamp

    with session_scope() as session:
        session.add(event)
        session.commit()
        session.refresh(event)
        # Expunge so the returned instance is usable after the session closes.
        session.expunge(event)

    log.info(
        "cost_event_recorded",
        event_id=event.id,
        feature=feature,
        model=model,
        provider=provider_enum.value,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd=str(total_cost),
        cached=cached,
    )
    return event


# ---------------------------------------------------------------------------
# Aggregate queries
# ---------------------------------------------------------------------------


def _to_decimal(value: Decimal | int | float | str | None) -> Decimal:
    """Normalize whatever SQLAlchemy's SUM returns into Decimal."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def session_total_usd(session_id: str | None = None) -> Decimal:
    """Total cost for a given session (defaults to the current one)."""
    sid = session_id if session_id is not None else SESSION_ID
    with session_scope() as session:
        result = session.exec(
            select(func.sum(CostEvent.total_cost_usd)).where(CostEvent.session_id == sid)
        ).one()
        return _to_decimal(result)


def lifetime_total_usd() -> Decimal:
    """Total across every session ever recorded."""
    with session_scope() as session:
        result = session.exec(select(func.sum(CostEvent.total_cost_usd))).one()
        return _to_decimal(result)


def per_feature_breakdown(since: datetime | None = None) -> dict[str, Decimal]:
    """Return `feature -> total_cost_usd`. If `since` is given, restrict to
    events after that timestamp (inclusive). Zero-cost features are included
    if they recorded any events."""
    with session_scope() as session:
        stmt = select(CostEvent.feature, func.sum(CostEvent.total_cost_usd))
        if since is not None:
            stmt = stmt.where(CostEvent.timestamp >= since)
        stmt = stmt.group_by(CostEvent.feature)
        rows = session.exec(stmt).all()

    return {feature: _to_decimal(total) for feature, total in rows}


def per_day_totals_usd(
    *, days_back: int = 30, now: datetime | None = None
) -> list[tuple[str, Decimal]]:
    """Return `[(YYYY-MM-DD, total_usd), ...]` for the last `days_back` days,
    oldest-first. Days with no events are present with `0`. Drives the
    Cost Details panel's per-day chart (spec §7.7.5 B bullet 3 / Q20)."""
    from datetime import UTC, date, timedelta

    if days_back <= 0:
        return []
    today = (now if now is not None else datetime.now(tz=UTC)).date()
    start_date = today - timedelta(days=days_back - 1)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)

    with session_scope() as session:
        stmt = (
            select(CostEvent.timestamp, CostEvent.total_cost_usd)
            .where(CostEvent.timestamp >= start_dt)
        )
        rows = list(session.exec(stmt).all())

    daily: dict[date, Decimal] = {
        start_date + timedelta(days=i): Decimal("0") for i in range(days_back)
    }
    for ts, cost in rows:
        if ts is None:
            continue
        d = ts.date() if hasattr(ts, "date") else ts
        if d in daily:
            daily[d] = daily[d] + _to_decimal(cost)

    return [(d.isoformat(), v) for d, v in sorted(daily.items())]


def recent_events(
    limit: int = 100,
    feature: str | None = None,
    cached: bool | None = None,
) -> list[CostEvent]:
    """Return the most-recent CostEvents (newest first) for the log view.

    Optional filters:
    - `feature`: exact match on the feature name.
    - `cached`: restrict to cache hits (`True`) or live calls (`False`).
    """
    with session_scope() as session:
        stmt = select(CostEvent)
        if feature is not None:
            stmt = stmt.where(CostEvent.feature == feature)
        if cached is not None:
            stmt = stmt.where(CostEvent.cached == cached)
        stmt = stmt.order_by(CostEvent.timestamp.desc()).limit(limit)
        events = list(session.exec(stmt).all())
        # Detach so callers can read attributes after the session closes.
        for ev in events:
            session.expunge(ev)
        return events


def session_token_total(session_id: str | None = None) -> tuple[int, int]:
    """Return `(input_tokens_total, output_tokens_total)` for a session.

    Drives the "$0.47 this session (142K tokens)" cost-badge display
    (spec §7.7.5 A)."""
    sid = session_id if session_id is not None else SESSION_ID
    with session_scope() as session:
        # Run the aggregate through the underlying SQLAlchemy session so the
        # tuple returned by SUM()/SUM() unpacks reliably; sqlmodel.Session.exec
        # wraps scalar results in a way that fights multi-column aggregates.
        row = session.connection().execute(
            select(
                func.sum(CostEvent.input_tokens),
                func.sum(CostEvent.output_tokens),
            ).where(CostEvent.session_id == sid)
        ).one()

    in_raw, out_raw = row
    in_total = int(in_raw) if in_raw is not None else 0
    out_total = int(out_raw) if out_raw is not None else 0
    return in_total, out_total


# ---------------------------------------------------------------------------
# Budget alerts (spec §7.7.5 D)
# ---------------------------------------------------------------------------


BudgetState = Literal["off", "ok", "warning", "exceeded"]


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of the user's monthly budget position.

    - `state == "off"`:     no cap configured → `cap_usd is None`.
    - `state == "ok"`:      under warning threshold (default 80% of cap).
    - `state == "warning"`: ≥ warning threshold but < cap.
    - `state == "exceeded"`: at or over the cap; non-cached LLM calls should
      be blocked until the user raises the cap or the month rolls.
    """

    cap_usd: Decimal | None
    current_month_usd: Decimal
    percent_used: float  # 0.0–1.0+; NaN-free
    state: BudgetState
    warning_threshold_pct: float  # spec default 0.80


_WARNING_THRESHOLD_PCT_DEFAULT = 0.80


def current_month_total_usd(now: datetime | None = None) -> Decimal:
    """Sum CostEvents in the calendar-month containing `now` (UTC).

    Default `now` is `datetime.now(tz=UTC)`. Accepting an override lets tests
    pin the boundary without freezing the clock."""
    t = now if now is not None else datetime.now(tz=UTC)
    month_start = datetime(t.year, t.month, 1, tzinfo=UTC)
    with session_scope() as session:
        result = session.exec(
            select(func.sum(CostEvent.total_cost_usd))
            .where(CostEvent.timestamp >= month_start)
        ).one()
    return _to_decimal(result)


def get_monthly_budget_cap_usd() -> Decimal | None:
    """Read the user's monthly cap.

    For Phase 2 the cap is supplied via `LAWSCHOOL_MONTHLY_CAP_USD` env var.
    The UI-editable setting (spec §7.7.5 D wants this live-editable) lands
    with the Settings → Budget page in a later slice — logged in
    SPEC_QUESTIONS.md. When unset OR set to `0` / negative, returns None
    ("cap is off").
    """
    raw = os.environ.get("LAWSCHOOL_MONTHLY_CAP_USD", "").strip()
    if not raw:
        return None
    try:
        cap = Decimal(raw)
    except (ValueError, ArithmeticError):
        log.warning("budget_cap_unparseable", raw=raw)
        return None
    if cap <= 0:
        return None
    return cap


def get_budget_status(now: datetime | None = None) -> BudgetStatus:
    """Compose the current budget status. Drives the cost-badge amber state
    (§7.7.5 A) and the blocking modal (§7.7.5 D)."""
    cap = get_monthly_budget_cap_usd()
    current = current_month_total_usd(now=now)
    if cap is None:
        return BudgetStatus(
            cap_usd=None,
            current_month_usd=current,
            percent_used=0.0,
            state="off",
            warning_threshold_pct=_WARNING_THRESHOLD_PCT_DEFAULT,
        )
    pct = float(current / cap) if cap > 0 else 0.0
    state: BudgetState
    if pct >= 1.0:
        state = "exceeded"
    elif pct >= _WARNING_THRESHOLD_PCT_DEFAULT:
        state = "warning"
    else:
        state = "ok"
    return BudgetStatus(
        cap_usd=cap,
        current_month_usd=current,
        percent_used=pct,
        state=state,
        warning_threshold_pct=_WARNING_THRESHOLD_PCT_DEFAULT,
    )


class BudgetExceededError(RuntimeError):
    """Raised by feature code when a non-cached LLM call is attempted and the
    user is at/past their monthly cap (spec §7.7.5 D)."""


def raise_if_over_budget(now: datetime | None = None) -> None:
    """Gate-check called by generate() before an LLM call. No-op when the
    budget is off, under cap, or only in warning state — warnings are for UI
    color, not for blocking."""
    status = get_budget_status(now=now)
    if status.state == "exceeded":
        cap = status.cap_usd
        raise BudgetExceededError(
            f"Monthly budget cap ${cap} reached "
            f"(${status.current_month_usd} used). "
            "Raise the cap in Settings → Budget or wait for the new month."
        )


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------


__all__ = [
    "SESSION_ID",
    "BudgetExceededError",
    "BudgetState",
    "BudgetStatus",
    "current_month_total_usd",
    "get_budget_status",
    "get_monthly_budget_cap_usd",
    "lifetime_total_usd",
    "per_feature_breakdown",
    "raise_if_over_budget",
    "recent_events",
    "record_llm_call",
    "reset_session_id",
    "session_token_total",
    "session_total_usd",
]
