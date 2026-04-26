"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Plain native <select> styled to match the form controls. We don't need the
 * full Radix Select primitive for the handful of filter dropdowns we render
 * in Phase 1; a native element is faster, accessible by default, and keeps
 * the dep footprint small.
 */

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, children, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        "flex h-9 w-full appearance-none rounded-sm border border-input bg-card px-3 py-1.5 pr-8 text-sm tracking-tight text-foreground shadow-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50",
        "bg-[image:linear-gradient(45deg,transparent_50%,hsl(var(--muted-foreground))_50%),linear-gradient(135deg,hsl(var(--muted-foreground))_50%,transparent_50%)] bg-[position:calc(100%-14px)_center,calc(100%-9px)_center] bg-[size:5px_5px] bg-no-repeat",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  ),
);
Select.displayName = "Select";

export { Select };
