/**
 * Display formatters used everywhere the UI surfaces numbers or timestamps.
 *
 * Keep these pure and dependency-free so they can be reused from both client
 * and server components and tested without DOM plumbing.
 */

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const COMPACT_TOKENS = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/**
 * Format a dollar amount. Accepts either a JS number or a decimal string (the
 * FastAPI backend serializes Decimals as strings to preserve precision).
 *
 * Values below one cent render as "$0.00" — we do not surface sub-cent
 * precision in the UI; that's reserved for the CSV export.
 */
export function formatUsd(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "$0.00";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "$0.00";
  return USD.format(n);
}

/**
 * Render a raw token count as, e.g., `"142K tokens"` using compact notation.
 * Non-finite or negative numbers collapse to "0 tokens" rather than leak NaN
 * into the UI.
 */
export function formatTokens(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) {
    return "0 tokens";
  }
  return `${COMPACT_TOKENS.format(n)} tokens`;
}

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] as const;

/**
 * Render a byte count using binary IEC units (KiB / MiB / …). Anything below
 * 1 KiB is shown as a whole-byte count to avoid lying about precision.
 *
 *   1024            → "1.0 KiB"
 *   1500000         → "1.4 MiB"
 *   500             → "500 B"
 *   null/undefined  → "0 B"
 */
export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) {
    return "0 B";
  }
  if (n < 1024) {
    // Integer bytes only at this scale.
    return `${Math.round(n)} B`;
  }
  let unitIndex = 0;
  let value = n;
  while (value >= 1024 && unitIndex < BYTE_UNITS.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${BYTE_UNITS[unitIndex]}`;
}

/**
 * Render an ISO timestamp as a short relative string: "just now", "2m ago",
 * "3h ago", "4d ago", or a short absolute date for anything older than ~30d.
 *
 * Returns an empty string for unparseable input — callers can fall back to
 * the original ISO if they want.
 */
export function formatRelativeTime(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  const deltaMs = now.getTime() - then.getTime();
  const sec = Math.round(deltaMs / 1000);
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day}d ago`;
  return then.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
