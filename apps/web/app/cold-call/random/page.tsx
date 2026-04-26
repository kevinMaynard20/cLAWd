"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { LoadingButton } from "@/components/LoadingButton";
import { PageNumberInput } from "@/components/PageNumberInput";
import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { api, ApiError } from "@/lib/api";

/**
 * Cold-call random picker. Pick a book + page range, server picks one
 * CASE_OPINION block at random, redirect into the chat UI for that case.
 *
 * Backend: `GET /books/{book_id}/cases?random=true&page_start=&page_end=`
 * returns a single-element list. We then push to `/cold-call/{block_id}`.
 */

type BookSummary = {
  id: string;
  title: string;
  source_page_min: number;
  source_page_max: number;
};

type CaseRow = {
  block_id: string;
  case_name: string;
  source_page: number;
};

type CasesResponse = {
  cases: CaseRow[];
};

export default function ColdCallRandomPage() {
  const router = useRouter();
  const search = useSearchParams();

  const initialCorpus = search.get("corpus_id") ?? "";
  const initialBook = search.get("book_id") ?? "";
  const initialStart = search.get("page_start");
  const initialEnd = search.get("page_end");

  const [corpora, setCorpora] = React.useState<
    Array<{ id: string; name: string; course: string }> | null
  >(null);
  const [corpusId, setCorpusId] = React.useState(initialCorpus);
  const [books, setBooks] = React.useState<BookSummary[] | null>(null);
  const [bookId, setBookId] = React.useState(initialBook);
  const [pageStart, setPageStart] = React.useState<number | null>(
    initialStart ? Number(initialStart) : null,
  );
  const [pageEnd, setPageEnd] = React.useState<number | null>(
    initialEnd ? Number(initialEnd) : null,
  );

  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.get<
          Array<{ id: string; name: string; course: string }>
        >("/corpora");
        if (!cancelled) setCorpora(list);
      } catch (err) {
        if (cancelled) return;
        setCorpora([]);
        setError(err instanceof ApiError ? err.message : "Could not load corpora.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  React.useEffect(() => {
    if (!corpusId) {
      setBooks(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const list = await api.get<BookSummary[]>(`/corpora/${corpusId}/books`);
        if (cancelled) return;
        setBooks(list);
        // Auto-select the first book if none selected yet.
        if (list.length > 0 && !bookId) {
          setBookId(list[0].id);
        }
      } catch (err) {
        if (cancelled) return;
        setBooks([]);
        setError(err instanceof ApiError ? err.message : "Could not load books.");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corpusId]);

  // Default page range to the selected book's full span if none provided.
  React.useEffect(() => {
    if (!books) return;
    const b = books.find((x) => x.id === bookId);
    if (!b) return;
    if (pageStart === null) setPageStart(b.source_page_min);
    if (pageEnd === null) setPageEnd(b.source_page_max);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [books, bookId]);

  const start = async () => {
    if (!corpusId || !bookId || pageStart === null || pageEnd === null) {
      setError("Pick a corpus + book + page range first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.get<CasesResponse>(`/books/${bookId}/cases`, {
        random: true,
        page_start: pageStart,
        page_end: pageEnd,
      });
      const pick = res.cases[0];
      if (!pick) {
        setError("No case opinions in that page range.");
        return;
      }
      router.push(
        `/cold-call/${pick.block_id}?corpus_id=${encodeURIComponent(corpusId)}`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not pick a case.");
    } finally {
      setBusy(false);
    }
  };

  const selectedBook = books?.find((b) => b.id === bookId) ?? null;

  return (
    <main className="mx-auto w-full max-w-3xl px-6 py-10">
      <Link
        href={corpusId ? `/corpora/${corpusId}` : "/"}
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← {corpusId ? "Corpus" : "Dashboard"}
      </Link>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        Cold call — random case
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Pick a book + page range. Server picks one case at random and drops
        you into the chat under pressure.
      </p>

      <div className="mt-8 flex flex-col gap-4 border border-border bg-card p-4">
        <div className="flex flex-col gap-1.5">
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

        <div className="flex flex-col gap-1.5">
          <Label>Book</Label>
          {books === null ? (
            <p className="text-xs text-muted-foreground">
              Pick a corpus first.
            </p>
          ) : books.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No books in this corpus. Ingest a casebook on the upload page.
            </p>
          ) : (
            <Select value={bookId} onChange={(e) => setBookId(e.target.value)}>
              <option value="">Select a book…</option>
              {books.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.title} (pp. {b.source_page_min}–{b.source_page_max})
                </option>
              ))}
            </Select>
          )}
        </div>

        {selectedBook && (
          <div className="flex flex-wrap items-center gap-3">
            <Label>Pages</Label>
            <PageNumberInput
              value={pageStart}
              onCommit={setPageStart}
              min={selectedBook.source_page_min}
              max={selectedBook.source_page_max}
              fallback={selectedBook.source_page_min}
            />
            <span className="text-muted-foreground">to</span>
            <PageNumberInput
              value={pageEnd}
              onCommit={setPageEnd}
              min={selectedBook.source_page_min}
              max={selectedBook.source_page_max}
              fallback={selectedBook.source_page_max}
            />
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setPageStart(selectedBook.source_page_min);
                setPageEnd(selectedBook.source_page_max);
              }}
            >
              Whole book
            </Button>
          </div>
        )}

        {error && <p className="text-sm text-destructive">{error}</p>}

        <div>
          <LoadingButton onClick={() => void start()} loading={busy}>
            Pick a case &amp; start
          </LoadingButton>
        </div>
      </div>
    </main>
  );
}
