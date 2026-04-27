"use client";

import Link from "next/link";
import * as React from "react";
import { useEffect, useState } from "react";

import { SpaFallbackRouter } from "@/components/SpaFallbackRouter";
import { Spinner } from "@/components/Spinner";
import { api, ApiError } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

/**
 * Dashboard. Shows what the user has ingested (corpora + counts), points to
 * the settings pages already built, and links to the OpenAPI docs so every
 * feature that has a backend endpoint but no dedicated UI is still reachable.
 *
 * This is intentionally a thin dashboard, not a full app shell — the feature-
 * specific UI surfaces (reading view, case-brief viewer, search page) are
 * deferred to a future UI session. See SPEC_QUESTIONS.md Q52.
 */

type CorpusSummary = {
  id: string;
  name: string;
  course: string;
  professor_name: string | null;
  school: string | null;
  created_at: string;
  book_count: number;
  transcript_count: number;
  artifact_count: number;
  professor_profile_count: number;
};

export default function Home() {
  // Tauri's WKURLSchemeHandler falls back to `index.html` (this page) for
  // any URL that doesn't match a built file — including every dynamic
  // route like `/corpora/<id>` or `/cold-call/<id>` (those only have a
  // `__shell__.html` placeholder that's not addressable by real id). The
  // SpaFallbackRouter inspects the pathname at runtime and renders the
  // matching shell ClientPage; only when the pathname is actually `/` do
  // we fall through to render the real dashboard below.
  return (
    <SpaFallbackRouter>
      <Dashboard />
    </SpaFallbackRouter>
  );
}

function Dashboard() {
  const [corpora, setCorpora] = useState<CorpusSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const data = await api.get<CorpusSummary[]>("/corpora");
        if (!cancelled) setCorpora(data);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError) setError(e.message);
        else setError("Failed to load corpora.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="mx-auto w-full max-w-4xl px-6 py-12">
      <p className="font-serif text-xs uppercase tracking-[0.18em] text-muted-foreground">
        cLAWd · Study system
      </p>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        Dashboard
      </h1>
      <p className="mt-3 max-w-prose font-serif text-base leading-relaxed text-foreground/90">
        Backend for every phase (ingest, retrieve, generate, verify, all nine
        features) is live. Dedicated reading and grading UI surfaces ship
        later — for now, see <code>/docs</code> for the full API.
      </p>

      <GetStartedSection corpora={corpora} loading={loading} />

      <CorporaSection corpora={corpora} error={error} loading={loading} />

      <section className="mt-12 border-t border-border pt-8">
        <h2 className="font-serif text-xl font-semibold tracking-tight">
          Controls
        </h2>
        <div className="mt-4 grid grid-cols-1 gap-0 border-t border-border md:grid-cols-2">
          <DashLink
            href="/settings/api-keys"
            label="API keys"
            detail="Rotate, test, or clear your Anthropic and Voyage keys."
          />
          <DashLink
            href="/settings/costs"
            label="Cost details"
            detail="Session + lifetime totals, per-feature breakdown, CSV export."
          />
          <DashLink
            href="/settings/models"
            label="Model defaults"
            detail="Which Claude model each feature uses by default."
          />
          <DashLink
            href="/api/docs"
            label="API reference"
            detail="Interactive Swagger UI for every feature endpoint."
            external
          />
        </div>
      </section>

      <section className="mt-12 border-t border-border pt-8">
        <h2 className="font-serif text-xl font-semibold tracking-tight">
          Study features
        </h2>
        <p className="mt-2 max-w-prose text-sm text-muted-foreground">
          Open a corpus above for the per-corpus dashboard (cases, briefs,
          transcripts, profiles). The shortcuts below land on the global
          builders — they ask which corpus to use first.
        </p>
        <div className="mt-4 grid grid-cols-1 gap-0 border-t border-border md:grid-cols-2">
          <DashLink
            href="/practice"
            label="IRAC practice"
            detail="Past exam + memo, fresh hypo, or paste a question. Inline graded feedback."
          />
          <DashLink
            href="/cold-call/random"
            label="Cold call (random)"
            detail="Pick a book + page range; server picks a case; drill under pressure."
          />
          <DashLink
            href="/synthesis"
            label="Multi-case synthesis"
            detail="Combine briefed cases into a doctrinal essay across one topic."
          />
          <DashLink
            href="/attack-sheets"
            label="Attack sheets"
            detail="One-page exam attack sheets per topic; print-friendly layout."
          />
          <DashLink
            href="/outline"
            label="Outline"
            detail="Hierarchical outline of every brief, transcript, and synthesis."
          />
          <DashLink
            href="/upload"
            label="Upload"
            detail="Add casebooks, transcripts, syllabi, or past exams."
          />
        </div>
      </section>
    </main>
  );
}

/**
 * Onboarding panel. Always visible, but auto-expands when the user has no
 * books yet (first-run experience). Spec'd order: textbook before anything
 * else, since briefs / drills / attack sheets / outlines all depend on the
 * casebook's case-opinion blocks.
 */
function GetStartedSection({
  corpora,
  loading,
}: {
  corpora: CorpusSummary[] | null;
  loading: boolean;
}) {
  const [open, setOpen] = React.useState<boolean | null>(null);
  const hasBooks =
    corpora !== null && corpora.some((c) => (c.book_count ?? 0) > 0);
  // First-paint default: expanded if we already know there are no books;
  // collapsed once books exist. The user can still toggle.
  const effectiveOpen =
    open !== null ? open : !loading && corpora !== null && !hasBooks;

  return (
    <section className="mt-8 border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen(!effectiveOpen)}
        className="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-muted"
        aria-expanded={effectiveOpen}
      >
        <div>
          <p className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">
            Getting started
          </p>
          <p className="mt-0.5 font-serif text-lg font-semibold">
            {hasBooks
              ? "Quick reference — the order things go in"
              : "Start here — upload a casebook first"}
          </p>
        </div>
        <span className="text-xs uppercase tracking-[0.12em] text-accent">
          {effectiveOpen ? "Hide" : "Show"}
        </span>
      </button>

      {effectiveOpen && (
        <ol className="space-y-4 border-t border-border px-4 py-4 font-serif text-sm leading-relaxed">
          <Step
            n={1}
            title="Upload your casebook"
            href="/upload"
            cta="Upload a PDF →"
          >
            Always do this first. Briefs, drills, attack sheets, and outlines
            all read from your casebook&apos;s case-opinion blocks. The
            ingestion pipeline extracts each printed page, classifies blocks
            (case opinions, numbered notes, headers), and indexes them by
            source-page number.
          </Step>

          <Step n={2} title="Open the casebook in the corpus dashboard">
            Click your corpus card below to enter its dashboard. Open the
            book on the <strong>Books</strong> tab. You&apos;ll see every
            case the segmenter detected, with one-click <strong>Brief</strong>{" "}
            / <strong>Drill</strong> / <strong>Cold-call</strong> buttons per
            case. The page-range slider at the top filters to whatever you
            were assigned this week.
          </Step>

          <Step n={3} title="Brief the cases you need">
            Click <strong>Brief</strong> next to a case to generate a FIRAC+
            brief. Briefs accumulate in the corpus&apos;s <strong>Briefs</strong>{" "}
            tab and feed every downstream feature. The model uses the
            casebook text by default; for cases with thin or missing text it
            falls back to general legal knowledge (badged as such).
          </Step>

          <Step n={4} title="Now the synthesis tools light up">
            Once you have <strong>2+ briefs</strong>, build a multi-case{" "}
            <strong>synthesis</strong>, a per-topic <strong>attack sheet</strong>,
            or a hierarchical <strong>outline</strong> across the whole
            corpus. The shortcuts under <em>Study features</em> below open
            those builders — each one has a brief picker that lists what
            you&apos;ve already produced.
          </Step>

          <Step n={5} title="Optional: transcripts and past exams">
            <strong>Transcripts</strong> (paste or upload Gemini class
            recordings on the Upload page) become emphasis maps —
            ranked &quot;what the professor actually emphasized&quot; signals
            that bias attack-sheet generation. <strong>Past exams</strong>{" "}
            (with grader memos when available) feed the IRAC practice wizard
            for graded feedback against the actual rubric.
          </Step>
        </ol>
      )}
    </section>
  );
}

function Step({
  n,
  title,
  children,
  href,
  cta,
}: {
  n: number;
  title: string;
  children: React.ReactNode;
  href?: string;
  cta?: string;
}) {
  return (
    <li className="grid grid-cols-[2rem_1fr] gap-3">
      <span className="font-serif text-xl font-semibold tabular-nums text-accent">
        {n}.
      </span>
      <div>
        <p className="font-semibold">{title}</p>
        <p className="mt-1 text-foreground/85">{children}</p>
        {href && cta && (
          <Link
            href={href}
            className="law-link mt-2 inline-block text-xs uppercase tracking-[0.12em] text-accent hover:underline"
          >
            {cta}
          </Link>
        )}
      </div>
    </li>
  );
}

function CorporaSection({
  corpora,
  error,
  loading,
}: {
  corpora: CorpusSummary[] | null;
  error: string | null;
  loading: boolean;
}) {
  if (error) {
    return (
      <section className="mt-10 rounded-sm border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        Couldn&rsquo;t load corpora: {error}
      </section>
    );
  }
  if (loading || corpora === null) {
    return (
      <section className="mt-10 flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner size="sm" label="Loading corpora" />
        <span>Loading corpora</span>
      </section>
    );
  }
  if (corpora.length === 0) {
    return (
      <section className="mt-10 rounded-sm border border-border bg-muted/30 px-4 py-4 font-serif text-sm">
        No corpora yet. Create one by POSTing to{" "}
        <code>/api/corpora</code> with <code>{"{name, course}"}</code>, then
        ingest a book with <code>POST /api/ingest/book</code>.
      </section>
    );
  }
  return (
    <section className="mt-10">
      <h2 className="font-serif text-xl font-semibold tracking-tight">
        Corpora
      </h2>
      <div className="mt-4 grid grid-cols-1 gap-0 border-t border-border">
        {corpora.map((c) => (
          <Link
            key={c.id}
            href={`/corpora/${c.id}`}
            className="group grid grid-cols-[1fr_auto] border-b border-border px-4 py-4 transition-colors hover:bg-muted"
          >
            <div>
              <p className="font-serif text-lg font-semibold tracking-tight">
                {c.name}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {c.course}
                {c.professor_name ? ` · ${c.professor_name}` : ""}
                {c.school ? ` · ${c.school}` : ""}
              </p>
              <p className="mt-1 text-xs tabular-nums text-muted-foreground">
                Created {formatRelativeTime(c.created_at)} · id{" "}
                <code>{c.id.slice(0, 8)}&hellip;</code>
              </p>
            </div>
            <div className="text-right text-xs tabular-nums text-muted-foreground">
              <div>{c.book_count} books</div>
              <div>{c.transcript_count} transcripts</div>
              <div>{c.artifact_count} artifacts</div>
              <div>{c.professor_profile_count} profiles</div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

function DashLink({
  href,
  label,
  detail,
  external,
}: {
  href: string;
  label: string;
  detail: string;
  external?: boolean;
}) {
  const className =
    "group flex flex-col gap-1 border-b border-border px-6 py-6 transition-colors hover:bg-muted md:border-b md:border-r md:last:border-r-0 md:[&:nth-child(even)]:border-r-0";
  const content = (
    <>
      <span className="font-serif text-lg font-semibold tracking-tight text-foreground">
        {label}
      </span>
      <span className="text-sm text-muted-foreground">{detail}</span>
      <span className="mt-2 text-xs uppercase tracking-[0.12em] text-accent group-hover:underline">
        Open
      </span>
    </>
  );
  if (external) {
    return (
      <a href={href} className={className} target="_blank" rel="noreferrer">
        {content}
      </a>
    );
  }
  return (
    <Link href={href} className={className}>
      {content}
    </Link>
  );
}
