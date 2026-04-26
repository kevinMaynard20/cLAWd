"use client";

import * as React from "react";

import { Input } from "@/components/ui/input";

/**
 * Number input that doesn't fight the user mid-edit.
 *
 * The naive pattern
 *   onChange={(e) => setValue(Math.max(min, Math.min(max, Number(e.target.value) || fallback)))}
 * has a UX bug: when the field is empty (because the user is *about* to
 * retype), `Number("")` is 0 (falsy), the `|| fallback` snaps it back to
 * `min`/`max`, and the parent's controlled `value` re-renders the input with
 * the clamped digit before the user can finish typing. Result: deleting "1"
 * to type "600" feels like the "1" is sticky.
 *
 * This component:
 *  - Holds the raw text in local state, so empty / mid-edit values are kept
 *    exactly as typed.
 *  - Pushes a clamped int up to the parent ONLY when the field has a real
 *    finite number (so live downstream effects still work as you type).
 *  - Clamps to [min, max] on blur and Enter (final commit).
 *  - Reflects external resets ("Reset" button etc.) back into the draft.
 */
export function PageNumberInput({
  id,
  value,
  onCommit,
  min,
  max,
  fallback,
  className,
}: {
  id?: string;
  value: number | null;
  onCommit: (next: number) => void;
  min: number;
  max: number;
  /** Used when the field is left blank or contains a non-numeric value. */
  fallback: number;
  className?: string;
}) {
  const [draft, setDraft] = React.useState<string>(
    value === null ? "" : String(value),
  );

  // Reflect external resets (parent set state from a "Reset" button etc.)
  // back into the draft, but only when the parent value diverges from what
  // we'd already pushed up — otherwise we clobber the user's mid-edit text.
  React.useEffect(() => {
    if (value === null) return;
    if (Number(draft) !== value) setDraft(String(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const commit = (raw: string) => {
    const trimmed = raw.trim();
    if (trimmed === "") {
      onCommit(fallback);
      setDraft(String(fallback));
      return;
    }
    const n = Number(trimmed);
    if (!Number.isFinite(n)) {
      onCommit(fallback);
      setDraft(String(fallback));
      return;
    }
    const clamped = Math.max(min, Math.min(max, Math.trunc(n)));
    onCommit(clamped);
    setDraft(String(clamped));
  };

  return (
    <Input
      id={id}
      type="number"
      inputMode="numeric"
      min={min}
      max={max}
      value={draft}
      onChange={(e) => {
        const next = e.target.value;
        // Reflect what the user typed as-is — empties don't snap back.
        setDraft(next);
        // Only commit upstream when the value parses; the parent keeps its
        // last good number across mid-edit empty states.
        if (next.trim() !== "") {
          const n = Number(next);
          if (Number.isFinite(n)) {
            const clamped = Math.max(min, Math.min(max, Math.trunc(n)));
            onCommit(clamped);
          }
        }
      }}
      onBlur={(e) => commit(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit((e.target as HTMLInputElement).value);
      }}
      className={className ?? "h-8 w-24"}
    />
  );
}
