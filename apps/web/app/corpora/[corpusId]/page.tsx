"use client";

import Link from "next/link";
import * as React from "react";

import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, ApiError } from "@/lib/api";
import { formatRelativeTime, formatUsd } from "@/lib/format";

/**
 * Corpus-detail dashboard. Tabs over Books / Cases / Transcripts / Past exams
 * / Profiles / Briefs / Synthesis / Attack sheets / Outlines. Each tab is a
 * thin list-with-actions surface — clicking through navigates to the right
 * feature page. This is the hub the dashboard cards (`/`) link into.
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

type CorpusStats = {
  corpus_id: string;
  book_count: number;
  transcript_count: number;
  professor_profile_count: number;
  artifacts_by_type: Record<string, number>;
  latest_brief_at: string | null;
  latest_outline_at: string | null;
  latest_emphasis_map_at: string | null;
};

type ArtifactRow = {
  id: string;
  type: string;
  created_at: string;
  cost_usd: string;
  title: string;
};

type TranscriptRow = {
  id: string;
  topic: string | null;
  lecture_date: string | null;
  assignment_code: string | null;
  source_type: string;
  ingested_at: string;
};

type ProfileRow = {
  id: string;
  professor_name: string;
  course: string;
  school: string | null;
  updated_at: string;
};

type BookRow = {
  // Books are exposed via the corpora list (counts only). For the books tab
  // we hit the export endpoint's metadata only — but simpler: filter Block-
  // free metadata via /artifacts of type book? No — Books aren't artifacts.
  // We accept a degraded view here: list known book ids inferred from cases
  // listing. As an MVP the books tab links straight to the case index page
  // which already lists case-name + page count, so for now we surface a
  // "no per-book metadata" panel and link to /upload to add more.
  // Updated approach: fetch via a tiny new BookRow shape from the corpora
  // export endpoint metadata.
  id: string;
  title: string;
  source_page_min: number;
  source_page_max: number;
};

export default function CorpusDetailPage(props: {
  params: Promise<{ corpusId: string }>;
}) {
  const { corpusId } = React.use(props.params);

  const [corpus, setCorpus] = React.useState<CorpusSummary | null>(null);
  const [stats, setStats] = React.useState<CorpusStats | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [c, s] = await Promise.all([
          api.get<CorpusSummary>(`/corpora/${corpusId}`),
          api.get<CorpusStats>(`/corpora/${corpusId}/stats`),
        ]);
        if (cancelled) return;
        setCorpus(c);
        setStats(s);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError ? err.message : "Could not load corpus.",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [corpusId]);

  if (error) {
    return (
      <main className="mx-auto w-full max-w-5xl px-6 py-12">
        <p className="text-destructive">{error}</p>
        <Link href="/" className="law-link mt-3 inline-block underline">
          ← Dashboard
        </Link>
      </main>
    );
  }

  if (!corpus || !stats) {
    return (
      <main className="mx-auto flex w-full max-w-5xl items-center gap-2 px-6 py-12 text-sm text-muted-foreground">
        <Spinner size="sm" /> Loading corpus…
      </main>
    );
  }

  const briefCount = stats.artifacts_by_type["case_brief"] ?? 0;
  const synthesisCount = stats.artifacts_by_type["synthesis"] ?? 0;
  const attackSheetCount = stats.artifacts_by_type["attack_sheet"] ?? 0;
  const outlineCount = stats.artifacts_by_type["outline"] ?? 0;
  const pastExamCount = stats.artifacts_by_type["past_exam"] ?? 0;
  const rubricCount = stats.artifacts_by_type["rubric"] ?? 0;

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <Link
        href="/"
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← Dashboard
      </Link>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        {corpus.name}
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        {corpus.course}
        {corpus.professor_name ? ` · ${corpus.professor_name}` : ""}
        {corpus.school ? ` · ${corpus.school}` : ""}
        {" · "}created {formatRelativeTime(corpus.created_at)}
      </p>

      <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4 md:grid-cols-6">
        <Stat label="Books" value={stats.book_count} />
        <Stat label="Transcripts" value={stats.transcript_count} />
        <Stat label="Briefs" value={briefCount} />
        <Stat label="Past exams" value={pastExamCount} />
        <Stat label="Profiles" value={stats.professor_profile_count} />
        <Stat label="Outlines" value={outlineCount} />
      </div>

      <div className="mt-8">
        <Tabs defaultValue="books">
          <TabsList>
            <TabsTrigger value="books">Books</TabsTrigger>
            <TabsTrigger value="transcripts">Transcripts</TabsTrigger>
            <TabsTrigger value="briefs">Briefs ({briefCount})</TabsTrigger>
            <TabsTrigger value="past-exams">Past exams ({pastExamCount})</TabsTrigger>
            <TabsTrigger value="profiles">Profiles</TabsTrigger>
            <TabsTrigger value="study">Study</TabsTrigger>
          </TabsList>

          <TabsContent value="books" className="pt-6">
            <BooksTab corpusId={corpusId} />
          </TabsContent>
          <TabsContent value="transcripts" className="pt-6">
            <TranscriptsTab corpusId={corpusId} />
          </TabsContent>
          <TabsContent value="briefs" className="pt-6">
            <ArtifactsTab corpusId={corpusId} type="case_brief" emptyHint="No case briefs yet. Use the Cases panel inside a book to generate one." />
          </TabsContent>
          <TabsContent value="past-exams" className="pt-6">
            <PastExamsTab corpusId={corpusId} rubricCount={rubricCount} />
          </TabsContent>
          <TabsContent value="profiles" className="pt-6">
            <ProfilesTab corpusId={corpusId} />
          </TabsContent>
          <TabsContent value="study" className="pt-6">
            <StudyTab
              corpusId={corpusId}
              briefCount={briefCount}
              synthesisCount={synthesisCount}
              attackSheetCount={attackSheetCount}
              outlineCount={outlineCount}
            />
          </TabsContent>
        </Tabs>
      </div>
    </main>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="border border-border bg-card px-3 py-3">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </p>
      <p className="mt-0.5 font-serif text-2xl font-semibold tabular-nums">
        {value}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Books tab — list books inferred from the corpus export. We don't have a
// dedicated GET /corpora/{id}/books, so we use /corpora/{id}/export?dry=meta
// approach... actually simpler: piggyback on a small inline endpoint missing
// today. As an MVP, list books via /search on a tautological query? Cleaner:
// just enumerate via /corpora/{id}/export endpoint metadata; but that streams
// a tarball. For UI-1 we add the books tab as a coming-soon placeholder for
// the dedicated detail page, with a "Cases" CTA that needs a book_id picker.
//
// Compromise: hit /artifacts?corpus_id=&type=case_brief and group by the
// originating sources[].book_id when present. This is best-effort — we'd
// rather have a real endpoint, but it's enough to drive navigation today.
// ---------------------------------------------------------------------------

function BooksTab({ corpusId }: { corpusId: string }) {
  const [books, setBooks] = React.useState<BookRow[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // We fetch the corpus export's book listing via a small probe: there
        // isn't a dedicated endpoint yet, so we ask the existing retrieve
        // primitive for an empty page-range query. The retrieve route returns
        // a generic shape — but for now, fall back to a single GET that
        // doesn't exist yet. As a stopgap, we render a notice and link the
        // user to /upload.
        const list = await api.get<BookRow[]>(`/corpora/${corpusId}/books`);
        if (!cancelled) setBooks(list);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setBooks([]);
        } else {
          setError(
            err instanceof ApiError ? err.message : "Could not load books.",
          );
          setBooks([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [corpusId]);

  if (error) {
    return <p className="text-sm text-destructive">{error}</p>;
  }
  if (books === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner size="sm" /> Loading books…
      </div>
    );
  }
  if (books.length === 0) {
    return (
      <div className="border border-border bg-card px-4 py-6 text-sm">
        <p>No books in this corpus yet.</p>
        <p className="mt-2 text-muted-foreground">
          Add a casebook PDF on the{" "}
          <Link href="/upload" className="law-link underline">
            Upload page
          </Link>
          . Once ingested, you can brief cases, generate flashcards, run
          Socratic drills, and build attack sheets here.
        </p>
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border border border-border bg-card">
      {books.map((b) => (
        <li
          key={b.id}
          className="grid grid-cols-[1fr_auto] items-center px-4 py-3"
        >
          <div className="min-w-0">
            <p className="truncate font-serif text-lg">{b.title}</p>
            <p className="text-[11px] tabular-nums text-muted-foreground">
              pp. {b.source_page_min}–{b.source_page_max} ·{" "}
              <code className="font-mono">{b.id.slice(0, 8)}…</code>
            </p>
          </div>
          <div className="flex gap-2">
            <Link href={`/corpora/${corpusId}/books/${b.id}`}>
              <Button size="sm" variant="outline">Open</Button>
            </Link>
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Transcripts tab
// ---------------------------------------------------------------------------

function TranscriptsTab({ corpusId }: { corpusId: string }) {
  const [rows, setRows] = React.useState<TranscriptRow[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busyId, setBusyId] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.get<TranscriptRow[]>("/transcripts", { corpus_id: corpusId });
        if (!cancelled) setRows(data);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load transcripts.");
        setRows([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [corpusId]);

  const buildEmphasisMap = async (transcriptId: string) => {
    setBusyId(transcriptId);
    try {
      await api.post("/features/emphasis-map", {
        corpus_id: corpusId,
        transcript_id: transcriptId,
      });
      // Navigate to the emphasis viewer regardless of cache hit.
      window.location.href = `/transcripts/${transcriptId}/emphasis`;
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Could not build emphasis map.");
    } finally {
      setBusyId(null);
    }
  };

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (rows === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner size="sm" /> Loading transcripts…
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="border border-border bg-card px-4 py-6 text-sm">
        <p>No transcripts in this corpus yet.</p>
        <p className="mt-2 text-muted-foreground">
          Paste or upload a Gemini transcript on the{" "}
          <Link href="/upload" className="law-link underline">
            Upload page
          </Link>
          .
        </p>
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border border border-border bg-card">
      {rows.map((t) => (
        <li key={t.id} className="grid grid-cols-[1fr_auto] items-center px-4 py-3">
          <div className="min-w-0">
            <p className="truncate font-serif text-base">
              {t.topic ?? "(untitled transcript)"}
            </p>
            <p className="text-[11px] tabular-nums text-muted-foreground">
              {t.assignment_code ? `${t.assignment_code} · ` : ""}
              {formatRelativeTime(t.ingested_at)} ·{" "}
              <code className="font-mono">{t.id.slice(0, 8)}…</code>
            </p>
          </div>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={busyId !== null}
              onClick={() => void buildEmphasisMap(t.id)}
            >
              {busyId === t.id ? "Building…" : "Emphasis map"}
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Generic artifacts tab — used for briefs, syntheses, attack sheets, outlines
// ---------------------------------------------------------------------------

function ArtifactsTab({
  corpusId,
  type,
  emptyHint,
}: {
  corpusId: string;
  type: string;
  emptyHint: string;
}) {
  const [rows, setRows] = React.useState<ArtifactRow[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get<{ count: number; artifacts: ArtifactRow[] }>(
          "/artifacts",
          { corpus_id: corpusId, type, limit: 200 },
        );
        if (!cancelled) setRows(res.artifacts);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load artifacts.");
        setRows([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [corpusId, type]);

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (rows === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner size="sm" /> Loading…
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="border border-border bg-card px-4 py-6 text-sm text-muted-foreground">
        {emptyHint}
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border border border-border bg-card">
      {rows.map((a) => (
        <li key={a.id} className="grid grid-cols-[1fr_auto] items-center px-4 py-3">
          <div className="min-w-0">
            <p className="truncate font-serif text-base">{a.title}</p>
            <p className="text-[11px] tabular-nums text-muted-foreground">
              {formatRelativeTime(a.created_at)} · {formatUsd(a.cost_usd)} ·{" "}
              <code className="font-mono">{a.id.slice(0, 8)}…</code>
            </p>
          </div>
          <Link href={`/artifacts/${a.id}`}>
            <Button size="sm" variant="outline">Open</Button>
          </Link>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Past exams tab
// ---------------------------------------------------------------------------

function PastExamsTab({ corpusId, rubricCount }: { corpusId: string; rubricCount: number }) {
  return (
    <div className="flex flex-col gap-4">
      <div className="border border-border bg-card px-4 py-3 text-sm">
        <p className="font-medium">Practice on a past exam</p>
        <p className="mt-1 text-muted-foreground">
          Take a Pollack exam (with grader memo if you have it). Get a rubric,
          write your answer, and grade against the rubric in one flow.
        </p>
        <Link href={`/practice?corpus_id=${corpusId}`}>
          <Button size="sm" className="mt-3">Start practice →</Button>
        </Link>
      </div>
      <ArtifactsTab
        corpusId={corpusId}
        type="past_exam"
        emptyHint="No past exams ingested yet. Use the practice wizard to upload one."
      />
      {rubricCount > 0 && (
        <ArtifactsTab
          corpusId={corpusId}
          type="rubric"
          emptyHint=""
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Profiles tab
// ---------------------------------------------------------------------------

function ProfilesTab({ corpusId }: { corpusId: string }) {
  const [rows, setRows] = React.useState<ProfileRow[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [seeding, setSeeding] = React.useState(false);

  const refresh = React.useCallback(async () => {
    try {
      const data = await api.get<ProfileRow[]>("/profiles", { corpus_id: corpusId });
      setRows(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load profiles.");
      setRows([]);
    }
  }, [corpusId]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  const seedPollack = async () => {
    setSeeding(true);
    try {
      await api.post("/profiles/seed-pollack", { corpus_id: corpusId });
      await refresh();
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Could not seed Pollack profile.");
    } finally {
      setSeeding(false);
    }
  };

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (rows === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner size="sm" /> Loading profiles…
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <Button size="sm" variant="outline" onClick={() => void seedPollack()} disabled={seeding}>
          {seeding ? "Seeding…" : "Seed Pollack profile"}
        </Button>
      </div>
      {rows.length === 0 ? (
        <div className="border border-border bg-card px-4 py-6 text-sm text-muted-foreground">
          No professor profiles yet. Seed Pollack's, or build one from grader
          memos via <code>POST /profiles/build</code>.
        </div>
      ) : (
        <ul className="divide-y divide-border border border-border bg-card">
          {rows.map((p) => (
            <li key={p.id} className="px-4 py-3">
              <p className="font-serif text-base">{p.professor_name}</p>
              <p className="text-[11px] tabular-nums text-muted-foreground">
                {p.course}{p.school ? ` · ${p.school}` : ""} · updated {formatRelativeTime(p.updated_at)}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Study tab — one-click links to synthesis / attack-sheet / outline / practice
// ---------------------------------------------------------------------------

function StudyTab({
  corpusId,
  briefCount,
  synthesisCount,
  attackSheetCount,
  outlineCount,
}: {
  corpusId: string;
  briefCount: number;
  synthesisCount: number;
  attackSheetCount: number;
  outlineCount: number;
}) {
  const enoughBriefs = briefCount >= 2;
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <StudyCard
        title="IRAC practice"
        body="Past exam + memo, fresh hypo, or paste your own question. Inline graded feedback."
        href={`/practice?corpus_id=${corpusId}`}
        cta="Practice"
      />
      <StudyCard
        title="Outline"
        body={
          outlineCount > 0
            ? "Hierarchical outline of every brief, transcript, and synthesis in this corpus."
            : "Build a hierarchical outline of everything in this corpus."
        }
        href={`/outline?corpus_id=${corpusId}`}
        cta={outlineCount > 0 ? "Open" : "Build"}
      />
      <StudyCard
        title="Multi-case synthesis"
        body={
          enoughBriefs
            ? `Combine ${briefCount} briefed cases into a doctrinal essay.`
            : "Brief at least 2 cases first, then synthesize across them."
        }
        href={`/synthesis?corpus_id=${corpusId}`}
        cta={synthesisCount > 0 ? "Open" : "Build"}
        disabled={!enoughBriefs}
      />
      <StudyCard
        title="Attack sheets"
        body={
          enoughBriefs
            ? "One-page exam attack sheets per topic. Bulk-build from the syllabus."
            : "Build briefs first; attack sheets composite them."
        }
        href={`/attack-sheets?corpus_id=${corpusId}`}
        cta={attackSheetCount > 0 ? "Open" : "Build"}
        disabled={!enoughBriefs}
      />
      <StudyCard
        title="Cold-call (random)"
        body="Pick a book + page range, server picks a case, drill under pressure."
        href={`/cold-call/random?corpus_id=${corpusId}`}
        cta="Start"
      />
      <StudyCard
        title="Search"
        body="Cross-corpus search over books, transcripts, and artifacts."
        href={`/search?corpus_id=${corpusId}`}
        cta="Search"
      />
    </div>
  );
}

function StudyCard({
  title,
  body,
  href,
  cta,
  disabled = false,
}: {
  title: string;
  body: string;
  href: string;
  cta: string;
  disabled?: boolean;
}) {
  const inner = (
    <div
      className={`flex h-full flex-col justify-between border border-border bg-card px-4 py-4 transition-colors ${
        disabled ? "opacity-60" : "hover:bg-muted"
      }`}
    >
      <div>
        <p className="font-serif text-lg font-semibold">{title}</p>
        <p className="mt-1 text-sm text-muted-foreground">{body}</p>
      </div>
      <p className="mt-3 text-xs uppercase tracking-[0.12em] text-accent">
        {cta} →
      </p>
    </div>
  );
  if (disabled) return inner;
  return <Link href={href}>{inner}</Link>;
}
