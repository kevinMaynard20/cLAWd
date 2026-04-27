"use client";

import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * `window.prompt()` doesn't render in the Tauri WKWebView (JS dialogs are
 * disabled by default and we don't enable them). Every previous "ask user
 * for a topic" call site silently returned null, the click became a no-op,
 * and the user thought the feature was broken. This is the inline
 * replacement: a Radix-based modal with an input field, submit, and
 * cancel, behaving like prompt() but actually visible.
 *
 * Imperative-style usage so call sites can `await topicPrompt({...})`
 * without managing the open state themselves — see `useTopicPrompt`.
 */

type Pending = {
  title: string;
  description?: string;
  placeholder?: string;
  resolve: (value: string | null) => void;
};

export function useTopicPrompt(): {
  promptForTopic: (opts: {
    title: string;
    description?: string;
    placeholder?: string;
  }) => Promise<string | null>;
  dialog: React.ReactNode;
} {
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [value, setValue] = React.useState("");

  const promptForTopic = React.useCallback(
    (opts: {
      title: string;
      description?: string;
      placeholder?: string;
    }) =>
      new Promise<string | null>((resolve) => {
        setValue("");
        setPending({
          title: opts.title,
          description: opts.description,
          placeholder: opts.placeholder,
          resolve,
        });
      }),
    [],
  );

  const close = (result: string | null) => {
    pending?.resolve(result);
    setPending(null);
  };

  const dialog = (
    <Dialog
      open={pending !== null}
      onOpenChange={(open) => {
        if (!open) close(null);
      }}
    >
      <DialogContent>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = value.trim();
            close(trimmed.length > 0 ? trimmed : null);
          }}
          className="flex flex-col gap-4"
        >
          <DialogHeader>
            <DialogTitle>{pending?.title ?? ""}</DialogTitle>
            {pending?.description && (
              <DialogDescription>{pending.description}</DialogDescription>
            )}
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="topic-prompt-input">Topic</Label>
            <Input
              id="topic-prompt-input"
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={pending?.placeholder ?? ""}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => close(null)}>
              Cancel
            </Button>
            <Button type="submit" disabled={value.trim().length === 0}>
              Continue
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );

  return { promptForTopic, dialog };
}
