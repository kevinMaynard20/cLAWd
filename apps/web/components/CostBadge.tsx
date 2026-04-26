"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { api, ApiError } from "@/lib/api";
import { formatTokens, formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Spec §7.7.5 A — the always-visible cost badge.
 *
 * Shows current session cost + compact token count, clicks through to the
 * full Cost Details panel, and polls `/api/costs/session` every 15 seconds
 * while the window is focused. Polling halts when the tab is hidden
 * (`document.visibilityState === "hidden"`) and resumes on `visibilitychange`.
 *
 * Phase 1: the badge always renders in the default tone. Budget alerts
 * (AMBER at 80%, blocking at 100%) are stubbed here pending the budget
 * plumbing that ships with real LLM calls in Phase 2.
 */

export type CostSession = {
  session_id: string;
  total_usd: string | number;
  input_tokens: number;
  output_tokens: number;
};

const POLL_MS = 15_000;

export function CostBadge({ className }: { className?: string }) {
  const router = useRouter();
  const [data, setData] = React.useState<CostSession | null>(null);
  const [errored, setErrored] = React.useState(false);

  React.useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const isVisible = () =>
      typeof document === "undefined" || document.visibilityState !== "hidden";

    const tick = async () => {
      if (!isVisible()) {
        // Skip polling when the tab is hidden (spec instruction). We'll
        // reschedule from the visibility listener below.
        return;
      }
      try {
        const next = await api.get<CostSession>("/costs/session");
        if (!cancelled) {
          setData(next);
          setErrored(false);
        }
      } catch (err) {
        if (!cancelled) {
          // A failing cost endpoint is non-fatal; show last-known value.
          setErrored(err instanceof ApiError);
        }
      } finally {
        if (!cancelled && isVisible()) {
          timer = setTimeout(tick, POLL_MS);
        }
      }
    };

    const handleVisibility = () => {
      if (isVisible() && timer === null) {
        timer = setTimeout(tick, 0);
      }
    };

    // Kick off the first fetch immediately.
    tick();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  const totalUsd = data?.total_usd ?? 0;
  const tokens =
    (data?.input_tokens ?? 0) + (data?.output_tokens ?? 0);

  const label = `${formatUsd(totalUsd)} this session (${formatTokens(tokens)})`;

  return (
    <button
      type="button"
      onClick={() => router.push("/settings/costs")}
      title={
        errored
          ? "Cost tracker unreachable — showing last known value."
          : "Session cost. Click for details."
      }
      aria-label={label}
      className={cn(
        "group inline-flex h-8 items-center gap-2 rounded-sm border border-border-strong bg-card px-2.5 text-xs font-medium tracking-tight text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          errored ? "bg-warning" : "bg-accent",
        )}
      />
      <span className="tabular-nums">{formatUsd(totalUsd)}</span>
      <span className="text-muted-foreground">this session</span>
      <span className="text-muted-foreground" aria-hidden="true">
        ·
      </span>
      <span className="tabular-nums text-muted-foreground">
        {formatTokens(tokens)}
      </span>
    </button>
  );
}

export default CostBadge;
