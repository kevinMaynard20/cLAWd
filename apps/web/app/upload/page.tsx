"use client";

import Link from "next/link";
import * as React from "react";

import { LoadingButton } from "@/components/LoadingButton";
import { Spinner } from "@/components/Spinner";
import { TaskProgress } from "@/components/TaskProgress";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, ApiError } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { uploadEndpoint } from "@/lib/uploadEndpoint";
import { cn } from "@/lib/utils";

/**
 * Drag-drop multi-file upload (spec §7.7 — large casebook ingestion + the
 * tail-end transcript / syllabus uploads).
 *
 * Two tabs: PDF (default, drives `/api/ingest/book/async`) and Text
 * (transcripts, syllabi). PDFs are streamed via XMLHttpRequest so we can
 * read the upload-progress events the Fetch API still doesn't expose.
 *
 * The page is the only client of <TaskProgress> in the tree today, but the
 * widget is generic so future async pipelines (re-embed, export, etc.) can
 * reuse it as-is.
 */

type CorpusSummary = {
  id: string;
  name: string;
  course: string;
};

type UploadedFileDTO = {
  filename: string;
  size_bytes: number;
  sha256: string;
  stored_path: string;
  uploaded_at: string;
};

type UploadResponse = {
  files: UploadedFileDTO[];
};

type IngestBookAsyncResponse = {
  task_id: string;
  poll_url: string;
};

type FileSlot = {
  id: string;
  file: File;
  /** 0..1 — driven by XHR upload progress events. */
  progress: number;
  status: "pending" | "uploading" | "uploaded" | "error";
  storedPath: string | null;
  error: string | null;
};

function genId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `f-${Math.random().toString(36).slice(2)}`;
}

function uploadOne(
  file: File,
  endpoint: "/uploads/pdf" | "/uploads/text",
  onProgress: (pct: number) => void,
): Promise<UploadedFileDTO> {
  // Direct POST to the FastAPI backend, bypassing the Next dev-server's
  // rewrite proxy. Next 15 caps proxied request bodies at 10 MiB, which
  // truncates real casebook PDFs (typical 50–500 MB) and produces socket
  // hang-ups instead of useful errors. Backend CORS allows this origin.
  const url = uploadEndpoint(endpoint);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(1, event.loaded / event.total));
      }
    };
    xhr.upload.onload = () => onProgress(1);
    xhr.onerror = () => reject(new Error("Network error during upload."));
    xhr.onabort = () => reject(new Error("Upload aborted."));
    xhr.onload = () => {
      const status = xhr.status;
      const body = xhr.response as unknown;
      if (status >= 200 && status < 300) {
        const parsed = body as UploadResponse | null;
        const first = parsed?.files?.[0];
        if (!first) {
          reject(new Error("Upload returned no file metadata."));
          return;
        }
        resolve(first);
        return;
      }
      let detail = `HTTP ${status}`;
      if (body && typeof body === "object" && "detail" in body) {
        const d = (body as { detail: unknown }).detail;
        if (typeof d === "string") detail = d;
      }
      reject(new Error(detail));
    };
    const fd = new FormData();
    fd.append("files", file, file.name);
    xhr.send(fd);
  });
}

export default function UploadPage() {
  return (
    <main className="mx-auto w-full max-w-4xl px-6 py-10">
      <header>
        <p className="font-serif text-xs uppercase tracking-[0.18em] text-muted-foreground">
          Workspace · Upload
        </p>
        <h1 className="mt-2 font-serif text-2xl font-semibold tracking-tight">
          Add casebooks, transcripts, syllabi
        </h1>
        <p className="mt-2 max-w-prose text-sm leading-relaxed text-muted-foreground">
          PDFs run through the full ingestion pipeline (page extraction, block
          segmentation, optional LLM polish). Text uploads can become
          transcripts or syllabi.
        </p>
      </header>

      <div className="mt-8">
        <Tabs defaultValue="pdf">
          <TabsList>
            <TabsTrigger value="pdf">Casebook PDF</TabsTrigger>
            <TabsTrigger value="text">Text upload</TabsTrigger>
          </TabsList>
          <TabsContent value="pdf" className="pt-6">
            <PdfTab />
          </TabsContent>
          <TabsContent value="text" className="pt-6">
            <TextTab />
          </TabsContent>
        </Tabs>
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// PDF tab
// ---------------------------------------------------------------------------

function PdfTab() {
  const [slots, setSlots] = React.useState<FileSlot[]>([]);
  const [title, setTitle] = React.useState("");
  const [authors, setAuthors] = React.useState("");
  const [edition, setEdition] = React.useState("");

  const [corpora, setCorpora] = React.useState<CorpusSummary[] | null>(null);
  const [corpusId, setCorpusId] = React.useState<string>("");
  const [corporaError, setCorporaError] = React.useState<string | null>(null);

  const [creatingCorpus, setCreatingCorpus] = React.useState(false);
  const [newCorpusName, setNewCorpusName] = React.useState("");
  const [newCorpusCourse, setNewCorpusCourse] = React.useState("");
  const [newCorpusBusy, setNewCorpusBusy] = React.useState(false);
  const [newCorpusError, setNewCorpusError] = React.useState<string | null>(null);

  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [taskId, setTaskId] = React.useState<string | null>(null);
  const [completed, setCompleted] = React.useState(false);

  const inputRef = React.useRef<HTMLInputElement>(null);

  const refreshCorpora = React.useCallback(async () => {
    try {
      const data = await api.get<CorpusSummary[]>("/corpora");
      setCorpora(data);
      setCorporaError(null);
      return data;
    } catch (err) {
      setCorporaError(
        err instanceof ApiError
          ? err.message
          : "Could not load corpora.",
      );
      setCorpora([]);
      return [] as CorpusSummary[];
    }
  }, []);

  React.useEffect(() => {
    void refreshCorpora();
  }, [refreshCorpora]);

  const addFiles = React.useCallback(
    (incoming: FileList | File[]) => {
      const arr = Array.from(incoming).filter((f) =>
        f.name.toLowerCase().endsWith(".pdf"),
      );
      if (arr.length === 0) return;
      setSlots((prev) => [
        ...prev,
        ...arr.map<FileSlot>((file) => ({
          id: genId(),
          file,
          progress: 0,
          status: "pending",
          storedPath: null,
          error: null,
        })),
      ]);
      setTitle((prev) => {
        if (prev.trim().length > 0) return prev;
        const stem = arr[0]?.name.replace(/\.pdf$/i, "") ?? "";
        return stem;
      });
    },
    [],
  );

  const removeSlot = (id: string) =>
    setSlots((prev) => prev.filter((s) => s.id !== id));

  const handleStart = async () => {
    if (slots.length === 0) {
      setSubmitError("Add at least one PDF first.");
      return;
    }
    if (!corpusId) {
      setSubmitError("Pick a corpus or create one.");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    setTaskId(null);
    setCompleted(false);

    // Snapshot for the loop — we mutate state from inside.
    const pendingIds = slots
      .filter((s) => s.status !== "uploaded")
      .map((s) => s.id);
    const storedPaths: string[] = slots
      .filter((s) => s.status === "uploaded" && s.storedPath)
      .map((s) => s.storedPath as string);

    try {
      for (const id of pendingIds) {
        const slot = slots.find((s) => s.id === id);
        if (!slot) continue;
        setSlots((prev) =>
          prev.map((s) =>
            s.id === id
              ? { ...s, status: "uploading", progress: 0, error: null }
              : s,
          ),
        );
        try {
          const dto = await uploadOne(slot.file, "/uploads/pdf", (pct) =>
            setSlots((prev) =>
              prev.map((s) => (s.id === id ? { ...s, progress: pct } : s)),
            ),
          );
          setSlots((prev) =>
            prev.map((s) =>
              s.id === id
                ? {
                    ...s,
                    status: "uploaded",
                    progress: 1,
                    storedPath: dto.stored_path,
                  }
                : s,
            ),
          );
          storedPaths.push(dto.stored_path);
        } catch (err) {
          const message = err instanceof Error ? err.message : "Upload failed.";
          setSlots((prev) =>
            prev.map((s) =>
              s.id === id ? { ...s, status: "error", error: message } : s,
            ),
          );
          throw err;
        }
      }

      const authorList = authors
        .split(",")
        .map((a) => a.trim())
        .filter((a) => a.length > 0);

      const res = await api.post<IngestBookAsyncResponse>(
        "/ingest/book/async",
        {
          pdf_paths: storedPaths,
          title: title.trim() || null,
          authors: authorList,
          edition: edition.trim() || null,
          corpus_id: corpusId,
          use_llm: true,
        },
      );
      setTaskId(res.task_id);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Could not start ingestion.";
      setSubmitError(message);
    } finally {
      setSubmitting(false);
    }
  };

  const createCorpus = async () => {
    if (!newCorpusName.trim() || !newCorpusCourse.trim()) {
      setNewCorpusError("Both name and course are required.");
      return;
    }
    setNewCorpusBusy(true);
    setNewCorpusError(null);
    try {
      const created = await api.post<CorpusSummary>("/corpora", {
        name: newCorpusName.trim(),
        course: newCorpusCourse.trim(),
      });
      const next = await refreshCorpora();
      setCorpusId(
        next.find((c) => c.id === created.id)?.id ?? created.id,
      );
      setCreatingCorpus(false);
      setNewCorpusName("");
      setNewCorpusCourse("");
    } catch (err) {
      setNewCorpusError(
        err instanceof ApiError ? err.message : "Could not create corpus.",
      );
    } finally {
      setNewCorpusBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <DropZone
        onFiles={addFiles}
        accept="application/pdf,.pdf"
        onPick={() => inputRef.current?.click()}
      >
        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) addFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </DropZone>

      {slots.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Files queued</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {slots.map((slot) => (
              <FileRow key={slot.id} slot={slot} onRemove={removeSlot} />
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Book metadata</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Title">
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Property: Cases and Materials"
            />
          </Field>
          <Field label="Edition">
            <Input
              value={edition}
              onChange={(e) => setEdition(e.target.value)}
              placeholder="9th"
            />
          </Field>
          <Field label="Authors (comma-separated)" className="md:col-span-2">
            <Input
              value={authors}
              onChange={(e) => setAuthors(e.target.value)}
              placeholder="Dukeminier, Krier, Alexander, Schill"
            />
          </Field>
          <Field label="Corpus" className="md:col-span-2">
            {corpora === null ? (
              <div className="flex h-9 items-center gap-2 text-sm text-muted-foreground">
                <Spinner size="sm" />
                Loading corpora…
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                <Select
                  value={creatingCorpus ? "__new" : corpusId}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (v === "__new") {
                      setCreatingCorpus(true);
                      setCorpusId("");
                    } else {
                      setCreatingCorpus(false);
                      setCorpusId(v);
                    }
                  }}
                >
                  <option value="">Select a corpus…</option>
                  {corpora.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} — {c.course}
                    </option>
                  ))}
                  <option value="__new">+ Create new corpus…</option>
                </Select>
                {corporaError && (
                  <p className="text-xs text-destructive">{corporaError}</p>
                )}
                {creatingCorpus && (
                  <div className="flex flex-col gap-2 border border-border bg-subtle p-3">
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                      <Input
                        placeholder="Corpus name"
                        value={newCorpusName}
                        onChange={(e) => setNewCorpusName(e.target.value)}
                      />
                      <Input
                        placeholder="Course (e.g., Property)"
                        value={newCorpusCourse}
                        onChange={(e) => setNewCorpusCourse(e.target.value)}
                      />
                    </div>
                    {newCorpusError && (
                      <p className="text-xs text-destructive">
                        {newCorpusError}
                      </p>
                    )}
                    <div className="flex items-center gap-2">
                      <LoadingButton
                        size="sm"
                        loading={newCorpusBusy}
                        onClick={createCorpus}
                      >
                        Create corpus
                      </LoadingButton>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setCreatingCorpus(false);
                          setNewCorpusError(null);
                        }}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </Field>
        </CardContent>
      </Card>

      <div className="flex items-center gap-3">
        <LoadingButton loading={submitting} onClick={handleStart}>
          Start ingestion
        </LoadingButton>
        {submitError && (
          <p className="text-xs text-destructive">{submitError}</p>
        )}
      </div>

      {taskId && (
        <TaskProgress
          taskId={taskId}
          onCompleted={() => setCompleted(true)}
          onFailed={() => setCompleted(false)}
        />
      )}

      {completed && (
        <div
          role="status"
          className="border border-success/40 bg-success/10 px-4 py-3 text-sm text-success"
        >
          Ingestion complete.{" "}
          <Link href="/" className="law-link underline">
            Return to dashboard
          </Link>
          .
        </div>
      )}
    </div>
  );
}

function FileRow({
  slot,
  onRemove,
}: {
  slot: FileSlot;
  onRemove: (id: string) => void;
}) {
  return (
    <div className="flex items-center gap-3 border border-border bg-card px-3 py-2 text-sm">
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono text-xs tracking-tight">
          {slot.file.name}
        </p>
        <p className="text-[11px] tabular-nums text-muted-foreground">
          {formatBytes(slot.file.size)}
          {slot.status === "uploaded" && slot.storedPath && (
            <>
              {" · "}
              <span className="text-success">uploaded</span>
            </>
          )}
          {slot.status === "uploading" && (
            <>
              {" · "}
              <span className="text-accent">
                uploading {Math.round(slot.progress * 100)}%
              </span>
            </>
          )}
          {slot.status === "error" && slot.error && (
            <>
              {" · "}
              <span className="text-destructive">{slot.error}</span>
            </>
          )}
        </p>
        {(slot.status === "uploading" || slot.status === "uploaded") && (
          <div className="mt-1 h-1 w-full overflow-hidden bg-muted">
            <div
              className="h-full bg-accent transition-[width] duration-200 ease-out"
              style={{ width: `${(slot.progress * 100).toFixed(1)}%` }}
            />
          </div>
        )}
      </div>
      {slot.status === "uploading" ? (
        <Spinner size="sm" />
      ) : (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => onRemove(slot.id)}
        >
          Remove
        </Button>
      )}
    </div>
  );
}

function DropZone({
  onFiles,
  accept,
  onPick,
  children,
}: {
  onFiles: (files: FileList | File[]) => void;
  accept: string;
  onPick: () => void;
  children?: React.ReactNode;
}) {
  const [over, setOver] = React.useState(false);
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onPick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onPick();
        }
      }}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        if (e.dataTransfer.files) onFiles(e.dataTransfer.files);
      }}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center gap-2 border border-dashed bg-card px-6 py-10 text-center text-sm transition-colors hover:bg-muted",
        over ? "border-accent bg-accent/5" : "border-border-strong",
      )}
    >
      <p className="font-serif text-base text-foreground">
        Drag files here, or click to choose
      </p>
      <p className="text-xs text-muted-foreground">
        Accepts {accept}. Multiple files supported.
      </p>
      {children}
    </div>
  );
}

function Field({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <Label>{label}</Label>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Text tab
// ---------------------------------------------------------------------------

type TextUploadResult = {
  uploaded: UploadedFileDTO;
  pasted_text: string;
};

function TextTab() {
  const [pasted, setPasted] = React.useState("");
  const [uploading, setUploading] = React.useState(false);
  const [uploadError, setUploadError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<TextUploadResult | null>(null);

  const inputRef = React.useRef<HTMLInputElement>(null);

  const handleFile = async (file: File) => {
    setUploading(true);
    setUploadError(null);
    try {
      const dto = await uploadOne(file, "/uploads/text", () => {});
      const text = await file.text();
      setResult({ uploaded: dto, pasted_text: text });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  const handlePaste = async () => {
    const text = pasted.trim();
    if (!text) {
      setUploadError("Paste some text first.");
      return;
    }
    setUploading(true);
    setUploadError(null);
    try {
      const blob = new Blob([text], { type: "text/plain" });
      const file = new File([blob], "pasted.txt", { type: "text/plain" });
      const dto = await uploadOne(file, "/uploads/text", () => {});
      setResult({ uploaded: dto, pasted_text: text });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>Drop a text file</CardTitle>
        </CardHeader>
        <CardContent>
          <div
            role="button"
            tabIndex={0}
            onClick={() => inputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                inputRef.current?.click();
              }
            }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const f = e.dataTransfer.files?.[0];
              if (f) void handleFile(f);
            }}
            className="flex cursor-pointer flex-col items-center justify-center gap-2 border border-dashed border-border-strong bg-card px-6 py-8 text-center text-sm hover:bg-muted"
          >
            <p className="font-serif text-base">Drag a .txt or click to choose</p>
            <p className="text-xs text-muted-foreground">
              Transcripts, syllabi, or memos.
            </p>
            <input
              ref={inputRef}
              type="file"
              accept="text/plain,.txt,.md,.markdown"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void handleFile(f);
                e.target.value = "";
              }}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Or paste text</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <textarea
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
            placeholder="Paste a transcript, syllabus, or memo here…"
            className="min-h-[180px] w-full rounded-sm border border-input bg-card px-3 py-2 font-serif text-sm leading-relaxed text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background"
          />
          <div className="flex items-center gap-3">
            <LoadingButton loading={uploading} onClick={handlePaste}>
              Upload pasted text
            </LoadingButton>
            {uploadError && (
              <p className="text-xs text-destructive">{uploadError}</p>
            )}
          </div>
        </CardContent>
      </Card>

      {result && <TextResultCard result={result} />}
    </div>
  );
}

function TextResultCard({ result }: { result: TextUploadResult }) {
  const [mode, setMode] = React.useState<"none" | "syllabus" | "transcript">(
    "none",
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>Stored</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-col gap-1 text-xs">
          <p className="font-mono">
            <span className="text-muted-foreground">path</span>{" "}
            {result.uploaded.stored_path}
          </p>
          <p className="font-mono">
            <span className="text-muted-foreground">sha256</span>{" "}
            {result.uploaded.sha256.slice(0, 16)}…
          </p>
          <p className="text-muted-foreground tabular-nums">
            {formatBytes(result.uploaded.size_bytes)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant={mode === "syllabus" ? "default" : "outline"}
            onClick={() =>
              setMode((m) => (m === "syllabus" ? "none" : "syllabus"))
            }
          >
            Ingest as syllabus
          </Button>
          <Button
            size="sm"
            variant={mode === "transcript" ? "default" : "outline"}
            onClick={() =>
              setMode((m) => (m === "transcript" ? "none" : "transcript"))
            }
          >
            Ingest as transcript
          </Button>
        </div>
        {mode === "syllabus" && (
          <SyllabusIngestForm pastedText={result.pasted_text} />
        )}
        {mode === "transcript" && (
          <TranscriptIngestForm
            pastedText={result.pasted_text}
            sourcePath={result.uploaded.stored_path}
          />
        )}
      </CardContent>
    </Card>
  );
}

function SyllabusIngestForm({ pastedText }: { pastedText: string }) {
  const [corpora, setCorpora] = React.useState<CorpusSummary[] | null>(null);
  const [corpusId, setCorpusId] = React.useState("");
  const [bookId, setBookId] = React.useState("");
  const [professor, setProfessor] = React.useState("");
  const [semester, setSemester] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [done, setDone] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.get<CorpusSummary[]>("/corpora");
        if (!cancelled) setCorpora(data);
      } catch {
        if (!cancelled) setCorpora([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = async () => {
    if (!corpusId) {
      setErr("Pick a corpus.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.post("/ingest/syllabus", {
        corpus_id: corpusId,
        syllabus_markdown: pastedText,
        book_id: bookId.trim() || null,
        professor_name: professor.trim() || null,
        semester_hint: semester.trim() || null,
      });
      setDone(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Ingest failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 border border-border bg-subtle p-3">
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
      <Input
        placeholder="Book id (optional)"
        value={bookId}
        onChange={(e) => setBookId(e.target.value)}
      />
      <Input
        placeholder="Professor name (optional)"
        value={professor}
        onChange={(e) => setProfessor(e.target.value)}
      />
      <Input
        placeholder="Semester (optional)"
        value={semester}
        onChange={(e) => setSemester(e.target.value)}
      />
      <div className="flex items-center gap-3">
        <LoadingButton loading={busy} onClick={submit} size="sm">
          Ingest syllabus
        </LoadingButton>
        {err && <p className="text-xs text-destructive">{err}</p>}
        {done && <p className="text-xs text-success">Ingested.</p>}
      </div>
    </div>
  );
}

function TranscriptIngestForm({
  pastedText,
  sourcePath,
}: {
  pastedText: string;
  sourcePath: string;
}) {
  const [corpora, setCorpora] = React.useState<CorpusSummary[] | null>(null);
  const [corpusId, setCorpusId] = React.useState("");
  const [topic, setTopic] = React.useState("");
  const [assignment, setAssignment] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [done, setDone] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.get<CorpusSummary[]>("/corpora");
        if (!cancelled) setCorpora(data);
      } catch {
        if (!cancelled) setCorpora([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = async () => {
    if (!corpusId) {
      setErr("Pick a corpus.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.post("/transcripts", {
        corpus_id: corpusId,
        raw_text: pastedText,
        topic: topic.trim() || null,
        assignment_code: assignment.trim() || null,
        source_path: sourcePath,
      });
      setDone(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Ingest failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 border border-border bg-subtle p-3">
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
      <Input
        placeholder="Topic (optional)"
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
      />
      <Input
        placeholder="Assignment code (optional)"
        value={assignment}
        onChange={(e) => setAssignment(e.target.value)}
      />
      <div className="flex items-center gap-3">
        <LoadingButton loading={busy} onClick={submit} size="sm">
          Ingest transcript
        </LoadingButton>
        {err && <p className="text-xs text-destructive">{err}</p>}
        {done && <p className="text-xs text-success">Ingested.</p>}
      </div>
    </div>
  );
}
