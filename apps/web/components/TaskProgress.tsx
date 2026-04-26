"use client";

import * as React from "react";

import { Spinner } from "@/components/Spinner";
import { Badge } from "@/components/ui/badge";
import { api, ApiError } from "@/lib/api";
import { formatTokens } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Shared progress widget for any background task (spec §4.x async pipelines).
 *
 * Polls `GET /api/tasks/{taskId}` while status is `pending` or `running`,
 * pauses when the tab is hidden (page-visibility API), and stops cleanly on
 * unmount. The polling cadence defaults to 1.5 s — fast enough to feel live
 * for a 1400-page casebook ingestion but slow enough not to thrash SQLite.
 *
 * Result fields aren't strongly typed at this layer — different task kinds
 * surface different shapes. We render a small "Done" line with token-style
 * counts pulled out by name, and pass the full payload up through
 * `onCompleted` so the parent decides what to do with it.
 */

type TaskStatus = "pending" | "running" | "completed" | "failed";

type TaskDTO = {
  id: string;
  kind: string;
  status: TaskStatus;
  progress_step: string;
  progress_pct: number;
  corpus_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  result: Record<string, unknown>;
};

export type TaskProgressProps = {
  taskId: string;
  onCompleted?: (result: Record<string, unknown>) => void;
  onFailed?: (error: string) => void;
  pollIntervalMs?: number;
  className?: string;
};

const TERMINAL: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "completed",
  "failed",
]);

function statusBadge(status: TaskStatus): React.ReactNode {
  if (status === "pending") return <Badge variant="muted">Pending</Badge>;
  if (status === "running") return <Badge variant="accent">Running</Badge>;
  if (status === "completed") return <Badge variant="success">Completed</Badge>;
  return <Badge variant="destructive">Failed</Badge>;
}

function pickNumber(
  obj: Record<string, unknown>,
  key: string,
): number | null {
  const v = obj[key];
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const parsed = Number(v);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function pickString(
  obj: Record<string, unknown>,
  key: string,
): string | null {
  const v = obj[key];
  return typeof v === "string" && v.length > 0 ? v : null;
}

export function TaskProgress({
  taskId,
  onCompleted,
  onFailed,
  pollIntervalMs = 1500,
  className,
}: TaskProgressProps) {
  const [task, setTask] = React.useState<TaskDTO | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  // Latch fired callbacks so we don't re-fire if the component re-renders
  // while still pointing at the same terminal task.
  const firedRef = React.useRef<{ done: boolean; failed: boolean }>({
    done: false,
    failed: false,
  });
  // Use refs for the callbacks so the polling effect doesn't tear down on
  // every render of the parent.
  const onCompletedRef = React.useRef(onCompleted);
  const onFailedRef = React.useRef(onFailed);
  React.useEffect(() => {
    onCompletedRef.current = onCompleted;
  }, [onCompleted]);
  React.useEffect(() => {
    onFailedRef.current = onFailed;
  }, [onFailed]);

  React.useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    firedRef.current = { done: false, failed: false };

    const isVisible = () =>
      typeof document === "undefined" ||
      document.visibilityState !== "hidden";

    const schedule = (delayMs: number) => {
      if (cancelled) return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(tick, delayMs);
    };

    const tick = async () => {
      if (cancelled) return;
      if (!isVisible()) {
        // Don't poll while hidden; the visibility listener wakes us up.
        timer = null;
        return;
      }
      try {
        const next = await api.get<TaskDTO>(`/tasks/${taskId}`);
        if (cancelled) return;
        setTask(next);
        setError(null);
        if (next.status === "completed" && !firedRef.current.done) {
          firedRef.current.done = true;
          onCompletedRef.current?.(next.result ?? {});
        }
        if (next.status === "failed" && !firedRef.current.failed) {
          firedRef.current.failed = true;
          onFailedRef.current?.(next.error ?? "Task failed.");
        }
        if (TERMINAL.has(next.status)) {
          timer = null;
          return;
        }
        schedule(pollIntervalMs);
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiError ? err.message : "Could not reach the task.";
        setError(message);
        // Keep trying on transient errors — the backend may briefly 5xx.
        schedule(pollIntervalMs);
      }
    };

    const handleVisibility = () => {
      if (cancelled) return;
      if (
        isVisible() &&
        timer === null &&
        (task === null || !TERMINAL.has(task.status))
      ) {
        // Resume immediately on focus.
        schedule(0);
      }
    };

    schedule(0);
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", handleVisibility);
    }

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", handleVisibility);
      }
    };
    // We intentionally restart polling only when taskId or interval changes.
    // task/error are managed inside; including them would loop forever.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, pollIntervalMs]);

  const pct = task ? Math.max(0, Math.min(1, task.progress_pct)) : 0;
  const widthPct = `${(pct * 100).toFixed(1)}%`;
  const status: TaskStatus = task?.status ?? "pending";
  const stepLabel = task?.progress_step?.trim() || "Queued";

  const details: Array<{ key: string; label: string }> = [];
  if (task?.result) {
    const inputTokens = pickNumber(task.result, "input_tokens");
    const outputTokens = pickNumber(task.result, "output_tokens");
    const blocks = pickNumber(task.result, "block_count");
    const pages = pickNumber(task.result, "page_count");
    const totalTokens =
      (inputTokens ?? 0) + (outputTokens ?? 0);
    if (totalTokens > 0) {
      details.push({ key: "tokens", label: formatTokens(totalTokens) });
    }
    if (typeof blocks === "number") {
      details.push({
        key: "blocks",
        label: `${blocks.toLocaleString()} blocks`,
      });
    }
    if (typeof pages === "number") {
      details.push({
        key: "pages",
        label: `${pages.toLocaleString()} pages`,
      });
    }
  }

  return (
    <div
      className={cn(
        "flex flex-col gap-3 border border-border bg-card p-4",
        className,
      )}
      data-testid="task-progress"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {!TERMINAL.has(status) && (
            <span className="text-muted-foreground" aria-hidden="true">
              <Spinner size="sm" />
            </span>
          )}
          <p className="truncate text-sm tracking-tight text-foreground">
            {stepLabel}
          </p>
        </div>
        {statusBadge(status)}
      </div>

      <div
        className="h-2 w-full overflow-hidden bg-muted"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct * 100)}
      >
        <div
          className="h-full bg-accent transition-[width] duration-500 ease-out"
          style={{ width: widthPct }}
        />
      </div>

      <div className="flex items-center justify-between text-xs tabular-nums text-muted-foreground">
        <span>{Math.round(pct * 100)}%</span>
        <span className="font-mono text-[11px] uppercase tracking-[0.08em]">
          task {taskId.slice(0, 8)}
        </span>
      </div>

      {status === "completed" && (
        <p className="text-xs tracking-tight text-success">
          Done.
          {details.length > 0 && (
            <span className="ml-2 text-muted-foreground">
              {details.map((d, i) => (
                <span key={d.key}>
                  {i > 0 ? " · " : ""}
                  {d.label}
                </span>
              ))}
            </span>
          )}
        </p>
      )}

      {status === "failed" && (
        <p
          role="alert"
          className="border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          {task?.error ?? "Task failed."}
        </p>
      )}

      {error && status !== "failed" && (
        <p className="text-xs text-warning">{error}</p>
      )}
    </div>
  );
}

export default TaskProgress;
