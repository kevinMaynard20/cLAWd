"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { LoadingButton } from "@/components/LoadingButton";
import { Spinner } from "@/components/Spinner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { api, ApiError } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

/**
 * Outline builder. Pre-flight panel shows artifact counts so the user knows
 * whether the outline will be rich or thin. The actual generation hits
 * `POST /features/outline` and navigates to the artifact viewer.
 *
 * If the outline already exists for this (corpus, course) pair, the backend
 * returns it cached — we still navigate but the UI shows `cache_hit=true`.
 */

type CorpusSummary = {
  id: string;
  name: string;
  course: string;
};

type CorpusStats = {
  corpus_id: string;
  book_count: number;
  transcript_count: number;
  professor_profile_count: number;
  artifacts_by_type: Record<string, number>;
  latest_brief_at: string | null;
  latest_outline_at: string | null;
};

export default function OutlinePage() {
  const search = useSearchParams();
  const router = useRouter();
  const initialCorpus = search.get("corpus_id") ?? "";

  const [corpora, setCorpora] = React.useState<CorpusSummary[] | null>(null);
  const [corpusId, setCorpusId] = React.useState(initialCorpus);
  const [course, setCourse] = React.useState("");
  const [stats, setStats] = React.useState<CorpusStats | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.get<CorpusSummary[]>("/corpora");
        if (cancelled) return;
        setCorpora(list);
        if (!corpusId && list.length === 1) setCorpusId(list[0].id);
      } catch {
        if (!cancelled) setCorpora([]);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  React.useEffect(() => {
    if (!corpusId) {
      setStats(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const [s, c] = await Promise.all([
          api.get<CorpusStats>(`/corpora/${corpusId}/stats`),
          api.get<CorpusSummary>(`/corpora/${corpusId}`),
        ]);
        if (cancelled) return;
        setStats(s);
        if (!course) setCourse(c.course);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load corpus stats.");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corpusId]);

  const submit = async () => {
    if (!corpusId || !course.trim()) {
      setError("Pick a corpus and confirm the course name.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const res = await api.post<{ artifact: { id: string } }>(
        "/features/outline",
        {
          corpus_id: corpusId,
          course: course.trim(),
        },
      );
      router.push(`/artifacts/${res.artifact.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Outline build failed.");
    } finally {
      setBusy(false);
    }
  };

  const briefCount = stats?.artifacts_by_type["case_brief"] ?? 0;
  const synthesisCount = stats?.artifacts_by_type["synthesis"] ?? 0;
  const transcriptCount = stats?.transcript_count ?? 0;
  const outlineCount = stats?.artifacts_by_type["outline"] ?? 0;

  const richness =
    briefCount >= 20
      ? "high"
      : briefCount >= 8
        ? "medium"
        : briefCount > 0
          ? "low"
          : "empty";

  return (
    <main className="mx-auto w-full max-w-4xl px-6 py-10">
      <Link
        href={corpusId ? `/corpora/${corpusId}` : "/"}
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← {corpusId ? "Corpus" : "Dashboard"}
      </Link>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        Outline
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Hierarchical outline pulling together every brief, transcript, and
        synthesis in this corpus.
      </p>

      <Card className="mt-8">
        <CardHeader>
          <CardTitle>1 · Corpus</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label>Corpus</Label>
            {corpora === null ? (
              <Spinner size="sm" />
            ) : (
              <Select
                value={corpusId}
                onChange={(e) => setCorpusId(e.target.value)}
              >
                <option value="">Select a corpus…</option>
                {corpora.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} — {c.course}
                  </option>
                ))}
              </Select>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Course</Label>
            <Input
              value={course}
              onChange={(e) => setCourse(e.target.value)}
              placeholder="Property"
            />
          </div>
        </CardContent>
      </Card>

      {stats && (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>2 · Pre-flight</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="Briefs" value={briefCount} />
              <Stat label="Transcripts" value={transcriptCount} />
              <Stat label="Syntheses" value={synthesisCount} />
              <Stat label="Profiles" value={stats.professor_profile_count} />
            </div>
            <RichnessHint richness={richness} briefCount={briefCount} />
            {stats.latest_outline_at && (
              <p className="mt-3 text-xs text-muted-foreground">
                Last outline built {formatRelativeTime(stats.latest_outline_at)}
                {stats.latest_brief_at &&
                stats.latest_brief_at > stats.latest_outline_at
                  ? " — newer briefs exist; rebuilding will pick them up."
                  : ""}
              </p>
            )}
            {outlineCount > 0 && (
              <p className="mt-1 text-xs text-muted-foreground">
                ({outlineCount} outline{outlineCount === 1 ? "" : "s"} already in this corpus.)
              </p>
            )}
          </CardContent>
        </Card>
      )}

      <div className="mt-6 flex items-center gap-3">
        <LoadingButton
          onClick={() => void submit()}
          loading={busy}
          disabled={!corpusId || !course.trim()}
        >
          {busy ? "Building outline…" : outlineCount > 0 ? "Rebuild outline" : "Build outline"}
        </LoadingButton>
        {error && <p className="text-sm text-destructive">{error}</p>}
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

function RichnessHint({
  richness,
  briefCount,
}: {
  richness: "empty" | "low" | "medium" | "high";
  briefCount: number;
}) {
  const tone =
    richness === "high"
      ? "border-success/40 bg-success/5"
      : richness === "medium"
        ? "border-border bg-card"
        : "border-amber-500/40 bg-amber-500/5";
  const text =
    richness === "high"
      ? "Plenty of briefs — the outline will be rich and high-confidence."
      : richness === "medium"
        ? "Decent corpus depth. Outline will be useful for review."
        : richness === "low"
          ? `Only ${briefCount} briefs. The outline will be skeletal — consider briefing more cases first.`
          : "No briefs yet — the outline will be a header skeleton with empty bodies.";
  return <p className={`mt-3 border px-3 py-2 text-sm ${tone}`}>{text}</p>;
}
