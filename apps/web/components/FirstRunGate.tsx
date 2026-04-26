"use client";

import { usePathname, useRouter } from "next/navigation";
import * as React from "react";

import { api } from "@/lib/api";

/**
 * Client-side gate for first-run (spec §7.7.1). On mount, ask the backend
 * whether LLM features are enabled. If not, send the user to `/first-run`.
 *
 * Chose client-side over RSC middleware because (a) the FastAPI proxy is a
 * client-only concept from Next's PoV during RSC, (b) it keeps the setup
 * code local and reviewable, and (c) Phase 1 only needs soft gating.
 */

export function FirstRunGate({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const [ready, setReady] = React.useState(pathname.startsWith("/first-run"));

  React.useEffect(() => {
    let cancelled = false;

    // The first-run page itself must always render, regardless of gate state.
    if (pathname.startsWith("/first-run")) {
      setReady(true);
      return;
    }

    (async () => {
      try {
        const res = await api.get<{ llm_enabled: boolean; reason: string }>(
          "/credentials/gate",
        );
        if (cancelled) return;
        if (!res.llm_enabled) {
          router.replace("/first-run");
          return;
        }
        setReady(true);
      } catch {
        // If the API is unreachable, assume we need setup. Better to surface
        // the wall than to let the user click around dead features.
        if (!cancelled) router.replace("/first-run");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [pathname, router]);

  if (!ready) {
    return (
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 bg-background"
      />
    );
  }
  return <>{children}</>;
}

export default FirstRunGate;
