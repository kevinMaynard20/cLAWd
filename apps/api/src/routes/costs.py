"""Cost-tracking API routes — spec §7.7.5.

Feeds the always-visible cost badge (§7.7.5 A), the Cost Details panel (B),
CSV export (B), and the reset-session button (B). The pre-flight modal (C) is
triggered client-side based on `estimator.estimate_feature_cost` which is
stubbed in Phase 1 — endpoints for it land in Phase 2.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from costs import tracker
from data.models import CostEvent, Provider

router = APIRouter(prefix="/costs", tags=["costs"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SessionTotalsResponse(BaseModel):
    session_id: str
    total_usd: Decimal
    input_tokens: int
    output_tokens: int


class LifetimeTotalResponse(BaseModel):
    total_usd: Decimal


class FeatureBreakdownResponse(BaseModel):
    breakdown: dict[str, Decimal]


class CostEventDTO(BaseModel):
    """Serialized CostEvent for the UI. Every field safe to expose publicly
    (local-only, no secrets)."""

    id: str
    timestamp: datetime
    session_id: str
    model: str
    provider: Provider
    input_tokens: int
    output_tokens: int
    input_cost_usd: Decimal
    output_cost_usd: Decimal
    total_cost_usd: Decimal
    feature: str
    artifact_id: str | None = None
    cached: bool

    @classmethod
    def from_model(cls, event: CostEvent) -> CostEventDTO:
        return cls(
            id=event.id,
            timestamp=event.timestamp,
            session_id=event.session_id,
            model=event.model,
            provider=event.provider,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            input_cost_usd=event.input_cost_usd,
            output_cost_usd=event.output_cost_usd,
            total_cost_usd=event.total_cost_usd,
            feature=event.feature,
            artifact_id=event.artifact_id,
            cached=event.cached,
        )


class EventsResponse(BaseModel):
    events: list[CostEventDTO]
    count: int


class ResetSessionResponse(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


@router.get("/session", response_model=SessionTotalsResponse)
def session_totals() -> SessionTotalsResponse:
    sess_id = tracker.SESSION_ID
    total = tracker.session_total_usd()
    inp, out = tracker.session_token_total()
    return SessionTotalsResponse(
        session_id=sess_id,
        total_usd=total,
        input_tokens=inp,
        output_tokens=out,
    )


@router.get("/lifetime", response_model=LifetimeTotalResponse)
def lifetime_total() -> LifetimeTotalResponse:
    return LifetimeTotalResponse(total_usd=tracker.lifetime_total_usd())


@router.get("/features", response_model=FeatureBreakdownResponse)
def feature_breakdown(
    since: datetime | None = Query(
        None, description="Optional ISO-8601 timestamp; only events at or after this."
    ),
) -> FeatureBreakdownResponse:
    return FeatureBreakdownResponse(breakdown=tracker.per_feature_breakdown(since=since))


class DailyPoint(BaseModel):
    date: str  # YYYY-MM-DD
    total_usd: Decimal


class DailyTotalsResponse(BaseModel):
    days: list[DailyPoint]


@router.get("/daily", response_model=DailyTotalsResponse)
def daily_totals(
    days_back: int = Query(30, ge=1, le=365),
) -> DailyTotalsResponse:
    """Per-day cost totals for the last N days (spec §7.7.5 B bullet 3, Q20).
    Drives the chart on the Cost Details panel."""
    rows = tracker.per_day_totals_usd(days_back=days_back)
    return DailyTotalsResponse(
        days=[DailyPoint(date=d, total_usd=v) for d, v in rows]
    )


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


@router.get("/events", response_model=EventsResponse)
def list_events(
    limit: int = Query(100, ge=1, le=1000),
    feature: str | None = Query(None),
    cached: bool | None = Query(None),
) -> EventsResponse:
    rows = tracker.recent_events(limit=limit, feature=feature, cached=cached)
    return EventsResponse(
        events=[CostEventDTO.from_model(r) for r in rows],
        count=len(rows),
    )


@router.get("/export.csv")
def export_csv(feature: str | None = Query(None), cached: bool | None = Query(None)):
    """Stream a CSV export of all matching events (spec §7.7.5 B)."""
    rows = tracker.recent_events(limit=100_000, feature=feature, cached=cached)

    def iter_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "id",
                "timestamp",
                "session_id",
                "model",
                "provider",
                "input_tokens",
                "output_tokens",
                "input_cost_usd",
                "output_cost_usd",
                "total_cost_usd",
                "feature",
                "artifact_id",
                "cached",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()
        for r in rows:
            writer.writerow(
                [
                    r.id,
                    r.timestamp.isoformat(),
                    r.session_id,
                    r.model,
                    r.provider.value,
                    r.input_tokens,
                    r.output_tokens,
                    str(r.input_cost_usd),
                    str(r.output_cost_usd),
                    str(r.total_cost_usd),
                    r.feature,
                    r.artifact_id or "",
                    "true" if r.cached else "false",
                ]
            )
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cost_events.csv"},
    )


# ---------------------------------------------------------------------------
# Session reset
# ---------------------------------------------------------------------------


@router.post(
    "/reset-session",
    response_model=ResetSessionResponse,
    status_code=status.HTTP_200_OK,
)
def reset_session() -> ResetSessionResponse:
    new_id = tracker.reset_session_id()
    return ResetSessionResponse(session_id=new_id)


# ---------------------------------------------------------------------------
# Budget status (spec §7.7.5 D)
# ---------------------------------------------------------------------------


class BudgetStatusResponse(BaseModel):
    cap_usd: Decimal | None
    current_month_usd: Decimal
    percent_used: float
    state: str  # "off" | "ok" | "warning" | "exceeded"
    warning_threshold_pct: float


@router.get("/budget", response_model=BudgetStatusResponse)
def budget_status() -> BudgetStatusResponse:
    """Drives the cost-badge amber state (§7.7.5 A) and blocking modal (§7.7.5 D)."""
    st = tracker.get_budget_status()
    return BudgetStatusResponse(
        cap_usd=st.cap_usd,
        current_month_usd=st.current_month_usd,
        percent_used=st.percent_used,
        state=st.state,
        warning_threshold_pct=st.warning_threshold_pct,
    )


# ---------------------------------------------------------------------------
# Unused-import guard
# ---------------------------------------------------------------------------

_ = HTTPException  # for future 400/409 handlers; keeps import sticky
