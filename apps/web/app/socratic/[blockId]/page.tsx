"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import * as React from "react";

import { ChatPanel } from "@/components/ChatPanel";
import { Spinner } from "@/components/Spinner";

/**
 * Socratic drill page. Path: `/socratic/{block_id}?corpus_id=...`.
 *
 * `corpus_id` comes in via querystring because the chat-turn endpoint requires
 * it and we don't have a separate "look up corpus by block_id" route. The
 * cases tab on the corpus-detail page sends it along when it links here.
 */

export default function SocraticPage(props: {
  params: Promise<{ blockId: string }>;
}) {
  const { blockId } = React.use(props.params);
  const search = useSearchParams();
  const corpusId = search.get("corpus_id");
  const profileId = search.get("profile_id");

  if (!corpusId) {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <p className="text-sm text-destructive">
          Missing <code>corpus_id</code> querystring. Open this page from the
          cases tab inside the corpus dashboard, not directly.
        </p>
        <Link href="/" className="law-link mt-3 inline-block underline">
          ← Dashboard
        </Link>
      </main>
    );
  }

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-8">
      <Link
        href={`/corpora/${corpusId}`}
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← Corpus
      </Link>
      <React.Suspense
        fallback={
          <div className="mt-8 flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner size="sm" /> Loading drill…
          </div>
        }
      >
        <div className="mt-4">
          <ChatPanel
            corpusId={corpusId}
            caseBlockId={blockId}
            mode="socratic"
            professorProfileId={profileId}
            backHref={`/corpora/${corpusId}`}
          />
        </div>
      </React.Suspense>
    </main>
  );
}
