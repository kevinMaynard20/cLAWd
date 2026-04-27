"use client";

import { usePathname } from "next/navigation";
import dynamic from "next/dynamic";
import * as React from "react";

import { Spinner } from "@/components/Spinner";

/**
 * Why this exists: Next.js's `output: "export"` requires
 * `dynamicParams: false` on every dynamic route segment, and
 * `generateStaticParams` only emits a single `__shell__` placeholder for
 * IDs we can't enumerate at build time (corpora, books, blocks, artifacts,
 * transcripts). Tauri's WKURLSchemeHandler asset resolver, when it can't
 * find a path on disk, falls back through `<path>.html`,
 * `<path>/index.html`, and finally `index.html` — so navigating to a real
 * id like `/corpora/<id>` lands on the dashboard's HTML, with the dashboard
 * component rendering. From the user's perspective: "every link sends me
 * back to the home page."
 *
 * The fix lives here: the dashboard page renders this component, which
 * reads the pathname at runtime and dynamically loads the matching shell
 * Client Component when the URL doesn't actually correspond to the
 * dashboard. The shells already extract their ids from props.params, so we
 * synthesise the params Promise from the URL.
 *
 * Why this beats the alternatives:
 * - Refactoring every dynamic route to query params (`/corpora?id=…`)
 *   would touch ~16 Link sites across 10 files plus the routing of every
 *   downstream feature. Disruptive.
 * - Overriding Tauri's `tauri://` scheme handler in Rust would require
 *   re-implementing CSP injection and MIME sniffing and tracking the
 *   `tauri-utils::Assets` interface. Brittle.
 * - This component is purely client-side and frontend-only.
 */

// Each shell ClientPage declares its own param shape (`{ corpusId: string }`,
// `{ blockId: string }`, etc.) so a single typed union doesn't cover all
// six cleanly. The runtime match enforces the shapes line up; TypeScript
// sees them as structurally distinct and would otherwise reject the
// assignment. `unknown` here is the narrowest type that lets the union
// hold and is cast at the render site.
type ShellComponent = React.ComponentType<{ params: Promise<unknown> }>;
type Match = {
  Component: ShellComponent;
  params: Record<string, string>;
};

// Lazy-imported so the dashboard's initial paint doesn't pay the cost of
// every shell's chunk. Each `dynamic` call becomes its own webpack chunk in
// the static export under `out/_next/static/chunks/`.
const CorpusPage = dynamic(
  () => import("@/app/corpora/[corpusId]/ClientPage"),
  { loading: () => <RouteLoading label="Loading corpus" /> },
) as unknown as ShellComponent;
const CorpusBookPage = dynamic(
  () => import("@/app/corpora/[corpusId]/books/[bookId]/ClientPage"),
  { loading: () => <RouteLoading label="Loading book" /> },
) as unknown as ShellComponent;
const ColdCallPage = dynamic(
  () => import("@/app/cold-call/[blockId]/ClientPage"),
  { loading: () => <RouteLoading label="Loading cold call" /> },
) as unknown as ShellComponent;
const SocraticPage = dynamic(
  () => import("@/app/socratic/[blockId]/ClientPage"),
  { loading: () => <RouteLoading label="Loading drill" /> },
) as unknown as ShellComponent;
const ArtifactPage = dynamic(
  () => import("@/app/artifacts/[artifactId]/ClientPage"),
  { loading: () => <RouteLoading label="Loading artifact" /> },
) as unknown as ShellComponent;
const TranscriptEmphasisPage = dynamic(
  () => import("@/app/transcripts/[transcriptId]/emphasis/ClientPage"),
  { loading: () => <RouteLoading label="Loading emphasis" /> },
) as unknown as ShellComponent;

function matchRoute(pathname: string): Match | null {
  // Order matters — more specific patterns first.
  let m: RegExpMatchArray | null;

  m = pathname.match(/^\/corpora\/([^/]+)\/books\/([^/]+)\/?$/);
  if (m) {
    return {
      Component: CorpusBookPage,
      params: { corpusId: m[1], bookId: m[2] },
    };
  }

  m = pathname.match(/^\/transcripts\/([^/]+)\/emphasis\/?$/);
  if (m) {
    return { Component: TranscriptEmphasisPage, params: { transcriptId: m[1] } };
  }

  m = pathname.match(/^\/corpora\/([^/]+)\/?$/);
  if (m) {
    return { Component: CorpusPage, params: { corpusId: m[1] } };
  }

  m = pathname.match(/^\/cold-call\/([^/]+)\/?$/);
  if (m && m[1] !== "random") {
    return { Component: ColdCallPage, params: { blockId: m[1] } };
  }

  m = pathname.match(/^\/socratic\/([^/]+)\/?$/);
  if (m) {
    return { Component: SocraticPage, params: { blockId: m[1] } };
  }

  m = pathname.match(/^\/artifacts\/([^/]+)\/?$/);
  if (m) {
    return { Component: ArtifactPage, params: { artifactId: m[1] } };
  }

  return null;
}

function RouteLoading({ label }: { label: string }) {
  return (
    <main className="mx-auto flex w-full max-w-3xl items-center gap-2 px-6 py-16 text-sm text-muted-foreground">
      <Spinner size="sm" /> {label}…
    </main>
  );
}

/**
 * Render the matching shell for the current URL, or fall through to
 * `children` when the URL is the actual dashboard root. Mounted by
 * `app/page.tsx`.
 */
export function SpaFallbackRouter({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname() ?? "/";

  // Memoize the route match + the params Promise on the pathname. ClientPages
  // call `React.use(props.params)` and a new Promise reference on every
  // render keeps `React.use` suspending, which manifests as either an
  // unmount/remount loop (the page's useEffect fires its cleanup function
  // immediately, leaving fetched state stuck in `error: "Could not load …"`)
  // or visible flicker. Memoizing both gives the shells a stable Promise.
  const match = React.useMemo(() => matchRoute(pathname), [pathname]);
  const paramsPromise = React.useMemo(
    () => (match ? Promise.resolve(match.params) : null),
    // Match's `params` object is stable for a given pathname (matchRoute is
    // pure), so depending on `match` is sufficient here.
    [match],
  );

  // Real dashboard URL — render the dashboard.
  if (pathname === "/" || pathname === "") {
    return <>{children}</>;
  }

  if (!match || !paramsPromise) {
    // Path didn't match any known dynamic route. Tauri's asset fallback
    // brought us here in error — render the dashboard as a graceful
    // degradation rather than a blank screen.
    return <>{children}</>;
  }

  const Component = match.Component;
  return <Component params={paramsPromise} />;
}

export default SpaFallbackRouter;
