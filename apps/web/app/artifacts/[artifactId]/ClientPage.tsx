"use client";

import Link from "next/link";
import * as React from "react";

import { ArtifactMarkdown } from "@/components/ArtifactMarkdown";
import { PageNumberInput } from "@/components/PageNumberInput";
import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { formatRelativeTime, formatUsd } from "@/lib/format";

/**
 * Generic artifact viewer. Renders `content.markdown` if present, otherwise
 * pretty-prints the JSON. Side panel: type + cost + lineage link + per-feature
 * actions ("What if?" for case briefs, "Open synthesis" for syntheses, etc.).
 */

type ArtifactDetail = {
  id: string;
  corpus_id: string;
  type: string;
  created_at: string;
  sources: Array<{ kind: string; id: string }>;
  content: Record<string, unknown>;
  prompt_template: string;
  llm_model: string;
  cost_usd: string;
  cache_key: string;
  parent_artifact_id: string | null;
  markdown: string | null;
  title: string;
};

// Matches schemas/what_if_variations.json — the per-variation shape.
type WhatIfVariation = {
  id: string;
  fact_changed: string;
  consequence: string;
  doctrinal_reason: string;
  tests_understanding_of: string;
};

export default function ArtifactPage(props: {
  params: Promise<{ artifactId: string }>;
}) {
  const { artifactId } = React.use(props.params);
  const [a, setA] = React.useState<ArtifactDetail | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const detail = await api.get<ArtifactDetail>(`/artifacts/${artifactId}`);
        if (!cancelled) setA(detail);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load artifact.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifactId]);

  if (error) {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <p className="text-destructive">{error}</p>
      </main>
    );
  }
  if (!a) {
    return (
      <main className="mx-auto flex max-w-4xl items-center gap-2 px-6 py-12 text-sm text-muted-foreground">
        <Spinner size="sm" /> Loading artifact…
      </main>
    );
  }

  const md =
    renderTypedArtifact(a.type, a.content, a.title) ??
    a.markdown ??
    extractFallbackMarkdown(a.content) ??
    `\`\`\`json\n${JSON.stringify(a.content, null, 2)}\n\`\`\``;

  return (
    <main className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-8 px-6 py-10 lg:grid-cols-[1fr_280px]">
      <article className="min-w-0">
        <Link
          href={`/corpora/${a.corpus_id}`}
          className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
        >
          ← Corpus
        </Link>
        <p className="mt-3 text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
          {a.type.replace(/_/g, " ")}
        </p>
        <h1 className="mt-1 font-serif text-3xl font-semibold tracking-tight">
          {a.title}
        </h1>
        <p className="mt-2 text-xs tabular-nums text-muted-foreground">
          {formatRelativeTime(a.created_at)} · {formatUsd(a.cost_usd)} ·{" "}
          {a.llm_model || "no model"} · {a.prompt_template || "no template"}
        </p>

        {a.content?.from_general_knowledge === true && (
          <div className="mt-4 border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm">
            <p className="font-medium">From general knowledge — not your casebook.</p>
            <p className="mt-1 text-muted-foreground">
              The casebook text for this case wasn&apos;t available (the
              ingestion pipeline produced an empty case-opinion block), so
              this brief was generated from the model&apos;s general knowledge
              of US case law. Cross-check key wording against the printed
              opinion before relying on quoted rule language.
            </p>
          </div>
        )}

        <div className="mt-6">
          <ArtifactMarkdown markdown={md} />
        </div>

        {a.type === "case_brief" && <WhatIfPanel artifact={a} />}
      </article>

      <aside className="flex flex-col gap-3">
        <SidePanel a={a} />
      </aside>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Per-type renderers turn the structured artifact payloads (spec §5.x) into
// the kind of markdown a 1L actually reads. Where the model commonly emits
// alias field names (e.g. `fact_pattern` instead of `trigger`,
// `case`/`holding` instead of `case_name`/`one_line_holding`), each
// renderer accepts BOTH so the user sees real content even when the model
// drifts off the schema's exact field names.
// ---------------------------------------------------------------------------

function renderTypedArtifact(
  type: string,
  content: Record<string, unknown>,
  fallbackTitle: string,
): string | null {
  switch (type) {
    case "case_brief":
      return renderCaseBriefMarkdown(content, fallbackTitle);
    case "attack_sheet":
      return renderAttackSheetMarkdown(content, fallbackTitle);
    case "synthesis":
      return renderSynthesisMarkdown(content, fallbackTitle);
    case "outline":
      return renderOutlineMarkdown(content, fallbackTitle);
    case "flashcard_set":
      return renderFlashcardsMarkdown(content, fallbackTitle);
    case "mc_question_set":
      return renderMcQuestionsMarkdown(content, fallbackTitle);
    default:
      return null;
  }
}

function pickStr(obj: unknown, ...keys: string[]): string {
  if (!obj || typeof obj !== "object") return "";
  const o = obj as Record<string, unknown>;
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return "";
}

function pickList(obj: unknown, ...keys: string[]): string[] {
  if (!obj || typeof obj !== "object") return [];
  const o = obj as Record<string, unknown>;
  for (const k of keys) {
    const v = o[k];
    if (Array.isArray(v)) {
      const out = v
        .map((it) => (typeof it === "string" ? it.trim() : ""))
        .filter((s) => s.length > 0);
      if (out.length > 0) return out;
    }
  }
  return [];
}

function pickArrayOfObjects(obj: unknown, ...keys: string[]): Array<Record<string, unknown>> {
  if (!obj || typeof obj !== "object") return [];
  const o = obj as Record<string, unknown>;
  for (const k of keys) {
    const v = o[k];
    if (Array.isArray(v)) {
      return v.filter((it): it is Record<string, unknown> => !!it && typeof it === "object" && !Array.isArray(it));
    }
  }
  return [];
}

// ---------------------------------------------------------------------------
// Case-brief renderer
// ---------------------------------------------------------------------------

type Claim = { text?: unknown; source_block_ids?: unknown };

function claimText(v: unknown): string {
  if (typeof v === "string") return v.trim();
  if (v && typeof v === "object") {
    const c = v as Claim;
    if (typeof c.text === "string") return c.text.trim();
  }
  return "";
}

function claimList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map(claimText).filter((s) => s.length > 0);
}

function renderCaseBriefMarkdown(
  content: Record<string, unknown>,
  fallbackName: string,
): string | null {
  if (!content || typeof content !== "object") return null;
  // Quick sniff — if none of the FIRAC+ fields are present, this isn't a
  // case_brief shape and the generic fallback should handle it.
  const looksLikeBrief =
    "facts" in content ||
    "issue" in content ||
    "holding" in content ||
    "rule" in content;
  if (!looksLikeBrief) return null;

  const out: string[] = [];
  const caseName =
    (typeof content.case_name === "string" && content.case_name.trim()) ||
    fallbackName;
  out.push(`# ${caseName}`);

  const meta: string[] = [];
  if (typeof content.citation === "string" && content.citation.trim())
    meta.push(content.citation.trim());
  if (typeof content.court === "string" && content.court.trim())
    meta.push(content.court.trim());
  if (typeof content.year === "number") meta.push(String(content.year));
  if (meta.length > 0) {
    out.push("", `*${meta.join(" · ")}*`);
  }

  const factsBullets = claimList(content.facts);
  if (factsBullets.length > 0) {
    out.push("", "## Facts");
    for (const f of factsBullets) out.push(`- ${f}`);
  }

  const procedural = claimText(content.procedural_posture);
  if (procedural) {
    out.push("", "## Procedural posture", "", procedural);
  }

  const issue = claimText(content.issue);
  if (issue) {
    out.push("", "## Issue", "", issue);
  }

  const holding = claimText(content.holding);
  if (holding) {
    out.push("", "## Holding", "", holding);
  }

  const rule = claimText(content.rule);
  if (rule) {
    out.push("", "## Rule", "", rule);
  }

  const reasoningBullets = claimList(content.reasoning);
  if (reasoningBullets.length > 0) {
    out.push("", "## Reasoning");
    for (const r of reasoningBullets) out.push(`- ${r}`);
  }

  const significance = claimText(content.significance);
  if (significance) {
    out.push("", "## Significance", "", significance);
  }

  if (
    typeof content.where_this_fits === "string" &&
    content.where_this_fits.trim()
  ) {
    out.push("", "## Where this fits", "", content.where_this_fits.trim());
  }

  if (
    typeof content.likely_emphasis === "string" &&
    content.likely_emphasis.trim()
  ) {
    out.push("", "## Likely emphasis", "", content.likely_emphasis.trim());
  }

  const limitations = Array.isArray(content.limitations)
    ? (content.limitations as unknown[]).filter(
        (s): s is string => typeof s === "string" && s.trim().length > 0,
      )
    : [];
  if (limitations.length > 0) {
    out.push("", "## Limitations");
    for (const l of limitations) out.push(`- ${l}`);
  }

  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Attack-sheet renderer (spec §5.9, schemas/attack_sheet.json). Tolerates
// the model's alias field names — `fact_pattern`/`doctrine` ↔ `trigger`/`points_to`,
// `case`/`holding` ↔ `case_name`/`one_line_holding`.
// ---------------------------------------------------------------------------

function renderAttackSheetMarkdown(
  content: Record<string, unknown>,
  fallbackTitle: string,
): string | null {
  if (!content || typeof content !== "object") return null;
  const looks =
    "issue_spotting_triggers" in content ||
    "rules_with_elements" in content ||
    "common_traps" in content ||
    "decision_tree" in content;
  if (!looks) return null;

  const out: string[] = [];
  const topic =
    (typeof content.topic === "string" && content.topic.trim()) || fallbackTitle;
  out.push(`# ${topic}`);

  const triggers = pickArrayOfObjects(content, "issue_spotting_triggers");
  if (triggers.length > 0) {
    out.push("", "## Issue spotting");
    for (const t of triggers) {
      const trig = pickStr(t, "trigger", "fact_pattern");
      const points = pickStr(t, "points_to", "doctrine");
      if (trig || points) {
        out.push(`- **${trig || "Trigger"}** — ${points || ""}`.trim());
      }
    }
  }

  const dt = content.decision_tree;
  if (dt && typeof dt === "object") {
    const root = pickStr(dt, "root", "question", "prompt");
    const branches = pickArrayOfObjects(dt, "branches");
    if (root || branches.length > 0) {
      out.push("", "## Decision tree");
      if (root) out.push("", `**Q:** ${root}`);
      const renderBranch = (b: Record<string, unknown>, depth: number) => {
        const cond = pickStr(b, "condition", "if", "case");
        const then = pickStr(b, "then", "do", "result");
        const indent = "  ".repeat(depth);
        if (cond || then) {
          out.push(`${indent}- **If** ${cond || "(unspecified)"} **→** ${then || ""}`.trim());
        }
        const subs = pickArrayOfObjects(b, "subbranches", "children");
        for (const sub of subs) renderBranch(sub, depth + 1);
      };
      for (const b of branches) renderBranch(b, 0);
    }
  }

  const cases = pickArrayOfObjects(content, "controlling_cases");
  if (cases.length > 0) {
    out.push("", "## Controlling cases");
    for (const c of cases) {
      const name = pickStr(c, "case_name", "case", "name");
      const holding = pickStr(c, "one_line_holding", "holding");
      if (name || holding) {
        out.push(`- ***${name || "Case"}*** — ${holding || ""}`.trim());
      }
    }
  }

  const rules = pickArrayOfObjects(content, "rules_with_elements");
  if (rules.length > 0) {
    out.push("", "## Rules with elements");
    for (const r of rules) {
      const ruleText = pickStr(r, "rule", "name");
      const elements = pickList(r, "elements");
      if (ruleText) out.push("", `**${ruleText}**`);
      for (const e of elements) out.push(`- ${e}`);
    }
  }

  const exceptions = pickList(content, "exceptions");
  if (exceptions.length > 0) {
    out.push("", "## Exceptions");
    for (const e of exceptions) out.push(`- ${e}`);
  }

  const splits = pickArrayOfObjects(content, "majority_minority_splits");
  if (splits.length > 0) {
    out.push("", "## Majority / minority splits");
    for (const s of splits) {
      const issue = pickStr(s, "issue");
      const maj = pickStr(s, "majority");
      const min = pickStr(s, "minority");
      if (issue) out.push("", `**${issue}**`);
      if (maj) out.push(`- *Majority:* ${maj}`);
      if (min) out.push(`- *Minority:* ${min}`);
    }
  }

  const traps = pickList(content, "common_traps");
  if (traps.length > 0) {
    out.push("", "## Common traps");
    for (const t of traps) out.push(`- ${t}`);
  }

  const summaries = pickList(content, "one_line_summaries");
  if (summaries.length > 0) {
    out.push("", "## One-line takeaways");
    for (const s of summaries) out.push(`- ${s}`);
  }

  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Synthesis renderer (spec §5.8, schemas/synthesis.json).
// ---------------------------------------------------------------------------

function renderSynthesisMarkdown(
  content: Record<string, unknown>,
  fallbackTitle: string,
): string | null {
  if (!content || typeof content !== "object") return null;
  const looks =
    "modern_synthesis" in content ||
    "categorical_rules" in content ||
    "balancing_tests" in content ||
    "timeline" in content;
  if (!looks) return null;

  const out: string[] = [];
  const area =
    (typeof content.doctrinal_area === "string" && content.doctrinal_area.trim()) ||
    fallbackTitle;
  out.push(`# ${area}`);

  if (typeof content.modern_synthesis === "string" && content.modern_synthesis.trim()) {
    out.push("", "## Where the doctrine sits today", "", content.modern_synthesis.trim());
  }

  const cases = pickArrayOfObjects(content, "cases");
  if (cases.length > 0) {
    out.push("", "## Cases covered");
    for (const c of cases) {
      const name = pickStr(c, "case_name", "name");
      const year = c.year;
      const court = pickStr(c, "court");
      const tail: string[] = [];
      if (typeof year === "number") tail.push(String(year));
      if (court) tail.push(court);
      if (name) {
        out.push(
          tail.length > 0 ? `- ***${name}*** — ${tail.join(", ")}` : `- ***${name}***`,
        );
      }
    }
  }

  const timeline = pickArrayOfObjects(content, "timeline");
  if (timeline.length > 0) {
    out.push("", "## Timeline");
    for (const t of timeline) {
      const year = t.year;
      const event = pickStr(t, "event");
      const cn = pickStr(t, "case_name");
      if (event) {
        const yr = typeof year === "number" ? `${year} — ` : "";
        const tail = cn ? ` *(${cn})*` : "";
        out.push(`- ${yr}${event}${tail}`);
      }
    }
  }

  const rules = pickArrayOfObjects(content, "categorical_rules");
  if (rules.length > 0) {
    out.push("", "## Categorical rules");
    for (const r of rules) {
      const rule = pickStr(r, "rule");
      const from = pickStr(r, "from_case");
      if (rule) out.push(`- ${rule}${from ? ` *(from ${from})*` : ""}`);
    }
  }

  const balancing = pickArrayOfObjects(content, "balancing_tests");
  if (balancing.length > 0) {
    out.push("", "## Balancing tests");
    for (const b of balancing) {
      const test = pickStr(b, "test");
      const factors = pickList(b, "factors");
      const from = pickStr(b, "from_case");
      if (test) {
        out.push("", `**${test}**${from ? ` *(${from})*` : ""}`);
        for (const f of factors) out.push(`- ${f}`);
      }
    }
  }

  const rels = pickArrayOfObjects(content, "relationships");
  if (rels.length > 0) {
    out.push("", "## How the cases relate");
    for (const r of rels) {
      const desc = pickStr(r, "description");
      if (desc) out.push(`- ${desc}`);
    }
  }

  if (typeof content.exam_framework === "string" && content.exam_framework.trim()) {
    out.push("", "## Exam framework", "", content.exam_framework.trim());
  }

  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Outline renderer (spec §5.11, schemas/outline.json). The schema uses a
// recursive `TopicNode`; we render as nested markdown headings.
// ---------------------------------------------------------------------------

function renderOutlineMarkdown(
  content: Record<string, unknown>,
  fallbackTitle: string,
): string | null {
  if (!content || typeof content !== "object") return null;
  if (!Array.isArray(content.topics)) return null;

  const out: string[] = [];
  const course =
    (typeof content.course === "string" && content.course.trim()) || fallbackTitle;
  out.push(`# ${course}`);

  const renderTopic = (node: unknown, depth: number) => {
    if (!node || typeof node !== "object") return;
    const n = node as Record<string, unknown>;
    const title = pickStr(n, "title");
    if (!title) return;
    const heading = "#".repeat(Math.min(6, Math.max(2, depth + 1)));
    out.push("", `${heading} ${title}`);

    const rules = pickList(n, "rule_statements");
    if (rules.length > 0) {
      out.push("", "**Rules:**");
      for (const r of rules) out.push(`- ${r}`);
    }

    const cases = pickArrayOfObjects(n, "controlling_cases");
    if (cases.length > 0) {
      out.push("", "**Controlling cases:**");
      for (const c of cases) {
        const name = pickStr(c, "case_name", "name");
        const cite = pickStr(c, "cite", "citation");
        const oneLine = pickStr(c, "one_line", "holding");
        const head = cite ? `***${name}*** (${cite})` : `***${name}***`;
        out.push(oneLine ? `- ${head} — ${oneLine}` : `- ${head}`);
      }
    }

    const policy = pickList(n, "policy_rationales");
    if (policy.length > 0) {
      out.push("", "**Policy:**");
      for (const p of policy) out.push(`- ${p}`);
    }

    const traps = pickList(n, "exam_traps");
    if (traps.length > 0) {
      out.push("", "**Exam traps:**");
      for (const t of traps) out.push(`- ${t}`);
    }

    const xrefs = pickList(n, "cross_references");
    if (xrefs.length > 0) {
      out.push("", "**See also:**");
      for (const x of xrefs) out.push(`- ${x}`);
    }

    if (Array.isArray(n.children)) {
      for (const child of n.children) renderTopic(child, depth + 1);
    }
  };

  for (const t of content.topics) renderTopic(t, 1);
  return out.join("\n");
}

function extractFallbackMarkdown(content: Record<string, unknown>): string | null {
  // Some artifacts don't have a top-level "markdown" — they have structured
  // content. Render a sensible default for the common ones.
  if (!content) return null;
  if (typeof content["markdown"] === "string") return content["markdown"] as string;
  // Synthesis: { doctrinal_area, body, citations }
  if (typeof content["body"] === "string") return content["body"] as string;
  // Outline: { sections: [{ heading, body, children: [...] }] }
  if (Array.isArray(content["sections"])) {
    const lines: string[] = [];
    const walk = (sections: unknown[], depth: number) => {
      for (const s of sections) {
        if (s && typeof s === "object") {
          const obj = s as Record<string, unknown>;
          const heading =
            typeof obj["heading"] === "string"
              ? (obj["heading"] as string)
              : "(untitled)";
          const body =
            typeof obj["body"] === "string" ? (obj["body"] as string) : "";
          lines.push(`${"#".repeat(Math.min(depth, 6))} ${heading}`);
          if (body) lines.push("", body, "");
          if (Array.isArray(obj["children"])) walk(obj["children"] as unknown[], depth + 1);
        }
      }
    };
    walk(content["sections"] as unknown[], 1);
    return lines.join("\n");
  }
  // Attack sheet: { topic, sections: { rule, elements, traps } }
  if (
    typeof content["topic"] === "string" &&
    typeof content["sections"] === "object"
  ) {
    const sections = content["sections"] as Record<string, unknown>;
    const lines = [`# ${content["topic"]}`];
    for (const [k, v] of Object.entries(sections)) {
      lines.push(`## ${k.replace(/_/g, " ")}`);
      if (typeof v === "string") lines.push(v);
      else if (Array.isArray(v)) {
        for (const item of v) lines.push(`- ${typeof item === "string" ? item : JSON.stringify(item)}`);
      } else lines.push(JSON.stringify(v, null, 2));
    }
    return lines.join("\n\n");
  }
  return null;
}

function SidePanel({ a }: { a: ArtifactDetail }) {
  return (
    <>
      <div className="border border-border bg-card px-3 py-3 text-xs">
        <p className="font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Provenance
        </p>
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 tabular-nums">
          <dt className="text-muted-foreground">Sources</dt>
          <dd>{a.sources.length}</dd>
          <dt className="text-muted-foreground">Cost</dt>
          <dd>{formatUsd(a.cost_usd)}</dd>
          <dt className="text-muted-foreground">Cache key</dt>
          <dd className="break-all font-mono text-[10px]">{a.cache_key.slice(0, 24)}…</dd>
        </dl>
        <Link href={`/api/artifacts/${a.id}/lineage`} target="_blank" rel="noreferrer">
          <Button variant="link" size="sm" className="mt-2 px-0">
            View full lineage →
          </Button>
        </Link>
      </div>

      {a.parent_artifact_id && (
        <div className="border border-border bg-card px-3 py-3 text-xs">
          <p className="font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            Derived from
          </p>
          <Link
            href={`/artifacts/${a.parent_artifact_id}`}
            className="law-link mt-1 block underline"
          >
            <code className="font-mono">{a.parent_artifact_id.slice(0, 12)}…</code>
          </Link>
        </div>
      )}

      <div className="border border-border bg-card px-3 py-3 text-xs">
        <p className="font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Sources cited
        </p>
        {a.sources.length === 0 ? (
          <p className="mt-2 text-muted-foreground">None</p>
        ) : (
          <ul className="mt-2 space-y-1">
            {a.sources.slice(0, 12).map((s, idx) => (
              <li key={`${s.kind}-${s.id}-${idx}`} className="font-mono">
                {s.kind} · {s.id.slice(0, 10)}…
              </li>
            ))}
            {a.sources.length > 12 && (
              <li className="text-muted-foreground">
                … and {a.sources.length - 12} more
              </li>
            )}
          </ul>
        )}
      </div>

      <div className="border border-border bg-card px-3 py-3 text-xs">
        <p className="font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Raw content
        </p>
        <details className="mt-1">
          <summary className="cursor-pointer text-muted-foreground">
            Show JSON
          </summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-[10px] leading-snug">
            {JSON.stringify(a.content, null, 2)}
          </pre>
        </details>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Flashcards renderer (spec §5.3, schemas/flashcards.json).
//
// Each card becomes a Q→A block: bold question, indented answer, a small
// uppercase "kind" chip (rule / case_to_doctrine / etc.), and the source
// blocks as a footnote. We deliberately don't show the giant
// `source_block_ids` arrays — they're long opaque hex strings that aren't
// useful to a reading student.
// ---------------------------------------------------------------------------

function renderFlashcardsMarkdown(
  content: Record<string, unknown>,
  fallbackTitle: string,
): string | null {
  if (!content || typeof content !== "object") return null;
  if (!Array.isArray(content.cards)) return null;

  const out: string[] = [];
  const topic =
    (typeof content.topic === "string" && content.topic.trim()) ||
    fallbackTitle;
  out.push(`# ${topic}`);
  out.push("");
  out.push(`*${(content.cards as unknown[]).length} cards*`);
  out.push("");

  for (let i = 0; i < (content.cards as unknown[]).length; i++) {
    const raw = (content.cards as unknown[])[i];
    if (!raw || typeof raw !== "object") continue;
    const card = raw as Record<string, unknown>;
    const front = pickStr(card, "front", "question", "prompt");
    const back = pickStr(card, "back", "answer", "response");
    const kind =
      typeof card.kind === "string" && card.kind.trim()
        ? card.kind.replace(/_/g, " ")
        : null;

    out.push(`## ${i + 1}. ${front || "(no question)"}`);
    if (kind) {
      out.push(`*${kind}*`);
    }
    out.push("");
    out.push(`> ${back || "(no answer)"}`);
    out.push("");
  }

  return out.join("\n");
}

// ---------------------------------------------------------------------------
// MC questions renderer (spec §5.12, schemas/mc_questions.json).
//
// Each question shows the stem, the four lettered options, and a folded
// "Show answer & explanation" section that lists the correct letter, the
// rationale, and the per-distractor explanations. Folding keeps the page
// usable for self-quizzing — the student can read all 10 questions before
// peeking.
// ---------------------------------------------------------------------------

function renderMcQuestionsMarkdown(
  content: Record<string, unknown>,
  fallbackTitle: string,
): string | null {
  if (!content || typeof content !== "object") return null;
  if (!Array.isArray(content.questions)) return null;

  const out: string[] = [];
  const topic =
    (typeof content.topic === "string" && content.topic.trim()) ||
    fallbackTitle;
  out.push(`# ${topic}`);
  out.push("");
  out.push(`*${(content.questions as unknown[]).length} questions*`);
  out.push("");

  for (let i = 0; i < (content.questions as unknown[]).length; i++) {
    const raw = (content.questions as unknown[])[i];
    if (!raw || typeof raw !== "object") continue;
    const q = raw as Record<string, unknown>;
    const stem = pickStr(q, "stem", "question", "prompt");
    out.push(`## ${i + 1}. ${stem || "(no stem)"}`);
    out.push("");

    const options = Array.isArray(q.options) ? q.options : [];
    for (const opt of options) {
      if (!opt || typeof opt !== "object") continue;
      const o = opt as Record<string, unknown>;
      const letter = pickStr(o, "letter");
      const text = pickStr(o, "text");
      out.push(`- **${letter || "?"}.** ${text || ""}`);
    }
    out.push("");

    const correct = pickStr(q, "correct_answer", "answer");
    const explanation = pickStr(q, "explanation", "rationale");
    const doctrine = pickStr(q, "doctrine_tested", "doctrine");
    const distractors = q.distractor_explanations;

    out.push("<details>");
    out.push("<summary>Show answer &amp; explanation</summary>");
    out.push("");
    if (correct) out.push(`**Correct:** ${correct}`);
    if (doctrine) out.push(`**Doctrine tested:** ${doctrine}`);
    if (explanation) {
      out.push("");
      out.push(explanation);
    }
    if (distractors && typeof distractors === "object") {
      out.push("");
      out.push("**Why the others are wrong:**");
      const entries = Array.isArray(distractors)
        ? distractors.map((d, idx) => [String.fromCharCode(65 + idx), d] as const)
        : Object.entries(distractors as Record<string, unknown>);
      for (const [letter, explanation] of entries) {
        const text =
          typeof explanation === "string"
            ? explanation
            : explanation && typeof explanation === "object"
              ? pickStr(explanation as Record<string, unknown>, "text", "explanation")
              : "";
        if (text) out.push(`- **${letter}.** ${text}`);
      }
    }
    out.push("");
    out.push("</details>");
    out.push("");
  }

  return out.join("\n");
}

// ---------------------------------------------------------------------------
// What-if panel — only shown on case_brief artifacts
// ---------------------------------------------------------------------------

function WhatIfPanel({ artifact }: { artifact: ArtifactDetail }) {
  const [open, setOpen] = React.useState(false);
  const [count, setCount] = React.useState(5);
  const [busy, setBusy] = React.useState(false);
  const [variations, setVariations] = React.useState<WhatIfVariation[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ artifact: { content: { variations: WhatIfVariation[] } } }>(
        "/features/what-if",
        {
          corpus_id: artifact.corpus_id,
          case_brief_artifact_id: artifact.id,
          num_variations: count,
        },
      );
      setVariations(res.artifact.content.variations ?? []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "What-if failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="mt-10 border-t border-border pt-6">
      <div className="flex items-center justify-between">
        <h2 className="font-serif text-xl font-semibold">What if…?</h2>
        <Button
          variant={open ? "ghost" : "outline"}
          size="sm"
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Hide" : "Open"}
        </Button>
      </div>
      {open && (
        <div className="mt-4">
          <div className="flex items-center gap-3 text-sm">
            <label className="font-medium">Variations</label>
            <PageNumberInput
              value={count}
              onCommit={setCount}
              min={3}
              max={10}
              fallback={5}
              className="h-8 w-16"
            />
            <Button size="sm" onClick={() => void run()} disabled={busy}>
              {busy ? "Generating…" : "Generate"}
            </Button>
          </div>
          {error && <p className="mt-2 text-xs text-destructive">{error}</p>}
          {variations !== null && variations.length === 0 && (
            <p className="mt-3 text-sm text-muted-foreground">No variations returned.</p>
          )}
          {variations !== null && variations.length > 0 && (
            <ul className="mt-4 flex flex-col gap-3">
              {variations.map((v, idx) => (
                <li key={v.id || idx} className="border border-border bg-card px-4 py-3">
                  <p className="font-serif font-semibold">
                    {v.id ? `${v.id.toUpperCase()} · ` : ""}
                    {v.tests_understanding_of || "Variation"}
                  </p>
                  <p className="mt-2 text-sm">
                    <strong>Fact changed:</strong> {v.fact_changed}
                  </p>
                  <p className="mt-1 text-sm">
                    <strong>Consequence:</strong> {v.consequence}
                  </p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    <em>Why doctrinally:</em> {v.doctrinal_reason}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
