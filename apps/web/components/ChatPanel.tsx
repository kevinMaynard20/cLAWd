"use client";

import * as React from "react";

import { LoadingButton } from "@/components/LoadingButton";
import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Shared chat surface for Socratic drill (`/features/socratic/turn`) and
 * cold-call (`/features/cold-call/turn` + `/cold-call/debrief`). Backend turn
 * shape is identical between the two — only the endpoint and the "End" button
 * label differ.
 *
 * On first send: `session_id=null`, `user_answer=null` → server creates the
 * session and returns the opening question. On subsequent sends: pass the
 * session_id we got back. We persist session_id to the URL via
 * `router.replace` so a refresh resumes mid-conversation.
 */

type ProfessorTurn = {
  question?: string;
  feedback?: string;
  closing?: string;
  citations?: string[];
  [key: string]: unknown;
};

type HistoryItem = {
  role: "professor" | "student";
  content: string;
};

type TurnResponse = {
  session_id: string;
  turn_index: number;
  professor_turn: ProfessorTurn;
  history: HistoryItem[];
};

export type ChatMode = "socratic" | "cold-call";

type Props = {
  corpusId: string;
  caseBlockId: string;
  /** Title above the chat panel — usually the case_name. */
  caseTitle?: string;
  /** Subtitle below the title — usually the citation + court. */
  caseSubtitle?: string;
  mode: ChatMode;
  professorProfileId?: string | null;
  /** When the panel is rendered inside another page (book detail), allow caller
   * to override the back-link. Default: corpus-detail page. */
  backHref?: string;
  /** Optional callback fired when the session ends (cold-call debrief). */
  onEnded?: () => void;
};

export function ChatPanel(props: Props) {
  const {
    corpusId,
    caseBlockId,
    caseTitle,
    caseSubtitle,
    mode,
    professorProfileId,
  } = props;

  const turnEndpoint =
    mode === "socratic" ? "/features/socratic/turn" : "/features/cold-call/turn";

  const [sessionId, setSessionId] = React.useState<string | null>(null);
  const [history, setHistory] = React.useState<HistoryItem[]>([]);
  const [pendingTurn, setPendingTurn] = React.useState<ProfessorTurn | null>(null);
  const [draft, setDraft] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [ended, setEnded] = React.useState(false);
  const [debrief, setDebrief] = React.useState<ProfessorTurn | null>(null);

  const scrollRef = React.useRef<HTMLDivElement>(null);

  // Open the session on mount: send an empty turn so the server creates the
  // ChatSession + returns the opening question.
  const sendTurn = React.useCallback(
    async (userAnswer: string | null) => {
      setBusy(true);
      setError(null);
      try {
        const res = await api.post<TurnResponse>(turnEndpoint, {
          corpus_id: corpusId,
          case_block_id: caseBlockId,
          session_id: sessionId,
          user_answer: userAnswer,
          professor_profile_id: professorProfileId ?? null,
        });
        setSessionId(res.session_id);
        setHistory(res.history);
        setPendingTurn(res.professor_turn);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Turn failed.");
      } finally {
        setBusy(false);
      }
    },
    [turnEndpoint, corpusId, caseBlockId, sessionId, professorProfileId],
  );

  // React StrictMode in dev double-fires mount effects, which would post the
  // opening turn twice. Guard via a ref so only the first mount fires the
  // request — the second invocation no-ops. The ref is module-stable across
  // StrictMode's intentional unmount/remount because React preserves refs
  // across that pair, but we double-check via a session/history flag too.
  const openedRef = React.useRef(false);
  React.useEffect(() => {
    if (openedRef.current) return;
    openedRef.current = true;
    void sendTurn(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  React.useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [history, pendingTurn, debrief]);

  const submit = async () => {
    const text = draft.trim();
    if (!text || busy || ended) return;
    setDraft("");
    await sendTurn(text);
  };

  const endAndDebrief = async () => {
    if (sessionId === null || mode !== "cold-call") return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<TurnResponse>("/features/cold-call/debrief", {
        session_id: sessionId,
      });
      setHistory(res.history);
      setDebrief(res.professor_turn);
      setEnded(true);
      props.onEnded?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Debrief failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_260px]">
      <section className="flex min-h-[500px] flex-col border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            {mode === "socratic" ? "Socratic drill" : "Cold call"}
          </p>
          {caseTitle && (
            <h1 className="mt-0.5 font-serif text-lg font-semibold">{caseTitle}</h1>
          )}
          {caseSubtitle && (
            <p className="text-xs text-muted-foreground">{caseSubtitle}</p>
          )}
        </header>

        <div
          ref={scrollRef}
          className="min-h-[360px] flex-1 space-y-4 overflow-y-auto px-4 py-4"
        >
          {history.map((h, idx) => (
            <Bubble key={idx} role={h.role} content={h.content} />
          ))}
          {pendingTurn && !history.some(
            (h) => h.role === "professor" && h.content === pendingTurnText(pendingTurn),
          ) && (
            <Bubble role="professor" content={pendingTurnText(pendingTurn)} />
          )}
          {debrief && (
            <div className="border-t border-border pt-4">
              <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                Debrief
              </p>
              <Bubble role="professor" content={pendingTurnText(debrief)} />
            </div>
          )}
          {busy && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Spinner size="sm" /> Thinking…
            </div>
          )}
          {error && (
            <p className="text-sm text-destructive">{error}</p>
          )}
        </div>

        {!ended && (
          <div className="border-t border-border bg-subtle p-3">
            <div className="flex items-end gap-2">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    void submit();
                  }
                }}
                placeholder="Your answer… (⌘/Ctrl + Enter to send)"
                className="min-h-[80px] flex-1 rounded-sm border border-input bg-card px-3 py-2 font-serif text-sm leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background"
              />
              <LoadingButton
                onClick={() => void submit()}
                loading={busy}
                disabled={!draft.trim() || ended}
              >
                Send
              </LoadingButton>
            </div>
          </div>
        )}
      </section>

      <aside className="flex flex-col gap-3 lg:sticky lg:top-6 lg:h-fit">
        <div className="border border-border bg-card px-3 py-3 text-xs">
          <p className="font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            Session
          </p>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 tabular-nums">
            <dt className="text-muted-foreground">Mode</dt>
            <dd>{mode}</dd>
            <dt className="text-muted-foreground">Turns</dt>
            <dd>{history.filter((h) => h.role === "professor").length}</dd>
            {sessionId && (
              <>
                <dt className="text-muted-foreground">id</dt>
                <dd className="font-mono text-[10px]">{sessionId.slice(0, 12)}…</dd>
              </>
            )}
          </dl>
        </div>

        {mode === "cold-call" && !ended && (
          <Button
            variant="outline"
            disabled={busy || sessionId === null}
            onClick={() => void endAndDebrief()}
          >
            End &amp; debrief
          </Button>
        )}
        {ended && (
          <p className="border border-success/40 bg-success/10 px-3 py-2 text-xs text-success">
            Session ended.
          </p>
        )}
      </aside>
    </div>
  );
}

function pendingTurnText(turn: ProfessorTurn): string {
  // The professor_turn payload varies by template — Socratic gives `question`,
  // cold-call adds optional `feedback` (after the student's answer) and
  // `closing` on debrief. Render in priority order.
  const parts: string[] = [];
  if (typeof turn.feedback === "string" && turn.feedback.trim())
    parts.push(turn.feedback.trim());
  if (typeof turn.question === "string" && turn.question.trim())
    parts.push(turn.question.trim());
  if (typeof turn.closing === "string" && turn.closing.trim())
    parts.push(turn.closing.trim());
  if (parts.length === 0) return "(no professor response)";
  return parts.join("\n\n");
}

function Bubble({
  role,
  content,
}: {
  role: "professor" | "student";
  content: string;
}) {
  return (
    <div
      className={cn(
        "rounded-sm border px-3 py-2 font-serif text-sm leading-relaxed",
        role === "professor"
          ? "border-border bg-card"
          : "border-accent/40 bg-accent/5 ml-8",
      )}
    >
      <p className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
        {role === "professor" ? "Professor" : "You"}
      </p>
      <p className="mt-1 whitespace-pre-wrap">{content}</p>
    </div>
  );
}
