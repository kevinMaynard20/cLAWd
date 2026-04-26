"use client";

import { Download, RotateCcw } from "lucide-react";
import * as React from "react";

import { LoadingButton } from "@/components/LoadingButton";
import { Spinner } from "@/components/Spinner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { api, ApiError } from "@/lib/api";
import { formatRelativeTime, formatTokens, formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Spec §7.7.5 B — the Cost Details panel.
 *
 * Phase 1 surfaces: session total + token breakdown, lifetime total,
 * per-feature breakdown table, and a filterable event log with CSV export.
 * The per-day chart (§7.7.5 B bullet 3) is deferred — no chart library is
 * in the dependency manifest, and spec §2.1 discourages pulling in new
 * deps for a UI nicety we can ship later once real cost data exists.
 */

type SessionTotals = {
  session_id: string;
  total_usd: string;
  input_tokens: number;
  output_tokens: number;
};
type LifetimeTotal = { total_usd: string };
type FeatureBreakdown = { breakdown: Record<string, string> };
type CostEvent = {
  id: string;
  timestamp: string;
  session_id: string;
  model: string;
  provider: "anthropic" | "voyage";
  input_tokens: number;
  output_tokens: number;
  input_cost_usd: string;
  output_cost_usd: string;
  total_cost_usd: string;
  feature: string;
  artifact_id: string | null;
  cached: boolean;
};
type EventsResponse = { events: CostEvent[]; count: number };

type DailyPoint = { date: string; total_usd: string };
type DailyTotalsResponse = { days: DailyPoint[] };

type CachedFilter = "any" | "true" | "false";

const DEBOUNCE_MS = 200;

export default function CostsSettingsPage() {
  const [session, setSession] = React.useState<SessionTotals | null>(null);
  const [lifetime, setLifetime] = React.useState<LifetimeTotal | null>(null);
  const [features, setFeatures] = React.useState<FeatureBreakdown | null>(null);
  const [events, setEvents] = React.useState<EventsResponse | null>(null);
  const [daily, setDaily] = React.useState<DailyTotalsResponse | null>(null);
  const [dailyLoading, setDailyLoading] = React.useState(true);
  const [resetting, setResetting] = React.useState(false);
  const [loadError, setLoadError] = React.useState<string | null>(null);

  // Filter UI state.
  const [featureFilter, setFeatureFilter] = React.useState("");
  const [cachedFilter, setCachedFilter] = React.useState<CachedFilter>("any");
  // Debounced committed values — drive the API call.
  const [committedFeature, setCommittedFeature] = React.useState("");

  React.useEffect(() => {
    const handle = setTimeout(() => {
      setCommittedFeature(featureFilter.trim());
    }, DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [featureFilter]);

  const loadAggregates = React.useCallback(async () => {
    try {
      const [sess, life, feat] = await Promise.all([
        api.get<SessionTotals>("/costs/session"),
        api.get<LifetimeTotal>("/costs/lifetime"),
        api.get<FeatureBreakdown>("/costs/features"),
      ]);
      setSession(sess);
      setLifetime(life);
      setFeatures(feat);
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof ApiError
          ? err.message
          : "Could not reach the local backend.",
      );
    }
  }, []);

  const loadDaily = React.useCallback(async () => {
    setDailyLoading(true);
    try {
      const res = await api.get<DailyTotalsResponse>("/costs/daily", {
        days_back: 30,
      });
      setDaily(res);
    } catch (err) {
      setLoadError(
        err instanceof ApiError
          ? err.message
          : "Could not load daily totals.",
      );
    } finally {
      setDailyLoading(false);
    }
  }, []);

  const loadEvents = React.useCallback(async () => {
    const cachedParam =
      cachedFilter === "any" ? undefined : cachedFilter === "true";
    try {
      const res = await api.get<EventsResponse>("/costs/events", {
        limit: 50,
        feature: committedFeature || undefined,
        cached: cachedParam,
      });
      setEvents(res);
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof ApiError
          ? err.message
          : "Could not reach the local backend.",
      );
    }
  }, [committedFeature, cachedFilter]);

  React.useEffect(() => {
    void loadAggregates();
  }, [loadAggregates]);

  React.useEffect(() => {
    void loadDaily();
  }, [loadDaily]);

  React.useEffect(() => {
    void loadEvents();
  }, [loadEvents]);

  const resetSession = async () => {
    setResetting(true);
    try {
      await api.post("/costs/reset-session");
      void loadAggregates();
      void loadEvents();
      void loadDaily();
    } catch (err) {
      setLoadError(
        err instanceof ApiError ? err.message : "Reset failed.",
      );
    } finally {
      setResetting(false);
    }
  };

  const featureRows = React.useMemo(() => {
    if (!features) return [] as Array<[string, number]>;
    return Object.entries(features.breakdown)
      .map(([name, usd]) => [name, Number(usd)] as [string, number])
      .sort((a, b) => b[1] - a[1]);
  }, [features]);

  const sessionTokens =
    (session?.input_tokens ?? 0) + (session?.output_tokens ?? 0);

  const csvHref = React.useMemo(() => {
    const params = new URLSearchParams();
    if (committedFeature) params.set("feature", committedFeature);
    if (cachedFilter !== "any") params.set("cached", cachedFilter);
    const qs = params.toString();
    return qs ? `/api/costs/export.csv?${qs}` : "/api/costs/export.csv";
  }, [committedFeature, cachedFilter]);

  return (
    <div className="flex flex-col gap-10">
      <header>
        <h1 className="font-serif text-2xl font-semibold tracking-tight text-foreground">
          Costs
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Every LLM call is logged locally. The session resets on app launch
          or on demand.
        </p>
      </header>

      {loadError && (
        <div
          role="alert"
          className="rounded-sm border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {loadError}
        </div>
      )}

      <section>
        <Card>
          <CardHeader>
            <CardTitle>Cost per day (last 30 days)</CardTitle>
            <p className="text-sm text-muted-foreground">
              Daily spend across every feature. Resets are not retroactive —
              past days remain.
            </p>
          </CardHeader>
          <CardContent>
            {dailyLoading || daily === null ? (
              <div className="flex h-[140px] items-center justify-center text-muted-foreground">
                <Spinner size="md" label="Loading daily totals" />
              </div>
            ) : (
              <DailyChart days={daily.days} />
            )}
          </CardContent>
        </Card>
      </section>

      <section className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-start justify-between">
            <div>
              <CardTitle>Session total</CardTitle>
              <p className="text-sm text-muted-foreground">
                Since this app launch, or the last manual reset.
              </p>
            </div>
            <LoadingButton
              type="button"
              variant="outline"
              size="sm"
              loading={resetting}
              onClick={resetSession}
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              Reset session counter
            </LoadingButton>
          </CardHeader>
          <CardContent>
            <p className="font-serif text-4xl font-semibold tracking-tight tabular-nums text-foreground">
              {formatUsd(session?.total_usd ?? 0)}
            </p>
            <p className="mt-1 text-sm tabular-nums text-muted-foreground">
              {formatTokens(sessionTokens)}
              {session && (
                <>
                  {" "}
                  <span className="text-xs text-muted-foreground/80">
                    ({(session.input_tokens ?? 0).toLocaleString()} in ·{" "}
                    {(session.output_tokens ?? 0).toLocaleString()} out)
                  </span>
                </>
              )}
            </p>
            {session?.session_id && (
              <p className="mt-3 font-mono text-[11px] uppercase tracking-wider text-muted-foreground/80">
                Session · {session.session_id.slice(0, 12)}
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Lifetime total</CardTitle>
            <p className="text-sm text-muted-foreground">
              Across every session since install.
            </p>
          </CardHeader>
          <CardContent>
            <p className="font-serif text-4xl font-semibold tracking-tight tabular-nums text-foreground">
              {formatUsd(lifetime?.total_usd ?? 0)}
            </p>
          </CardContent>
        </Card>
      </section>

      <section>
        <Card>
          <CardHeader>
            <CardTitle>Per-feature breakdown</CardTitle>
            <p className="text-sm text-muted-foreground">
              Cost grouped by which feature generated it. Sorted by spend.
            </p>
          </CardHeader>
          <CardContent>
            {featureRows.length === 0 ? (
              <EmptyRow message="No cost events recorded yet." />
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="rule-below text-left text-xs uppercase tracking-[0.08em] text-muted-foreground">
                    <th className="py-2 font-medium">Feature</th>
                    <th className="py-2 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {featureRows.map(([name, usd]) => (
                    <tr key={name} className="rule-below last:border-0">
                      <td className="py-2 font-mono text-[13px] tracking-tight">
                        {name}
                      </td>
                      <td className="py-2 text-right tabular-nums">
                        {formatUsd(usd)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      </section>

      <section>
        <Card>
          <CardHeader className="flex-row items-end justify-between gap-4">
            <div>
              <CardTitle>Recent events</CardTitle>
              <p className="text-sm text-muted-foreground">
                Most recent 50 CostEvents. Filter by feature or cache status.
              </p>
            </div>
            <Button asChild variant="outline" size="sm">
              <a href={csvHref} download>
                <Download className="h-3.5 w-3.5" aria-hidden="true" />
                Export CSV
              </a>
            </Button>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_200px]">
              <div className="flex flex-col gap-1">
                <Label htmlFor="feature-filter">Feature</Label>
                <Input
                  id="feature-filter"
                  value={featureFilter}
                  onChange={(e) => setFeatureFilter(e.target.value)}
                  placeholder="case_brief, irac_grade, emphasis_analysis…"
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="cached-filter">Cached</Label>
                <Select
                  id="cached-filter"
                  value={cachedFilter}
                  onChange={(e) =>
                    setCachedFilter(e.target.value as CachedFilter)
                  }
                >
                  <option value="any">Any</option>
                  <option value="true">Cache hits only</option>
                  <option value="false">Fresh calls only</option>
                </Select>
              </div>
            </div>

            {events && events.events.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="rule-below text-left text-xs uppercase tracking-[0.08em] text-muted-foreground">
                      <th className="py-2 pr-3 font-medium">When</th>
                      <th className="py-2 pr-3 font-medium">Feature</th>
                      <th className="py-2 pr-3 font-medium">Model</th>
                      <th className="py-2 pr-3 text-right font-medium">
                        Tokens
                      </th>
                      <th className="py-2 pr-3 text-right font-medium">
                        Cost
                      </th>
                      <th className="py-2 font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.events.map((ev) => (
                      <EventRow key={ev.id} event={ev} />
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyRow message="No events match the current filter." />
            )}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}

function EventRow({ event }: { event: CostEvent }) {
  const totalTokens = event.input_tokens + event.output_tokens;
  return (
    <tr className="rule-below last:border-0 align-top">
      <td
        className="py-2 pr-3 font-mono text-xs text-muted-foreground"
        title={new Date(event.timestamp).toLocaleString()}
      >
        {formatRelativeTime(event.timestamp)}
      </td>
      <td className="py-2 pr-3">
        <span className="font-mono text-[13px] tracking-tight">
          {event.feature}
        </span>
      </td>
      <td className="py-2 pr-3">
        <span className="font-mono text-xs text-muted-foreground">
          {event.model}
        </span>
      </td>
      <td className="py-2 pr-3 text-right tabular-nums text-muted-foreground">
        <span>{formatTokens(totalTokens)}</span>
        <span className="ml-1.5 text-[11px] text-muted-foreground/70">
          ({event.input_tokens.toLocaleString()}/
          {event.output_tokens.toLocaleString()})
        </span>
      </td>
      <td
        className={cn(
          "py-2 pr-3 text-right tabular-nums font-medium",
          event.cached ? "text-muted-foreground" : "text-foreground",
        )}
      >
        {formatUsd(event.total_cost_usd)}
      </td>
      <td className="py-2">
        {event.cached ? (
          <Badge variant="muted">Cached</Badge>
        ) : (
          <Badge variant="outline">{event.provider}</Badge>
        )}
      </td>
    </tr>
  );
}

function EmptyRow({ message }: { message: string }) {
  return (
    <div className="border border-dashed border-border bg-subtle px-4 py-8 text-center text-sm text-muted-foreground">
      {message}
    </div>
  );
}

/**
 * Pure-SVG bar chart of per-day cost. No chart library — the deps manifest
 * stays tight and the visual stays in our hairline-and-paper aesthetic.
 *
 * Layout: 24px-wide bars with a 4px gap. Y-axis is implicit (no gridlines)
 * but a 1px baseline runs across the bottom. Hairline date labels appear
 * every 5 days so the axis doesn't crowd. Hover gives a textual tooltip
 * via a `<title>` element for screen readers and pointing devices.
 */
function DailyChart({ days }: { days: DailyPoint[] }) {
  if (days.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No cost events recorded in this window.
      </p>
    );
  }
  const values = days.map((d) => Number(d.total_usd) || 0);
  const max = Math.max(...values, 0.0001);
  const total = values.reduce((acc, v) => acc + v, 0);

  const BAR_W = 24;
  const GAP = 4;
  const CHART_H = 140;
  const PAD_TOP = 8;
  const PAD_BOTTOM = 24; // room for date labels
  const innerH = CHART_H - PAD_TOP - PAD_BOTTOM;
  const width = days.length * (BAR_W + GAP);

  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto">
        <svg
          role="img"
          aria-label="Cost per day, last 30 days"
          width={width}
          height={CHART_H}
          viewBox={`0 0 ${width} ${CHART_H}`}
          className="block"
        >
          {/* Baseline. */}
          <line
            x1={0}
            x2={width}
            y1={CHART_H - PAD_BOTTOM}
            y2={CHART_H - PAD_BOTTOM}
            stroke="hsl(var(--border-strong))"
            strokeWidth={1}
          />
          {days.map((d, i) => {
            const v = values[i] ?? 0;
            const h = max === 0 ? 0 : (v / max) * innerH;
            const x = i * (BAR_W + GAP);
            const y = CHART_H - PAD_BOTTOM - h;
            const showLabel = i % 5 === 0 || i === days.length - 1;
            const labelDate = d.date.slice(5); // MM-DD
            return (
              <g key={d.date}>
                <rect
                  x={x}
                  y={y}
                  width={BAR_W}
                  height={Math.max(h, v > 0 ? 1 : 0)}
                  fill="hsl(var(--accent))"
                >
                  <title>
                    {d.date} — {formatUsd(d.total_usd)}
                  </title>
                </rect>
                {showLabel && (
                  <text
                    x={x + BAR_W / 2}
                    y={CHART_H - 8}
                    textAnchor="middle"
                    className="fill-muted-foreground"
                    fontSize={10}
                    style={{ fontFamily: "var(--font-sans)" }}
                  >
                    {labelDate}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      <p className="text-xs tabular-nums text-muted-foreground">
        Total over window:{" "}
        <span className="font-medium text-foreground">{formatUsd(total)}</span>
      </p>
    </div>
  );
}
