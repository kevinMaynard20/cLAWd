"use client";

import * as React from "react";

/**
 * Lightweight markdown-ish renderer for artifact content. We avoid pulling in
 * a full markdown parser dependency for now — most artifacts are case briefs,
 * syntheses, and outlines that follow a tight structure (## headings + ###
 * sub-headings + paragraphs + bullet lists). This renderer handles those
 * shapes plus inline `code` and **bold** / *italic*. Anything richer falls
 * through as preformatted text.
 *
 * If we ever need full GFM (tables, footnotes, etc.) swap this out for
 * `react-markdown` + `remark-gfm` — same interface.
 */

type Block =
  | { kind: "h1"; text: string }
  | { kind: "h2"; text: string }
  | { kind: "h3"; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "p"; text: string }
  | { kind: "blockquote"; text: string };

function parseBlocks(md: string): Block[] {
  const lines = md.split(/\r?\n/);
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const raw = lines[i];
    const trimmed = raw.trim();
    if (trimmed === "") {
      i += 1;
      continue;
    }
    if (trimmed.startsWith("### ")) {
      blocks.push({ kind: "h3", text: trimmed.slice(4) });
      i += 1;
      continue;
    }
    if (trimmed.startsWith("## ")) {
      blocks.push({ kind: "h2", text: trimmed.slice(3) });
      i += 1;
      continue;
    }
    if (trimmed.startsWith("# ")) {
      blocks.push({ kind: "h1", text: trimmed.slice(2) });
      i += 1;
      continue;
    }
    if (/^[-*]\s/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, ""));
        i += 1;
      }
      blocks.push({ kind: "ul", items });
      continue;
    }
    if (/^\d+\.\s/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s+/, ""));
        i += 1;
      }
      blocks.push({ kind: "ol", items });
      continue;
    }
    if (trimmed.startsWith("> ")) {
      const buf: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("> ")) {
        buf.push(lines[i].trim().slice(2));
        i += 1;
      }
      blocks.push({ kind: "blockquote", text: buf.join(" ") });
      continue;
    }
    // Paragraph: gather until blank line.
    const buf: string[] = [trimmed];
    i += 1;
    while (i < lines.length && lines[i].trim() !== "") {
      buf.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ kind: "p", text: buf.join(" ") });
  }
  return blocks;
}

function renderInline(text: string, baseKey: string): React.ReactNode[] {
  // Order matters: code spans first to avoid * / _ inside them being parsed.
  const tokens: React.ReactNode[] = [];
  const codeSplit = text.split(/(`[^`]+`)/g);
  let counter = 0;
  for (const chunk of codeSplit) {
    if (chunk.startsWith("`") && chunk.endsWith("`") && chunk.length > 2) {
      tokens.push(
        <code key={`${baseKey}-c-${counter++}`} className="rounded-sm bg-muted px-1 py-0.5 font-mono text-[0.85em]">
          {chunk.slice(1, -1)}
        </code>,
      );
    } else {
      // bold / italic — naive but adequate.
      const parts = chunk.split(/(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)/g);
      for (const p of parts) {
        if (p.startsWith("**") && p.endsWith("**")) {
          tokens.push(
            <strong key={`${baseKey}-b-${counter++}`} className="font-semibold">
              {p.slice(2, -2)}
            </strong>,
          );
        } else if (
          (p.startsWith("*") && p.endsWith("*") && p.length > 2) ||
          (p.startsWith("_") && p.endsWith("_") && p.length > 2)
        ) {
          tokens.push(
            <em key={`${baseKey}-i-${counter++}`} className="italic">
              {p.slice(1, -1)}
            </em>,
          );
        } else if (p) {
          tokens.push(p);
        }
      }
    }
  }
  return tokens;
}

export function ArtifactMarkdown({ markdown }: { markdown: string }) {
  const blocks = React.useMemo(() => parseBlocks(markdown), [markdown]);
  return (
    <article className="prose-law max-w-none">
      {blocks.map((b, idx) => {
        const k = `b-${idx}`;
        if (b.kind === "h1")
          return (
            <h1 key={k} className="mt-6 mb-3 font-serif text-2xl font-semibold tracking-tight">
              {renderInline(b.text, k)}
            </h1>
          );
        if (b.kind === "h2")
          return (
            <h2 key={k} className="mt-5 mb-2 font-serif text-xl font-semibold tracking-tight">
              {renderInline(b.text, k)}
            </h2>
          );
        if (b.kind === "h3")
          return (
            <h3 key={k} className="mt-4 mb-2 font-serif text-base font-semibold tracking-tight">
              {renderInline(b.text, k)}
            </h3>
          );
        if (b.kind === "ul")
          return (
            <ul key={k} className="my-2 ml-5 list-disc space-y-1 font-serif">
              {b.items.map((it, j) => (
                <li key={`${k}-${j}`}>{renderInline(it, `${k}-${j}`)}</li>
              ))}
            </ul>
          );
        if (b.kind === "ol")
          return (
            <ol key={k} className="my-2 ml-5 list-decimal space-y-1 font-serif">
              {b.items.map((it, j) => (
                <li key={`${k}-${j}`}>{renderInline(it, `${k}-${j}`)}</li>
              ))}
            </ol>
          );
        if (b.kind === "blockquote")
          return (
            <blockquote
              key={k}
              className="my-3 border-l-2 border-accent pl-3 font-serif italic text-foreground/80"
            >
              {renderInline(b.text, k)}
            </blockquote>
          );
        return (
          <p key={k} className="my-3 font-serif text-base leading-relaxed">
            {renderInline(b.text, k)}
          </p>
        );
      })}
    </article>
  );
}
