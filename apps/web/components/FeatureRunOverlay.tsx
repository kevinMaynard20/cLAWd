"use client";

import * as React from "react";

import { Spinner } from "@/components/Spinner";

/**
 * Why this exists: every "long" feature on the book page (Brief, Flashcards,
 * MCQ) hits the LLM and takes 30s–2min. The previous UX was `setBusy("brief")`
 * → button text becomes "…" → silence. The user had no signal whether the
 * call was progressing or stuck. Worse, in the Tauri WKWebView,
 * `window.alert()` is a no-op (a known WKWebView quirk — JS dialogs are
 * disabled by default), so when a request errored the user just saw the
 * button reset to its idle label and assumed nothing happened.
 *
 * This overlay fixes both problems with one component:
 *   - Full-page sticky overlay, dimmed background, can't be missed.
 *   - "Generating <label>…" with a live elapsed-time counter so the user
 *     knows it's progressing.
 *   - Subcopy explains the typical duration so 90s doesn't feel like
 *     "stuck."
 *
 * Inline errors are handled separately by the caller — see the
 * `error`/`onDismissError` props which render a banner inside the overlay
 * shell rather than firing a system dialog the WebView eats.
 */

export type FeatureRunStatus = {
  label: string;
  startedAt: number;
} | null;

export function FeatureRunOverlay({
  status,
  error,
  onDismissError,
}: {
  status: FeatureRunStatus;
  error: string | null;
  onDismissError: () => void;
}) {
  // Render-only when there's something to show. Important: hooks must run
  // unconditionally, so the timer effect lives above this guard.
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!status) return;
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [status]);

  if (!status && !error) return null;

  return (
    <div
      role="dialog"
      aria-live="polite"
      className="fixed inset-0 z-40 flex items-center justify-center bg-foreground/30"
    >
      <div className="mx-4 w-full max-w-md border border-border-strong bg-card p-6 shadow-lg">
        {status && (
          <div className="flex flex-col items-center gap-3 text-center">
            <Spinner size="md" />
            <p className="font-serif text-lg font-semibold tracking-tight">
              Generating {status.label}…
            </p>
            <p className="text-sm tabular-nums text-muted-foreground">
              {formatElapsed(now - status.startedAt)} elapsed
            </p>
            <p className="mt-1 max-w-prose text-xs text-muted-foreground">
              Long-form LLM calls run 30 seconds to 2 minutes. We&apos;ll
              navigate to the result as soon as it&apos;s ready.
            </p>
          </div>
        )}
        {error && (
          <div className="flex flex-col gap-3">
            <p className="font-serif text-lg font-semibold tracking-tight text-destructive">
              {status ? "Still working — last error:" : "Generation failed"}
            </p>
            <p className="rounded-sm border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </p>
            <button
              type="button"
              onClick={onDismissError}
              className="self-end text-xs uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground"
            >
              Dismiss
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m === 0) return `${s}s`;
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}

export default FeatureRunOverlay;
