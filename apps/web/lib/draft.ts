"use client";

import * as React from "react";

/**
 * localStorage-backed draft hook for long-form text inputs (the practice
 * answer workspace primarily). Drafts are namespaced by key so different
 * questions don't clobber each other.
 *
 * SSR-safe: the initial render returns the supplied default; we hydrate from
 * storage in an effect to avoid the "client-only API used during render"
 * footgun in Next.js App Router.
 */
export function useDraft(
  key: string,
  defaultValue: string = "",
): [string, (next: string) => void, () => void] {
  const [value, setValue] = React.useState<string>(defaultValue);
  const hydratedRef = React.useRef(false);

  React.useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const raw = window.localStorage.getItem(key);
      if (raw !== null) setValue(raw);
    } catch {
      // Storage may be disabled (Safari private mode, etc.) — fall through.
    } finally {
      hydratedRef.current = true;
    }
  }, [key]);

  React.useEffect(() => {
    if (!hydratedRef.current) return;
    if (typeof window === "undefined") return;
    try {
      if (value === "") window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch {
      // Quota exceeded / disabled — best-effort.
    }
  }, [key, value]);

  const clear = React.useCallback(() => {
    setValue("");
    if (typeof window !== "undefined") {
      try {
        window.localStorage.removeItem(key);
      } catch {
        // ignore
      }
    }
  }, [key]);

  return [value, setValue, clear];
}
