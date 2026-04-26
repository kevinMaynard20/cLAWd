"use client";

import { AlertCircle, CheckCircle2, ShieldAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import * as React from "react";

import KeyInputPanel from "@/components/KeyInputPanel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";

/**
 * Spec §7.7.1 — the first-run wall.
 *
 * Gating rule: when `/api/credentials/gate` returns `llm_enabled: false`, the
 * FirstRunGate component pushes the user here. This page does NOT call the
 * gate itself — it lives outside the gate and is always reachable. The user
 * gets two input paths (paste / upload) for Anthropic plus an optional
 * Voyage key field.
 *
 * Validation model:
 *
 *  - `valid`        → auto-navigate to `/` after ~1s (success affordance).
 *  - `invalid`      → surface the API's error message, let the user retype.
 *  - `unreachable`  → warn, then offer a "Continue anyway" button. The
 *                     backend has ALREADY stored the key before validating
 *                     (see apps/api/src/routes/credentials.py), so we don't
 *                     need to re-POST — we just let the user navigate.
 */

type ValidationState = "valid" | "invalid" | "unreachable";
type ValidationResult = {
  state: ValidationState;
  message: string;
  status_code?: number | null;
  validated_at: string;
};
type StoreKeyResponse = {
  display: string;
  validation: ValidationResult;
};

type StoreStatus = {
  loading: boolean;
  state: ValidationState | null;
  message: string | null;
};

const EMPTY: StoreStatus = { loading: false, state: null, message: null };

export default function FirstRunPage() {
  const router = useRouter();
  const [anthropic, setAnthropic] = React.useState<StoreStatus>(EMPTY);
  const [voyage, setVoyage] = React.useState<StoreStatus>(EMPTY);

  const storeAnthropic = async (key: string) => {
    setAnthropic({ loading: true, state: null, message: null });
    try {
      const res = await api.post<StoreKeyResponse>(
        "/credentials/anthropic",
        { key },
      );
      setAnthropic({
        loading: false,
        state: res.validation.state,
        message: res.validation.message,
      });
      if (res.validation.state === "valid") {
        // Brief success dwell, then proceed.
        window.setTimeout(() => router.push("/"), 900);
      }
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : "Could not reach the local backend.";
      setAnthropic({ loading: false, state: "invalid", message });
    }
  };

  const storeVoyage = async (key: string) => {
    setVoyage({ loading: true, state: null, message: null });
    try {
      const res = await api.post<StoreKeyResponse>(
        "/credentials/voyage",
        { key },
      );
      setVoyage({
        loading: false,
        state: res.validation.state,
        message: res.validation.message,
      });
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : "Could not reach the local backend.";
      setVoyage({ loading: false, state: "invalid", message });
    }
  };

  const continueAnyway = () => router.push("/");

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col justify-center px-6 py-16">
      <div className="flex flex-col gap-8">
        <header className="flex flex-col gap-2">
          <p className="font-serif text-xs uppercase tracking-[0.16em] text-muted-foreground">
            Setup · Step 1 of 1
          </p>
          <h1 className="font-serif text-3xl font-semibold tracking-tight text-foreground">
            Provide your Anthropic API key
          </h1>
          <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">
            The study system makes all LLM calls on your behalf using a key you
            supply. No feature that requires an LLM call is enabled until a
            valid Anthropic key is stored.
          </p>
        </header>

        <Card>
          <CardHeader>
            <CardTitle>Anthropic</CardTitle>
            <p className="text-sm text-muted-foreground">
              Required. Paste the key or upload a file containing it.
            </p>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <KeyInputPanel
              provider="anthropic"
              placeholder="sk-ant-..."
              onSubmit={storeAnthropic}
              loading={anthropic.loading}
              submitLabel={anthropic.loading ? "Validating…" : "Validate and continue"}
            />

            {anthropic.state === "valid" && (
              <div
                role="status"
                className="flex items-start gap-2 rounded-sm border border-success/40 bg-success/10 px-3 py-2 text-sm text-success"
              >
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <div>
                  <p className="font-medium">Key accepted.</p>
                  <p className="text-xs opacity-90">
                    Taking you to the study workspace.
                  </p>
                </div>
              </div>
            )}

            {anthropic.state === "invalid" && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-sm border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <div>
                  <p className="font-medium">Key rejected.</p>
                  <p className="text-xs opacity-90">
                    {anthropic.message ?? "Anthropic did not accept this key."}
                  </p>
                </div>
              </div>
            )}

            {anthropic.state === "unreachable" && (
              <div
                role="alert"
                className="flex items-start justify-between gap-3 rounded-sm border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning"
              >
                <div className="flex items-start gap-2">
                  <ShieldAlert
                    className="mt-0.5 h-4 w-4 shrink-0"
                    aria-hidden="true"
                  />
                  <div>
                    <p className="font-medium">Could not reach Anthropic.</p>
                    <p className="text-xs opacity-90">
                      {anthropic.message ??
                        "We stored your key locally but could not validate it against Anthropic."}
                    </p>
                  </div>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={continueAnyway}
                >
                  Continue anyway
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Voyage AI</CardTitle>
            <p className="text-sm text-muted-foreground">
              Optional — enables semantic retrieval. BM25 fallback works
              without it.
            </p>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <KeyInputPanel
              provider="voyage"
              placeholder="pa-voyage-..."
              onSubmit={storeVoyage}
              loading={voyage.loading}
              submitLabel={voyage.loading ? "Validating…" : "Save Voyage key"}
            />

            {voyage.state === "valid" && (
              <div
                role="status"
                className="flex items-start gap-2 rounded-sm border border-success/40 bg-success/10 px-3 py-2 text-sm text-success"
              >
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <p>Voyage key accepted.</p>
              </div>
            )}
            {voyage.state === "invalid" && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-sm border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <p>{voyage.message ?? "Voyage rejected this key."}</p>
              </div>
            )}
            {voyage.state === "unreachable" && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-sm border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning"
              >
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <p>
                  {voyage.message ??
                    "Could not reach Voyage. Key saved; we'll retry when you use it."}
                </p>
              </div>
            )}

            <div className="flex items-center justify-between rule-above pt-3 text-xs text-muted-foreground">
              <span>
                You can add or change this later in Settings.
              </span>
              <button
                type="button"
                onClick={continueAnyway}
                className="law-link"
              >
                Skip for now
              </button>
            </div>
          </CardContent>
        </Card>

        <footer className="rule-above pt-4 text-xs leading-relaxed text-muted-foreground">
          <p>
            Keys are stored in your OS keychain (macOS Keychain / Windows
            Credential Manager / Linux Secret Service). They are never logged
            or transmitted except to the provider.
          </p>
        </footer>
      </div>
    </main>
  );
}
