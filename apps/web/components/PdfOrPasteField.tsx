"use client";

import * as React from "react";

import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { uploadEndpoint } from "@/lib/uploadEndpoint";
import { cn } from "@/lib/utils";

/**
 * "Paste markdown OR drop a PDF" field used by the IRAC practice wizard for
 * both the exam and grader-memo inputs. The PDF path streams the file to the
 * backend's /uploads/pdf-extract endpoint, which returns extracted text. The
 * text fills the textarea and the user can edit it before submitting (text
 * extraction from a multi-column legal PDF is rarely perfect).
 *
 * Bypasses the Next 15 dev-server rewrite for the upload (same reason book
 * uploads do — Next caps proxied bodies at 10 MiB).
 */

type ExtractResponse = {
  filename: string;
  size_bytes: number;
  sha256: string;
  stored_path: string;
  page_count: number;
  text: string;
};

export function PdfOrPasteField({
  label,
  value,
  onChange,
  placeholder,
  rows = 8,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  rows?: number;
}) {
  const fileRef = React.useRef<HTMLInputElement>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [meta, setMeta] = React.useState<{
    filename: string;
    pages: number;
    chars: number;
  } | null>(null);
  const [over, setOver] = React.useState(false);

  const upload = async (file: File) => {
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setError("Drop a PDF or use the textarea to paste.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const res = await fetch(uploadEndpoint("/uploads/pdf-extract"), {
        method: "POST",
        body: fd,
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = (await res.json()) as { detail?: unknown };
          if (typeof body.detail === "string") detail = body.detail;
        } catch {
          /* ignore */
        }
        throw new Error(detail);
      }
      const parsed = (await res.json()) as ExtractResponse;
      onChange(parsed.text);
      setMeta({
        filename: parsed.filename,
        pages: parsed.page_count,
        chars: parsed.text.length,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const onPick = () => fileRef.current?.click();

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <Label>{label}</Label>
        <div className="flex items-center gap-2">
          {meta && (
            <span className="text-[11px] tabular-nums text-muted-foreground">
              {meta.filename} · {meta.pages}p · {meta.chars.toLocaleString()} chars
            </span>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onPick}
            disabled={busy}
          >
            {busy ? "Extracting…" : meta ? "Replace PDF" : "Upload PDF"}
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void upload(f);
              e.target.value = "";
            }}
          />
        </div>
      </div>

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const f = e.dataTransfer.files?.[0];
          if (f) void upload(f);
        }}
        className={cn(
          "rounded-sm border bg-card transition-colors",
          over ? "border-accent bg-accent/5" : "border-input",
        )}
      >
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          style={{ minHeight: rows * 22 }}
          className="w-full resize-y rounded-sm bg-transparent px-3 py-2 font-serif text-sm leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>

      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{busy ? <Spinner size="sm" /> : "Drop a PDF onto the box or paste text"}</span>
        {error && <span className="text-destructive">{error}</span>}
      </div>
    </div>
  );
}
