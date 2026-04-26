"use client";

import { FileUp, KeyRound } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { LoadingButton } from "@/components/LoadingButton";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

// Per-provider walkthrough URLs surfaced as a "How do I get this?" link
// next to the API-key field. We route to IN-APP help pages rather than
// raw external URLs because the bundled Tauri WebView silently swallows
// anchor `target="_blank"` clicks (no system-browser handoff without the
// shell plugin). The in-app help page embeds the same YouTube tutorial
// inline and walks through the steps as text, so it works regardless of
// whether the app is the Tauri shell or `pnpm dev` in a browser.
const HOWTO_URLS: Record<"anthropic" | "voyage", string | null> = {
  anthropic: "/help/api-key",
  voyage: null,
};

/**
 * Shared paste/upload control used by the first-run wall and the Rotate
 * modal on the Settings → API Key page. Keeps validation-state UI out of
 * this component so the owner decides how to surface it.
 */

export interface KeyInputPanelProps {
  provider: "anthropic" | "voyage";
  placeholder: string;
  /** Fired when the user commits a key (paste or upload). */
  onSubmit: (key: string) => void | Promise<void>;
  disabled?: boolean;
  /** Drives the spinner inside the submit button. Defaults to `disabled`. */
  loading?: boolean;
  submitLabel?: string;
  className?: string;
}

export function KeyInputPanel({
  provider,
  placeholder,
  onSubmit,
  disabled = false,
  loading,
  submitLabel = "Validate and continue",
  className,
}: KeyInputPanelProps) {
  const isLoading = loading ?? disabled;
  const [pasted, setPasted] = React.useState("");
  const [fileName, setFileName] = React.useState<string | null>(null);
  const [fileContents, setFileContents] = React.useState<string>("");
  const [error, setError] = React.useState<string | null>(null);
  const idPrefix = `${provider}-${React.useId()}`;

  const handleFile = async (file: File | null) => {
    if (!file) {
      setFileName(null);
      setFileContents("");
      return;
    }
    try {
      const text = await file.text();
      const trimmed = text.replace(/^\s+|\s+$/g, "");
      if (!trimmed) {
        setError("File is empty.");
        return;
      }
      if (trimmed.includes("\n")) {
        // Accept but warn: use only the first non-empty line.
        const firstLine = trimmed.split(/\r?\n/).find((l) => l.trim().length)
          ?.trim();
        if (!firstLine) {
          setError("File has no readable key line.");
          return;
        }
        setFileContents(firstLine);
      } else {
        setFileContents(trimmed);
      }
      setFileName(file.name);
      setError(null);
    } catch (_e) {
      setError("Could not read file.");
    }
  };

  const handleSubmit = async (raw: string) => {
    const key = raw.trim();
    if (!key) {
      setError("Enter a key before continuing.");
      return;
    }
    setError(null);
    await onSubmit(key);
  };

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <Tabs defaultValue="paste">
        <TabsList className="w-full">
          <TabsTrigger value="paste" className="flex-1 justify-start gap-2">
            <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
            Paste
          </TabsTrigger>
          <TabsTrigger value="upload" className="flex-1 justify-start gap-2">
            <FileUp className="h-3.5 w-3.5" aria-hidden="true" />
            Upload
          </TabsTrigger>
        </TabsList>

        <TabsContent value="paste" className="flex flex-col gap-3 pt-4">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-baseline justify-between gap-2">
              <label
                htmlFor={`${idPrefix}-paste`}
                className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground"
              >
                API key
              </label>
              {HOWTO_URLS[provider] && (
                <Link
                  href={HOWTO_URLS[provider] as string}
                  className="text-xs text-accent underline-offset-2 hover:underline"
                >
                  How do I get this? →
                </Link>
              )}
            </div>
            <Input
              id={`${idPrefix}-paste`}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={placeholder}
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              disabled={disabled}
              className="font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">
              Masked after save. Press Enter or click the button below.
            </p>
          </div>
          <div>
            <LoadingButton
              type="button"
              loading={isLoading}
              onClick={() => void handleSubmit(pasted)}
              disabled={pasted.trim().length === 0}
            >
              {submitLabel}
            </LoadingButton>
          </div>
        </TabsContent>

        <TabsContent value="upload" className="flex flex-col gap-3 pt-4">
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor={`${idPrefix}-file`}
              className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground"
            >
              Key file
            </label>
            <Input
              id={`${idPrefix}-file`}
              type="file"
              accept="text/plain,.txt,.key,.env,*"
              onChange={(e) => void handleFile(e.target.files?.[0] ?? null)}
              disabled={disabled}
              className="cursor-pointer text-xs file:cursor-pointer"
            />
            {fileName && (
              <p className="text-xs text-muted-foreground">
                Loaded <span className="font-mono">{fileName}</span>
                {fileContents && ` — ${fileContents.length} characters.`}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              A plain-text file containing the key on one line. Whitespace is
              trimmed.
            </p>
          </div>
          <div>
            <LoadingButton
              type="button"
              loading={isLoading}
              onClick={() => void handleSubmit(fileContents)}
              disabled={fileContents.trim().length === 0}
            >
              {submitLabel}
            </LoadingButton>
          </div>
        </TabsContent>
      </Tabs>

      {error && (
        <p
          role="alert"
          className="rounded-sm border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          {error}
        </p>
      )}
    </div>
  );
}

export default KeyInputPanel;
