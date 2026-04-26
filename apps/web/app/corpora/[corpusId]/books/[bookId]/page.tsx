"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";

import { PageNumberInput } from "@/components/PageNumberInput";
import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

/**
 * Book detail page. Two halves:
 *   - Left: case index for the book (page-range filter at top, list below).
 *     Each row has one-click action buttons (brief / drill / cold-call).
 *   - Right: page-range action sidebar that operates on whatever range is set:
 *     brief the dominant case, generate flashcards, generate MCQs, build a hypo.
 *
 * Source-page numbers (the printed numbers) are the ONLY numbers shown — never
 * pdf-page indices (spec §2.3).
 */

type BookSummary = {
  id: string;
  title: string;
  edition: string | null;
  authors: string[];
  source_page_min: number;
  source_page_max: number;
  ingested_at: string;
};

type CaseRow = {
  block_id: string;
  case_name: string;
  source_page: number;
  court: string | null;
  year: number | null;
  citation: string | null;
  judge: string | null;
  excerpt: string;
};

type CasesResponse = {
  book_id: string;
  book_title: string;
  count: number;
  cases: CaseRow[];
};

export default function BookDetailPage(props: {
  params: Promise<{ corpusId: string; bookId: string }>;
}) {
  const { corpusId, bookId } = React.use(props.params);
  const router = useRouter();

  const [book, setBook] = React.useState<BookSummary | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const [pageStart, setPageStart] = React.useState<number | null>(null);
  const [pageEnd, setPageEnd] = React.useState<number | null>(null);
  const [cases, setCases] = React.useState<CaseRow[] | null>(null);

  const [pendingAction, setPendingAction] = React.useState<string | null>(null);

  // Hydrate book metadata; default the page range to the whole book.
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.get<BookSummary[]>(`/corpora/${corpusId}/books`);
        const found = list.find((b) => b.id === bookId) ?? null;
        if (!cancelled) {
          if (!found) {
            setError("Book not found in this corpus.");
            return;
          }
          setBook(found);
          setPageStart(found.source_page_min);
          setPageEnd(found.source_page_max);
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load book.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [corpusId, bookId]);

  // Fetch cases whenever the page range changes.
  React.useEffect(() => {
    if (pageStart === null || pageEnd === null) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get<CasesResponse>(`/books/${bookId}/cases`, {
          page_start: pageStart,
          page_end: pageEnd,
        });
        if (!cancelled) setCases(res.cases);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load cases.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bookId, pageStart, pageEnd]);

  if (error) {
    return (
      <main className="mx-auto max-w-5xl px-6 py-12">
        <p className="text-destructive">{error}</p>
      </main>
    );
  }
  if (!book || pageStart === null || pageEnd === null) {
    return (
      <main className="mx-auto flex max-w-5xl items-center gap-2 px-6 py-12 text-sm text-muted-foreground">
        <Spinner size="sm" /> Loading book…
      </main>
    );
  }

  const briefRange = async () => {
    setPendingAction("brief");
    try {
      const res = await api.post<{ artifact: { id: string } }>("/features/case-brief", {
        corpus_id: corpusId,
        book_id: bookId,
        page_start: pageStart,
        page_end: pageEnd,
      });
      router.push(`/artifacts/${res.artifact.id}`);
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Brief failed.");
      setPendingAction(null);
    }
  };

  const flashcards = async () => {
    setPendingAction("flashcards");
    try {
      const res = await api.post<{ artifact: { id: string } }>("/features/flashcards", {
        corpus_id: corpusId,
        book_id: bookId,
        page_start: pageStart,
        page_end: pageEnd,
        num_cards: 12,
      });
      router.push(`/artifacts/${res.artifact.id}`);
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Flashcards failed.");
      setPendingAction(null);
    }
  };

  const mcq = async () => {
    setPendingAction("mcq");
    try {
      const topic = window.prompt("Topic for these MCQs?", "");
      if (topic === null || topic.trim() === "") {
        setPendingAction(null);
        return;
      }
      const res = await api.post<{ artifact: { id: string } }>("/features/mc-questions", {
        corpus_id: corpusId,
        book_id: bookId,
        page_start: pageStart,
        page_end: pageEnd,
        topic,
        num_questions: 10,
      });
      router.push(`/artifacts/${res.artifact.id}`);
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "MCQ generation failed.");
      setPendingAction(null);
    }
  };

  const coldCallRandom = () => {
    router.push(
      `/cold-call/random?corpus_id=${corpusId}&book_id=${bookId}&page_start=${pageStart}&page_end=${pageEnd}`,
    );
  };

  return (
    <main className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-8 px-6 py-10 lg:grid-cols-[1fr_300px]">
      <section>
        <Link
          href={`/corpora/${corpusId}`}
          className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
        >
          ← Corpus
        </Link>
        <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
          {book.title}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {(book.authors || []).join(", ")}
          {book.edition ? ` · ${book.edition} ed.` : ""}
          {" · "}pp. {book.source_page_min}–{book.source_page_max}
          {" · "}ingested {formatRelativeTime(book.ingested_at)}
        </p>

        <div className="mt-6 flex items-center gap-3 border border-border bg-card px-3 py-2 text-sm">
          <label htmlFor="ps" className="font-medium">
            Pages
          </label>
          <PageNumberInput
            id="ps"
            value={pageStart}
            onCommit={setPageStart}
            min={book.source_page_min}
            max={book.source_page_max}
            fallback={book.source_page_min}
          />
          <span className="text-muted-foreground">to</span>
          <PageNumberInput
            id="pe"
            value={pageEnd}
            onCommit={setPageEnd}
            min={book.source_page_min}
            max={book.source_page_max}
            fallback={book.source_page_max}
          />
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setPageStart(book.source_page_min);
              setPageEnd(book.source_page_max);
            }}
          >
            Reset
          </Button>
        </div>

        <h2 className="mt-8 font-serif text-xl font-semibold tracking-tight">
          Cases in pp. {pageStart}–{pageEnd}
        </h2>
        {cases === null ? (
          <div className="mt-4 flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner size="sm" /> Loading…
          </div>
        ) : cases.length === 0 ? (
          <p className="mt-4 text-sm text-muted-foreground">
            No case opinions in this range.
          </p>
        ) : (
          <ul className="mt-4 divide-y divide-border border border-border bg-card">
            {cases.map((c) => (
              <CaseListItem
                key={c.block_id}
                row={c}
                corpusId={corpusId}
              />
            ))}
          </ul>
        )}
      </section>

      <aside className="lg:sticky lg:top-6 lg:h-fit">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
          Range actions
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          All operate on pp. {pageStart}–{pageEnd}.
        </p>
        <div className="mt-3 flex flex-col gap-2">
          <Button onClick={() => void briefRange()} disabled={pendingAction !== null}>
            {pendingAction === "brief" ? "Briefing…" : "Brief first case in range"}
          </Button>
          <Button variant="outline" onClick={() => void flashcards()} disabled={pendingAction !== null}>
            {pendingAction === "flashcards" ? "Generating…" : "Flashcards"}
          </Button>
          <Button variant="outline" onClick={() => void mcq()} disabled={pendingAction !== null}>
            {pendingAction === "mcq" ? "Generating…" : "Multiple-choice questions"}
          </Button>
          <Button variant="outline" onClick={coldCallRandom} disabled={pendingAction !== null}>
            Cold-call random
          </Button>
        </div>
      </aside>
    </main>
  );
}

function CaseListItem({
  row,
  corpusId,
}: {
  row: CaseRow;
  corpusId: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = React.useState<string | null>(null);

  const briefThis = async () => {
    setBusy("brief");
    try {
      const res = await api.post<{ artifact: { id: string } }>("/features/case-brief", {
        corpus_id: corpusId,
        block_id: row.block_id,
      });
      router.push(`/artifacts/${res.artifact.id}`);
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Brief failed.");
      setBusy(null);
    }
  };

  return (
    <li className="grid grid-cols-[1fr_auto] items-start gap-3 px-4 py-3">
      <div className="min-w-0">
        <p className="font-serif text-base font-semibold">{row.case_name}</p>
        <p className="mt-0.5 text-[11px] tabular-nums text-muted-foreground">
          p. {row.source_page}
          {row.court ? ` · ${row.court}` : ""}
          {row.year ? ` · ${row.year}` : ""}
          {row.judge ? ` · ${row.judge}` : ""}
        </p>
        <p className="mt-1 text-xs text-muted-foreground/90 line-clamp-2">
          {row.excerpt}
        </p>
      </div>
      <div className="flex flex-shrink-0 flex-col gap-1.5">
        <Button
          size="sm"
          variant="outline"
          disabled={busy !== null}
          onClick={() => void briefThis()}
        >
          {busy === "brief" ? "…" : "Brief"}
        </Button>
        <Link href={`/socratic/${row.block_id}?corpus_id=${corpusId}`}>
          <Button size="sm" variant="ghost" className="w-full">
            Drill
          </Button>
        </Link>
        <Link href={`/cold-call/${row.block_id}?corpus_id=${corpusId}`}>
          <Button size="sm" variant="ghost" className="w-full">
            Cold-call
          </Button>
        </Link>
      </div>
    </li>
  );
}
