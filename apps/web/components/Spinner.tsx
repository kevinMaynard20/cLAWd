"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Inline rotating ring driven entirely by Tailwind's `animate-spin`. Inherits
 * its color from the surrounding text color, so callers can drop one inside
 * any container without threading theme tokens through.
 *
 * The ring is built from a 1px border whose top edge is transparent — this
 * is the cheapest way to draw a CSS spinner without an SVG dependency.
 */

export type SpinnerProps = {
  size?: "sm" | "md" | "lg";
  /** Visually hidden text exposed to screen readers. */
  label?: string;
  className?: string;
};

const SIZE_CLASS: Record<NonNullable<SpinnerProps["size"]>, string> = {
  sm: "h-[14px] w-[14px] border",
  md: "h-[20px] w-[20px] border-2",
  lg: "h-[28px] w-[28px] border-2",
};

export function Spinner({
  size = "md",
  label,
  className,
}: SpinnerProps) {
  return (
    <span
      role="status"
      aria-live="polite"
      className={cn("inline-flex items-center", className)}
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-block animate-spin rounded-full border-current border-t-transparent",
          SIZE_CLASS[size],
        )}
      />
      {label ? <span className="sr-only">{label}</span> : null}
    </span>
  );
}

export default Spinner;
