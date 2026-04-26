"use client";

import * as React from "react";

import { Spinner } from "@/components/Spinner";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Global search across blocks, transcript segments, and artifacts. Backend:
 * `GET /api/search?q=...&kinds=...&corpus_id=...`. The page debounces the
 * query at 400 ms and treats the kinds filter checkboxes as "include".
 *
 * No reading view exists yet, so result cards are display-only — we render
 * the snippet with the matched query bold and surface kind/score for context.
 */

type Kind = "block" | "transcript_segment" | "artifact";

type SearchResult = {
  kind: string;
  id: string;
  corpus_id: string;
  source_context: string;
  snippet: string;
  score: number;
  source_location: Record<string, unknown>;
};

type SearchResponse = {
  query: string;
  count: number;
  results: SearchResult[];
};

type CorpusSummary = {
  id: string;
  name: string;
  course: string;
};

const ALL_KINDS: Kind[] = ["block", "transcript_segment", "artifact"];

export default function SearchPage() {
  const [q, setQ] = React.useState("");
  const [kinds, setKinds] = React.useState<Set<Kind>>(new Set(ALL_KINDS));
  const [corpora, setCorpora] = React.useState<CorpusSummary[]>([]);
  const [corpusId, setCorpusId] = React.useState<string>("");

  const [committedQ, setCommittedQ] = React.useState("");
  const [response, setResponse] = React.useState<SearchResponse | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const inputRef = React.useRef<HTMLInputElement>(null);

  // Autofocus the query field on mount.
  React.useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Load corpora once (small list).
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.get<CorpusSummary[]>("/corpora");
        if (!cancelled) setCorpora(data);
      } catch {
        // Non-fatal; the dropdown will just show "All corpora".
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Debounce the live query at 400 ms.
  React.useEffect(() => {
    const handle = setTimeout(() => setCommittedQ(q.trim()), 400);
    return () => clearTimeout(handle);
  }, [q]);

  // Run the search whenever the committed query or filters change.
  React.useEffect(() => {
    if (committedQ.length === 0) {
      setResponse(null);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const kindsParam =
          kinds.size === ALL_KINDS.length
            ? undefined
            : Array.from(kinds).join(",");
        const res = await api.get<SearchResponse>("/search", {
          q: committedQ,
          kinds: kindsParam,
          corpus_id: corpusId || undefined,
        });
        if (cancelled) return;
        setResponse(res);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError ? err.message : "Search failed.",
        );
        setResponse(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [committedQ, kinds, corpusId]);

  const toggleKind = (kind: Kind) =>
    setKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });

  const submitImmediate = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCommittedQ(q.trim());
  };

  return (
    <main className="mx-auto w-full max-w-4xl px-6 py-10">
      <header>
        <p className="font-serif text-xs uppercase tracking-[0.18em] text-muted-foreground">
          Workspace · Search
        </p>
        <h1 className="mt-2 font-serif text-2xl font-semibold tracking-tight">
          Global search
        </h1>
        <p className="mt-2 max-w-prose text-sm leading-relaxed text-muted-foreground">
          BM25 + semantic match across casebook blocks, lecture transcripts,
          and generated artifacts. Live as you type.
        </p>
      </header>

      <form
        onSubmit={submitImmediate}
        className="mt-6 flex flex-col gap-4 border border-border bg-card p-4"
      >
        <Input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="adverse possession, mental state of trespasser…"
          className="font-serif text-base"
          aria-label="Search query"
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_220px]">
          <fieldset className="flex flex-col gap-2">
            <legend className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">
              Kinds
            </legend>
            <div className="flex flex-wrap items-center gap-3 text-sm">
              {ALL_KINDS.map((k) => (
                <label
                  key={k}
                  className="inline-flex cursor-pointer items-center gap-2 text-foreground"
                >
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 accent-accent"
                    checked={kinds.has(k)}
                    onChange={() => toggleKind(k)}
                  />
                  <span className="font-mono text-xs tracking-tight">{k}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="corpus-filter">Corpus</Label>
            <Select
              id="corpus-filter"
              value={corpusId}
              onChange={(e) => setCorpusId(e.target.value)}
            >
              <option value="">All corpora</option>
              {corpora.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} — {c.course}
                </option>
              ))}
            </Select>
          </div>
        </div>
      </form>

      <div className="mt-6 flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {response
            ? `${response.count.toLocaleString()} result${response.count === 1 ? "" : "s"}`
            : ""}
        </span>
        {loading && (
          <span className="inline-flex items-center gap-2">
            <Spinner size="sm" />
            Searching…
          </span>
        )}
      </div>

      {error && (
        <div
          role="alert"
          className="mt-4 border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </div>
      )}

      <ResultList
        committedQ={committedQ}
        response={response}
        loading={loading}
      />
    </main>
  );
}

function ResultList({
  committedQ,
  response,
  loading,
}: {
  committedQ: string;
  response: SearchResponse | null;
  loading: boolean;
}) {
  if (committedQ.length === 0) {
    return (
      <p className="mt-10 text-center text-sm text-muted-foreground">
        No results yet — type a query to begin.
      </p>
    );
  }
  if (loading && !response) {
    return null; // The header spinner already conveys loading.
  }
  if (!response || response.results.length === 0) {
    return (
      <p className="mt-10 text-center text-sm text-muted-foreground">
        No matches.
      </p>
    );
  }
  return (
    <div className="mt-4 flex flex-col gap-0 border-t border-border">
      {response.results.map((r) => (
        <ResultCard key={`${r.kind}:${r.id}`} result={r} query={committedQ} />
      ))}
    </div>
  );
}

function ResultCard({
  result,
  query,
}: {
  result: SearchResult;
  query: string;
}) {
  return (
    <article className="grid grid-cols-[1fr_auto] gap-3 border-b border-border px-4 py-4">
      <div className="min-w-0">
        <p className="text-xs uppercase tracking-[0.08em] text-muted-foreground">
          {result.source_context}
        </p>
        <p className="mt-2 font-serif text-base leading-relaxed text-foreground">
          {highlight(result.snippet, query)}
        </p>
        <p className="mt-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
          id: {result.id.slice(0, 12)}
        </p>
      </div>
      <div className="flex flex-col items-end gap-2 text-xs">
        <Badge variant={kindVariant(result.kind)}>{result.kind}</Badge>
        <span className="tabular-nums text-muted-foreground">
          score {result.score.toFixed(3)}
        </span>
      </div>
    </article>
  );
}

function kindVariant(
  kind: string,
):
  | "default"
  | "muted"
  | "success"
  | "warning"
  | "destructive"
  | "accent"
  | "outline" {
  if (kind === "block") return "accent";
  if (kind === "transcript_segment") return "muted";
  if (kind === "artifact") return "success";
  return "outline";
}

/**
 * Split a snippet by the query (case-insensitive) and bold the matches.
 * For an empty query we return the unmodified text — we never want to render
 * the entire string as one match block.
 */
function highlight(text: string, query: string): React.ReactNode {
  if (!query) return text;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: Array<{ kind: "match" | "plain"; text: string }> = [];
  let i = 0;
  while (i < text.length) {
    const next = lower.indexOf(needle, i);
    if (next < 0) {
      parts.push({ kind: "plain", text: text.slice(i) });
      break;
    }
    if (next > i) parts.push({ kind: "plain", text: text.slice(i, next) });
    parts.push({ kind: "match", text: text.slice(next, next + needle.length) });
    i = next + needle.length;
  }
  return parts.map((p, idx) =>
    p.kind === "match" ? (
      <strong
        key={idx}
        className={cn("font-semibold text-foreground")}
      >
        {p.text}
      </strong>
    ) : (
      <React.Fragment key={idx}>{p.text}</React.Fragment>
    ),
  );
}
