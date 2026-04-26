"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import * as React from "react";

import { ArtifactPicker } from "@/components/ArtifactPicker";
import { LoadingButton } from "@/components/LoadingButton";
import { PdfOrPasteField } from "@/components/PdfOrPasteField";
import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, ApiError } from "@/lib/api";
import { useDraft } from "@/lib/draft";
import { cn } from "@/lib/utils";

/**
 * IRAC practice wizard. Three flows reflect the workflow analysis:
 *   - Flow A: past exam + grader memo  → rubric-extract → answer → grade.
 *   - Flow B: generate a fresh hypo    → rubric is co-generated → answer → grade.
 *   - Flow C: paste a question         → reuse an existing rubric or skip rubric.
 *
 * The answer workspace is localStorage-backed via `useDraft` so navigating
 * away doesn't lose work. The graded result renders inline with the rubric
 * coverage checklist + Pollack anti-pattern highlights.
 */

type CorpusSummary = {
  id: string;
  name: string;
  course: string;
};

type ArtifactSlim = {
  id: string;
  content: Record<string, unknown>;
};

type DetectedPattern = {
  name: string;
  severity: string;
  excerpt: string;
  line_offset: number;
  message: string;
};

type GradeResponse = {
  grade_artifact: {
    id: string;
    content: Record<string, unknown>;
  };
  detected_patterns: DetectedPattern[];
  rubric_coverage_passed: boolean;
  rubric_coverage_warnings: string[];
  cache_hit: boolean;
};

export default function PracticePage() {
  const search = useSearchParams();
  const initialCorpus = search.get("corpus_id") ?? "";

  const [corpora, setCorpora] = React.useState<CorpusSummary[] | null>(null);
  const [corpusId, setCorpusId] = React.useState(initialCorpus);

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

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <Link
        href={corpusId ? `/corpora/${corpusId}` : "/"}
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← {corpusId ? "Corpus" : "Dashboard"}
      </Link>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        IRAC practice
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Past exam + memo, fresh hypo, or paste a question. The system grades
        you against a rubric — never vibes.
      </p>

      <div className="mt-6 max-w-md">
        <Label>Corpus</Label>
        {corpora === null ? (
          <Spinner size="sm" />
        ) : (
          <Select value={corpusId} onChange={(e) => setCorpusId(e.target.value)}>
            <option value="">Select a corpus…</option>
            {corpora.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} — {c.course}
              </option>
            ))}
          </Select>
        )}
      </div>

      {corpusId && (
        <div className="mt-8">
          <Tabs defaultValue="exam">
            <TabsList>
              <TabsTrigger value="exam">Past exam + memo</TabsTrigger>
              <TabsTrigger value="hypo">Fresh hypo</TabsTrigger>
              <TabsTrigger value="paste">Paste a question</TabsTrigger>
            </TabsList>
            <TabsContent value="exam" className="pt-6">
              <PastExamFlow corpusId={corpusId} />
            </TabsContent>
            <TabsContent value="hypo" className="pt-6">
              <HypoFlow corpusId={corpusId} />
            </TabsContent>
            <TabsContent value="paste" className="pt-6">
              <PasteFlow corpusId={corpusId} />
            </TabsContent>
          </Tabs>
        </div>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Flow A: past exam + memo
// ---------------------------------------------------------------------------

function PastExamFlow({ corpusId }: { corpusId: string }) {
  const [examMd, setExamMd] = React.useState("");
  const [memoMd, setMemoMd] = React.useState("");
  const [questionLabel, setQuestionLabel] = React.useState("Part II Q2");

  const [examId, setExamId] = React.useState<string | null>(null);
  const [memoId, setMemoId] = React.useState<string | null>(null);
  const [rubricId, setRubricId] = React.useState<string | null>(null);

  const [step, setStep] = React.useState<
    "input" | "ingesting" | "extracting" | "answer" | "graded"
  >("input");
  const [error, setError] = React.useState<string | null>(null);

  const ingestAndExtract = async () => {
    if (!examMd.trim() || !memoMd.trim() || !questionLabel.trim()) {
      setError("Provide the exam, the memo, and the question label.");
      return;
    }
    setError(null);
    setStep("ingesting");
    try {
      const ingest = await api.post<{
        past_exam_artifact_id: string;
        grader_memo_artifact_id: string;
      }>("/ingest/past-exam", {
        corpus_id: corpusId,
        exam_markdown: examMd,
        grader_memo_markdown: memoMd,
      });
      setExamId(ingest.past_exam_artifact_id);
      setMemoId(ingest.grader_memo_artifact_id);

      setStep("extracting");
      const rubric = await api.post<{
        rubric_artifact: { id: string };
      }>("/features/rubric-extract", {
        corpus_id: corpusId,
        past_exam_artifact_id: ingest.past_exam_artifact_id,
        grader_memo_artifact_id: ingest.grader_memo_artifact_id,
        question_label: questionLabel,
      });
      setRubricId(rubric.rubric_artifact.id);
      setStep("answer");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Setup failed.");
      setStep("input");
    }
  };

  if (step === "answer" || step === "graded") {
    return (
      <AnswerWorkspace
        corpusId={corpusId}
        rubricId={rubricId!}
        questionLabel={questionLabel}
        questionMarkdown={examMd}
        draftKey={`practice/exam/${examId}/${rubricId}`}
        onGraded={() => setStep("graded")}
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1 · Upload exam + grader memo</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <PdfOrPasteField
          label="Exam"
          value={examMd}
          onChange={setExamMd}
          placeholder="Paste the past exam — or drop a PDF anywhere on the field."
        />
        <PdfOrPasteField
          label="Grader memo"
          value={memoMd}
          onChange={setMemoMd}
          placeholder="Paste the grader memo — or drop a PDF. We extract the rubric from it."
        />
        <div className="flex flex-col gap-1.5">
          <Label>Question label</Label>
          <Input
            value={questionLabel}
            onChange={(e) => setQuestionLabel(e.target.value)}
            placeholder="Part II Q2"
            className="max-w-xs"
          />
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div>
          <LoadingButton
            onClick={() => void ingestAndExtract()}
            loading={step === "ingesting" || step === "extracting"}
          >
            {step === "ingesting"
              ? "Ingesting…"
              : step === "extracting"
                ? "Extracting rubric…"
                : "Build rubric & start answering"}
          </LoadingButton>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Flow B: fresh hypo
// ---------------------------------------------------------------------------

function HypoFlow({ corpusId }: { corpusId: string }) {
  const [topicsRaw, setTopicsRaw] = React.useState("");
  const [step, setStep] = React.useState<"input" | "generating" | "answer" | "graded">(
    "input",
  );
  const [hypoArtifact, setHypoArtifact] = React.useState<{
    id: string;
    content: Record<string, unknown>;
  } | null>(null);
  const [rubricId, setRubricId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const generate = async () => {
    const topics = topicsRaw
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    if (topics.length === 0) {
      setError("Enter at least one topic to cover.");
      return;
    }
    setError(null);
    setStep("generating");
    try {
      const res = await api.post<{
        hypo_artifact: { id: string; content: Record<string, unknown> };
      }>("/features/hypo", {
        corpus_id: corpusId,
        topics_to_cover: topics,
      });
      setHypoArtifact(res.hypo_artifact);

      // The hypo artifact embeds the rubric inline; the rubric is also
      // persisted as its own RUBRIC artifact via the generate primitive's
      // post-write fan-out. To find it we hit /artifacts and look for the
      // most recent RUBRIC whose parent_artifact_id equals the hypo id.
      const list = await api.get<{
        artifacts: Array<{ id: string; parent_artifact_id: string | null }>;
      }>("/artifacts", {
        corpus_id: corpusId,
        type: "rubric",
        limit: 50,
      });
      const tied = list.artifacts.find(
        (a) => a.parent_artifact_id === res.hypo_artifact.id,
      );
      // Fallback: most recent rubric in the corpus.
      const chosen = tied ?? list.artifacts[0];
      if (!chosen) {
        setError(
          "Hypo generated but no rubric artifact found. Open the hypo from the dashboard.",
        );
        setStep("input");
        return;
      }
      setRubricId(chosen.id);
      setStep("answer");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Hypo generation failed.");
      setStep("input");
    }
  };

  if ((step === "answer" || step === "graded") && rubricId && hypoArtifact) {
    const c = hypoArtifact.content as { hypo?: { prompt?: string } };
    const prompt = (c.hypo && c.hypo.prompt) || JSON.stringify(hypoArtifact.content, null, 2);
    return (
      <AnswerWorkspace
        corpusId={corpusId}
        rubricId={rubricId}
        questionLabel="Generated hypo"
        questionMarkdown={prompt}
        draftKey={`practice/hypo/${hypoArtifact.id}`}
        onGraded={() => setStep("graded")}
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1 · Topics to cover</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label>Topics (comma-separated)</Label>
          <Input
            value={topicsRaw}
            onChange={(e) => setTopicsRaw(e.target.value)}
            placeholder="adverse possession, future interests, easements"
          />
          <p className="text-xs text-muted-foreground">
            We&apos;ll generate a fact pattern that exercises every listed topic
            and a rubric to grade against.
          </p>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div>
          <LoadingButton
            onClick={() => void generate()}
            loading={step === "generating"}
          >
            {step === "generating" ? "Generating hypo…" : "Generate hypo"}
          </LoadingButton>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Flow C: paste a question, pick an existing rubric (or skip)
// ---------------------------------------------------------------------------

function PasteFlow({ corpusId }: { corpusId: string }) {
  const [question, setQuestion] = React.useState("");
  const [rubricId, setRubricId] = React.useState<string | null>(null);
  const [proceed, setProceed] = React.useState(false);

  const draftKey = `practice/paste/${corpusId}/${rubricId ?? "norubric"}`;

  if (proceed && rubricId) {
    return (
      <AnswerWorkspace
        corpusId={corpusId}
        rubricId={rubricId}
        questionLabel="Pasted question"
        questionMarkdown={question}
        draftKey={draftKey}
        onGraded={() => undefined}
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1 · Paste your question and pick a rubric</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <PdfOrPasteField
          label="Question"
          value={question}
          onChange={setQuestion}
          placeholder="Paste the prompt you want to practice on — or drop a PDF."
        />
        <div className="flex flex-col gap-1.5">
          <Label>Rubric</Label>
          <p className="text-xs text-muted-foreground">
            Pick a previously-built rubric in this corpus. (Use the past-exam
            or hypo flow to build one if you don&apos;t have any yet.)
          </p>
          <ArtifactPicker
            corpusId={corpusId}
            type="rubric"
            value={rubricId}
            onChange={setRubricId}
            multiple={false}
            emptyHint={
              <>
                No rubrics yet. Build one by switching to the{" "}
                <strong>Past exam + memo</strong> tab (rubric extracted from a
                grader memo) or the <strong>Fresh hypo</strong> tab (rubric
                co-generated with the hypo).
              </>
            }
          />
        </div>
        <div>
          <Button
            disabled={!question.trim() || !rubricId}
            onClick={() => setProceed(true)}
          >
            Start answering
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Answer workspace — split-pane question + answer + grade output
// ---------------------------------------------------------------------------

function AnswerWorkspace({
  corpusId,
  rubricId,
  questionLabel,
  questionMarkdown,
  draftKey,
  onGraded,
}: {
  corpusId: string;
  rubricId: string;
  questionLabel: string;
  questionMarkdown: string;
  draftKey: string;
  onGraded: () => void;
}) {
  const [answer, setAnswer, clearAnswer] = useDraft(draftKey, "");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [grade, setGrade] = React.useState<GradeResponse | null>(null);

  const wordCount = answer.trim().split(/\s+/).filter(Boolean).length;

  const submit = async () => {
    if (!answer.trim()) {
      setError("Write your answer first.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const res = await api.post<GradeResponse>("/features/irac-grade", {
        corpus_id: corpusId,
        rubric_artifact_id: rubricId,
        answer_markdown: answer,
        question_label: questionLabel,
      });
      setGrade(res);
      onGraded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Grading failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Question</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            {questionLabel}
          </p>
          <pre className="mt-2 max-h-[60vh] overflow-y-auto whitespace-pre-wrap font-serif text-sm leading-relaxed">
            {questionMarkdown}
          </pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {grade ? "Graded" : "Your answer"}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {!grade && (
            <>
              <textarea
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder="Write IRAC. Pollack wants RULES APPLIED, not recited."
                className="min-h-[420px] w-full rounded-sm border border-input bg-card px-3 py-2 font-serif text-sm leading-relaxed"
              />
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  {wordCount} word{wordCount === 1 ? "" : "s"} · draft autosaved
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={clearAnswer}
                  disabled={!answer}
                >
                  Clear draft
                </Button>
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
              <div>
                <LoadingButton onClick={() => void submit()} loading={busy}>
                  Grade my answer
                </LoadingButton>
              </div>
            </>
          )}
          {grade && (
            <GradeView grade={grade} answer={answer} onRetry={() => setGrade(null)} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function GradeView({
  grade,
  answer,
  onRetry,
}: {
  grade: GradeResponse;
  answer: string;
  onRetry: () => void;
}) {
  const content = grade.grade_artifact.content as {
    score?: number;
    score_breakdown?: Array<{ label: string; awarded: number; max: number }>;
    rubric_coverage?: Array<{ requirement: string; covered: boolean; note?: string }>;
    suggestions?: string[];
    summary?: string;
  };

  const score =
    typeof content.score === "number"
      ? content.score
      : content.score_breakdown
        ? content.score_breakdown.reduce((acc, x) => acc + (x.awarded || 0), 0)
        : null;
  const max = content.score_breakdown
    ? content.score_breakdown.reduce((acc, x) => acc + (x.max || 0), 0)
    : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-3 gap-3">
        <ScoreStat
          label="Score"
          value={score !== null ? `${score}${max ? ` / ${max}` : ""}` : "—"}
        />
        <ScoreStat
          label="Coverage"
          value={grade.rubric_coverage_passed ? "Passed" : "Gaps"}
          tone={grade.rubric_coverage_passed ? "success" : "warning"}
        />
        <ScoreStat
          label="Anti-patterns"
          value={String(grade.detected_patterns.length)}
          tone={grade.detected_patterns.length === 0 ? "success" : "warning"}
        />
      </div>

      {content.summary && (
        <p className="font-serif text-sm leading-relaxed">{content.summary}</p>
      )}

      {content.rubric_coverage && content.rubric_coverage.length > 0 && (
        <section>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            Rubric coverage
          </p>
          <ul className="mt-2 space-y-1 text-sm">
            {content.rubric_coverage.map((c, idx) => (
              <li key={idx} className="flex items-start gap-2">
                <span className={c.covered ? "text-success" : "text-destructive"}>
                  {c.covered ? "✓" : "✗"}
                </span>
                <span>
                  <strong>{c.requirement}</strong>
                  {c.note ? <span className="text-muted-foreground"> — {c.note}</span> : null}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {grade.detected_patterns.length > 0 && (
        <section>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            Anti-patterns detected
          </p>
          <ul className="mt-2 space-y-2 text-sm">
            {grade.detected_patterns.map((p, idx) => (
              <li
                key={idx}
                className={cn(
                  "border px-3 py-2",
                  p.severity === "high"
                    ? "border-destructive/40 bg-destructive/5"
                    : p.severity === "medium"
                      ? "border-amber-500/40 bg-amber-500/5"
                      : "border-border bg-card",
                )}
              >
                <p className="font-medium">
                  {p.name} <span className="text-xs text-muted-foreground">· {p.severity}</span>
                </p>
                <p className="mt-0.5 text-xs italic text-muted-foreground">
                  {p.excerpt}
                </p>
                <p className="mt-1">{p.message}</p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {content.suggestions && content.suggestions.length > 0 && (
        <section>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            Suggestions
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
            {content.suggestions.map((s, idx) => (
              <li key={idx}>{s}</li>
            ))}
          </ul>
        </section>
      )}

      <details>
        <summary className="cursor-pointer text-xs uppercase tracking-[0.14em] text-muted-foreground">
          Your answer
        </summary>
        <pre className="mt-2 whitespace-pre-wrap rounded-sm bg-muted px-3 py-2 font-serif text-sm">
          {answer}
        </pre>
      </details>

      <div className="flex gap-2">
        <Button variant="outline" onClick={onRetry}>
          Edit &amp; re-submit
        </Button>
        <Link href={`/artifacts/${grade.grade_artifact.id}`}>
          <Button variant="ghost">Open grade artifact →</Button>
        </Link>
      </div>
    </div>
  );
}

function ScoreStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "success" | "warning";
}) {
  return (
    <div
      className={cn(
        "border bg-card px-3 py-3",
        tone === "success" && "border-success/40 bg-success/5",
        tone === "warning" && "border-amber-500/40 bg-amber-500/5",
        !tone && "border-border",
      )}
    >
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </p>
      <p className="mt-0.5 font-serif text-2xl font-semibold tabular-nums">
        {value}
      </p>
    </div>
  );
}
