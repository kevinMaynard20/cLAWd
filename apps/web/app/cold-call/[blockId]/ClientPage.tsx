"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import * as React from "react";

import { ChatPanel } from "@/components/ChatPanel";
import { Spinner } from "@/components/Spinner";

/**
 * Cold-call page. Path: `/cold-call/{block_id}?corpus_id=...`. Same shape as
 * Socratic but the chat panel exposes the "End & debrief" button, which fires
 * `/features/cold-call/debrief` and shows the closing artifact inline.
 */

export default function ColdCallPage(props: {
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
          Missing <code>corpus_id</code> querystring. Use the cases tab or the
          random cold-call entry to launch a session.
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
            <Spinner size="sm" /> Loading cold call…
          </div>
        }
      >
        <div className="mt-4">
          <ChatPanel
            corpusId={corpusId}
            caseBlockId={blockId}
            mode="cold-call"
            professorProfileId={profileId}
            backHref={`/corpora/${corpusId}`}
          />
        </div>
      </React.Suspense>
    </main>
  );
}
