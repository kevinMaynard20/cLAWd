"use client";

import Link from "next/link";
import * as React from "react";

import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";

/**
 * Emphasis-map viewer. Path: `/transcripts/{transcript_id}/emphasis`.
 *
 * On mount we hit `POST /features/emphasis-map` (idempotent — cached on the
 * back-end) so the page also works as the post-build landing surface from the
 * upload page's transcript success state.
 */

type EmphasisItem = {
  id: string;
  subject_kind: string;
  subject_label: string;
  minutes_on: number;
  return_count: number;
  hypotheticals_run: string[];
  disclaimed: boolean;
  engaged_questions: number;
  exam_signal_score: number;
  justification: string;
};

type EmphasisResponse = {
  items: EmphasisItem[];
  summary: string | null;
  cache_hit: boolean;
  warnings: string[];
};

type TranscriptDetail = {
  id: string;
  corpus_id: string;
  topic: string | null;
  assignment_code: string | null;
  ingested_at: string;
};

export default function EmphasisPage(props: {
  params: Promise<{ transcriptId: string }>;
}) {
  const { transcriptId } = React.use(props.params);
  const [transcript, setTranscript] = React.useState<TranscriptDetail | null>(null);
  const [data, setData] = React.useState<EmphasisResponse | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const t = await api.get<TranscriptDetail>(`/transcripts/${transcriptId}`);
        if (cancelled) return;
        setTranscript(t);
        const map = await api.post<EmphasisResponse>("/features/emphasis-map", {
          corpus_id: t.corpus_id,
          transcript_id: transcriptId,
        });
        if (!cancelled) setData(map);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load emphasis map.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [transcriptId]);

  if (error) {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <p className="text-destructive">{error}</p>
      </main>
    );
  }
  if (!transcript || !data) {
    return (
      <main className="mx-auto flex max-w-4xl items-center gap-2 px-6 py-12 text-sm text-muted-foreground">
        <Spinner size="sm" /> Building emphasis map…
      </main>
    );
  }

  const ranked = [...data.items].sort(
    (a, b) => b.exam_signal_score - a.exam_signal_score,
  );

  return (
    <main className="mx-auto w-full max-w-4xl px-6 py-10">
      <Link
        href={`/corpora/${transcript.corpus_id}`}
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← Corpus
      </Link>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        Emphasis map
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        {transcript.topic ?? "(untitled transcript)"}
        {transcript.assignment_code ? ` · ${transcript.assignment_code}` : ""}
        {data.cache_hit ? " · cached" : ""}
      </p>

      {data.summary && (
        <section className="mt-6 border border-border bg-card px-4 py-3 font-serif text-base leading-relaxed">
          {data.summary}
        </section>
      )}

      {data.warnings.length > 0 && (
        <ul className="mt-4 space-y-1 text-xs text-amber-600">
          {data.warnings.map((w, idx) => (
            <li key={idx}>⚠ {w}</li>
          ))}
        </ul>
      )}

      <h2 className="mt-8 font-serif text-xl font-semibold tracking-tight">
        Ranked items ({ranked.length})
      </h2>

      {ranked.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">
          No emphasis items extracted from this transcript.
        </p>
      ) : (
        <ul className="mt-4 flex flex-col gap-3">
          {ranked.map((item, idx) => (
            <EmphasisCard key={item.id} item={item} rank={idx + 1} />
          ))}
        </ul>
      )}
    </main>
  );
}

function EmphasisCard({ item, rank }: { item: EmphasisItem; rank: number }) {
  return (
    <li className="border border-border bg-card px-4 py-3">
      <div className="flex items-baseline gap-3">
        <span className="font-serif text-2xl font-semibold tabular-nums text-accent">
          #{rank}
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-serif text-lg font-semibold">
            {item.subject_label}
          </p>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            {item.subject_kind}
            {" · "}exam-signal {item.exam_signal_score.toFixed(2)}
          </p>
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs tabular-nums sm:grid-cols-4">
        <Fact label="Minutes on" value={item.minutes_on.toFixed(1)} />
        <Fact label="Returns" value={item.return_count} />
        <Fact label="Engaged Qs" value={item.engaged_questions} />
        <Fact label="Hypos" value={item.hypotheticals_run.length} />
      </dl>

      {item.disclaimed && (
        <p className="mt-2 text-xs text-amber-600">
          ⚠ Professor disclaimed this — emphasis dampened.
        </p>
      )}

      <p className="mt-3 font-serif text-sm leading-relaxed">
        {item.justification}
      </p>

      {item.hypotheticals_run.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs uppercase tracking-[0.14em] text-muted-foreground">
            Hypotheticals run ({item.hypotheticals_run.length})
          </summary>
          <ul className="mt-2 space-y-1 text-sm">
            {item.hypotheticals_run.map((h, idx) => (
              <li key={idx} className="text-foreground/80">
                — {h}
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

function Fact({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </dt>
      <dd className="font-serif text-sm">{value}</dd>
    </div>
  );
}
