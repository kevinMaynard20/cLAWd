"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { ArtifactPicker } from "@/components/ArtifactPicker";
import { LoadingButton } from "@/components/LoadingButton";
import { Spinner } from "@/components/Spinner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { api, ApiError } from "@/lib/api";

/**
 * Attack-sheet builder. Pick a topic + case briefs → POST /features/attack-sheet.
 *
 * The output is a one-page exam attack sheet rendered through the shared
 * artifact viewer (which has print-friendly markup).
 */

type CorpusSummary = {
  id: string;
  name: string;
  course: string;
};

export default function AttackSheetsPage() {
  const search = useSearchParams();
  const router = useRouter();
  const initialCorpus = search.get("corpus_id") ?? "";

  const [corpora, setCorpora] = React.useState<CorpusSummary[] | null>(null);
  const [corpusId, setCorpusId] = React.useState(initialCorpus);
  const [topic, setTopic] = React.useState("");
  const [selected, setSelected] = React.useState<string[]>([]);
  const [emphasisMapId, setEmphasisMapId] = React.useState<string | null>(null);
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

  const submit = async () => {
    if (!corpusId || !topic.trim() || selected.length < 1) {
      setError("Pick a corpus, name the topic, and select at least 1 brief.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const res = await api.post<{ artifact: { id: string } }>(
        "/features/attack-sheet",
        {
          corpus_id: corpusId,
          topic: topic.trim(),
          case_brief_artifact_ids: selected,
          emphasis_map_artifact_id: emphasisMapId,
        },
      );
      router.push(`/artifacts/${res.artifact.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Attack-sheet failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto w-full max-w-4xl px-6 py-10">
      <Link
        href={corpusId ? `/corpora/${corpusId}` : "/"}
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← {corpusId ? "Corpus" : "Dashboard"}
      </Link>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        Attack-sheet builder
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        One-page exam attack sheet for a doctrinal topic. Composes from existing
        briefs and (optionally) an emphasis map.
      </p>

      <Card className="mt-8">
        <CardHeader>
          <CardTitle>1 · Corpus + topic</CardTitle>
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
            <Label>Topic</Label>
            <Input
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="regulatory takings"
            />
          </div>
        </CardContent>
      </Card>

      {corpusId && (
        <>
          <Card className="mt-6">
            <CardHeader>
              <CardTitle>2 · Pick supporting briefs</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="mb-3 text-xs text-muted-foreground">
                Briefs are generated from the cases in your casebook. Open a
                book on the corpus page, click <strong>Brief</strong> next to
                each case you want covered.
              </p>
              <ArtifactPicker
                corpusId={corpusId}
                type="case_brief"
                multiple
                values={selected}
                onChange={setSelected}
                searchPlaceholder="Filter briefs by case name…"
                emptyHint={
                  <>
                    No case briefs in this corpus yet.{" "}
                    <Link
                      href={`/corpora/${corpusId}`}
                      className="law-link underline"
                    >
                      Open the corpus →
                    </Link>{" "}
                    pick a book, click <strong>Brief</strong> on the cases for
                    this topic, then come back here.
                  </>
                }
              />
            </CardContent>
          </Card>
          <Card className="mt-6">
            <CardHeader>
              <CardTitle>3 · Optional · weight by emphasis</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="mb-2 text-xs text-muted-foreground">
                Pick an emphasis-map artifact (built from a class transcript)
                to bias the sheet toward what the professor actually emphasized.
                Leave empty to skip.
              </p>
              <ArtifactPicker
                corpusId={corpusId}
                type="emphasis_map"
                value={emphasisMapId}
                onChange={setEmphasisMapId}
                searchPlaceholder="Filter emphasis maps…"
                emptyHint={
                  <>
                    No emphasis maps yet — built from class transcripts on the
                    Transcripts tab of your{" "}
                    <Link
                      href={`/corpora/${corpusId}`}
                      className="law-link underline"
                    >
                      corpus dashboard
                    </Link>
                    . Optional — leave blank to skip.
                  </>
                }
              />
            </CardContent>
          </Card>
        </>
      )}

      <div className="mt-6 flex items-center gap-3">
        <LoadingButton
          onClick={() => void submit()}
          loading={busy}
          disabled={!corpusId || !topic.trim() || selected.length < 1}
        >
          {busy ? "Building…" : "Build attack sheet"}
        </LoadingButton>
        {error && <p className="text-sm text-destructive">{error}</p>}
      </div>
    </main>
  );
}
