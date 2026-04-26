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

// Routes that bypass the gate entirely. The first-run page IS the gate's
// destination, so it must render unconditionally. The /help/* tree is the
// in-app docs surface (e.g. the "How do I get this?" link from the API
// key field) — those pages have to work BEFORE the user has saved a key,
// otherwise the link snaps back to /first-run mid-tutorial.
const PUBLIC_ROUTES: readonly string[] = ["/first-run", "/help"];

function isPublic(pathname: string): boolean {
  return PUBLIC_ROUTES.some((p) => pathname === p || pathname.startsWith(p + "/"));
}

export function FirstRunGate({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const [ready, setReady] = React.useState(isPublic(pathname));

  React.useEffect(() => {
    let cancelled = false;

    // Public routes (first-run, help) render regardless of gate state.
    if (isPublic(pathname)) {
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
