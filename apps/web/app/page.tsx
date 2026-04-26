"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

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
