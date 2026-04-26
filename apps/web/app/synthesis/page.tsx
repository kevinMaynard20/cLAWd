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
 * Multi-case synthesis page. Pick a corpus, name a doctrinal area, multi-select
 * existing case briefs (via the shared ArtifactPicker), kick off
 * `POST /features/synthesis`, navigate to the artifact viewer when done.
 */

type CorpusSummary = {
  id: string;
  name: string;
  course: string;
};

export default function SynthesisPage() {
  const search = useSearchParams();
  const router = useRouter();
  const initialCorpus = search.get("corpus_id") ?? "";

  const [corpora, setCorpora] = React.useState<CorpusSummary[] | null>(null);
  const [corpusId, setCorpusId] = React.useState(initialCorpus);
  const [doctrinalArea, setDoctrinalArea] = React.useState("");
  const [selected, setSelected] = React.useState<string[]>([]);
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
    if (!corpusId || !doctrinalArea.trim() || selected.length < 2) {
      setError("Pick a corpus, name the doctrinal area, and select at least 2 briefs.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const res = await api.post<{ artifact: { id: string } }>(
        "/features/synthesis",
        {
          corpus_id: corpusId,
          doctrinal_area: doctrinalArea.trim(),
          case_brief_artifact_ids: selected,
        },
      );
      router.push(`/artifacts/${res.artifact.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Synthesis failed.");
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
        Multi-case synthesis
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Combine 2+ case briefs into a doctrinal essay. Briefs that don&apos;t
        exist yet need to be generated first.
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
            <Label>Doctrinal area</Label>
            <Input
              value={doctrinalArea}
              onChange={(e) => setDoctrinalArea(e.target.value)}
              placeholder="adverse possession"
            />
          </div>
        </CardContent>
      </Card>

      {corpusId && (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>2 · Pick case briefs (≥ 2)</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="mb-3 text-xs text-muted-foreground">
              Briefs are generated from the cases in your casebook. Open a
              book on the corpus page, click <strong>Brief</strong> next to
              each case you want — they&apos;ll show up here.
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
                  pick a book, click <strong>Brief</strong> on the cases you
                  want to synthesize, then come back here.
                </>
              }
            />
          </CardContent>
        </Card>
      )}

      <div className="mt-6 flex items-center gap-3">
        <LoadingButton
          onClick={() => void submit()}
          loading={busy}
          disabled={!corpusId || !doctrinalArea.trim() || selected.length < 2}
        >
          {busy ? "Synthesizing…" : "Build synthesis"}
        </LoadingButton>
        {error && <p className="text-sm text-destructive">{error}</p>}
      </div>
    </main>
  );
}
