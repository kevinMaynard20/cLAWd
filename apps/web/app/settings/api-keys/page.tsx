"use client";

import { AlertCircle, CheckCircle2, ShieldAlert } from "lucide-react";
import * as React from "react";

import KeyInputPanel from "@/components/KeyInputPanel";
import { LoadingButton } from "@/components/LoadingButton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Spec §7.7.3 — Settings → API Key.
 *
 * One card per provider (Anthropic required, Voyage optional). Each card
 * shows masked state, plus Rotate / Test / Clear actions. Rotate opens the
 * same paste-or-upload panel used on the first-run wall. Clear is a
 * confirmation modal because it disables LLM features immediately.
 */

type StatusResponse = {
  anthropic_display: string | null;
  voyage_display: string | null;
  anthropic_present: boolean;
  voyage_present: boolean;
  last_validated_at: string | null;
  last_validation_ok: boolean | null;
};

type ValidationState = "valid" | "invalid" | "unreachable";
type ValidationResult = {
  state: ValidationState;
  message: string;
  status_code?: number | null;
  validated_at: string;
};
type StoreKeyResponse = { display: string; validation: ValidationResult };

type ProviderKey = "anthropic" | "voyage";

type ProviderState = {
  testResult: ValidationResult | null;
  busy: boolean;
  rotateOpen: boolean;
  clearOpen: boolean;
  rotateResult: ValidationResult | null;
};

const INITIAL_PROVIDER: ProviderState = {
  testResult: null,
  busy: false,
  rotateOpen: false,
  clearOpen: false,
  rotateResult: null,
};

export default function ApiKeysSettingsPage() {
  const [status, setStatus] = React.useState<StatusResponse | null>(null);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [anthropic, setAnthropic] = React.useState<ProviderState>(
    INITIAL_PROVIDER,
  );
  const [voyage, setVoyage] = React.useState<ProviderState>(INITIAL_PROVIDER);

  const refresh = React.useCallback(async () => {
    try {
      const next = await api.get<StatusResponse>("/credentials/status");
      setStatus(next);
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof ApiError
          ? err.message
          : "Could not reach the local backend.",
      );
    }
  }, []);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  const testProvider = async (provider: ProviderKey) => {
    const setter = provider === "anthropic" ? setAnthropic : setVoyage;
    setter((s) => ({ ...s, busy: true, testResult: null }));
    try {
      const result = await api.post<ValidationResult>("/credentials/test", {
        provider,
      });
      setter((s) => ({ ...s, busy: false, testResult: result }));
      void refresh();
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Test failed.";
      setter((s) => ({
        ...s,
        busy: false,
        testResult: {
          state: "invalid",
          message,
          validated_at: new Date().toISOString(),
        },
      }));
    }
  };

  const rotateProvider = async (provider: ProviderKey, key: string) => {
    const setter = provider === "anthropic" ? setAnthropic : setVoyage;
    const path =
      provider === "anthropic" ? "/credentials/anthropic" : "/credentials/voyage";
    setter((s) => ({ ...s, busy: true, rotateResult: null }));
    try {
      const res = await api.post<StoreKeyResponse>(path, { key });
      setter((s) => ({
        ...s,
        busy: false,
        rotateResult: res.validation,
        rotateOpen: res.validation.state === "invalid",
      }));
      void refresh();
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Rotate failed.";
      setter((s) => ({
        ...s,
        busy: false,
        rotateResult: {
          state: "invalid",
          message,
          validated_at: new Date().toISOString(),
        },
      }));
    }
  };

  const clearProvider = async (provider: ProviderKey) => {
    const setter = provider === "anthropic" ? setAnthropic : setVoyage;
    const path =
      provider === "anthropic" ? "/credentials/anthropic" : "/credentials/voyage";
    setter((s) => ({ ...s, busy: true }));
    try {
      await api.delete(path);
      setter({ ...INITIAL_PROVIDER, clearOpen: false });
      void refresh();
    } catch (err) {
      setter((s) => ({
        ...s,
        busy: false,
        testResult: {
          state: "invalid",
          message:
            err instanceof ApiError ? err.message : "Clear failed.",
          validated_at: new Date().toISOString(),
        },
      }));
    }
  };

  return (
    <div className="flex flex-col gap-8">
      <header>
        <h1 className="font-serif text-2xl font-semibold tracking-tight text-foreground">
          API keys
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Keys live in your OS keychain. They are never logged or transmitted
          except to the provider.
        </p>
      </header>

      {loadError && (
        <div
          role="alert"
          className="rounded-sm border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {loadError}
        </div>
      )}

      <ProviderCard
        title="Anthropic"
        required
        description="Used for all Claude model calls. Required to enable LLM features."
        display={status?.anthropic_display ?? null}
        present={status?.anthropic_present ?? false}
        state={anthropic}
        onOpenRotate={() =>
          setAnthropic((s) => ({ ...s, rotateOpen: true, rotateResult: null }))
        }
        onCloseRotate={() =>
          setAnthropic((s) => ({ ...s, rotateOpen: false }))
        }
        onSubmitRotate={(key) => rotateProvider("anthropic", key)}
        onOpenClear={() =>
          setAnthropic((s) => ({ ...s, clearOpen: true }))
        }
        onCloseClear={() =>
          setAnthropic((s) => ({ ...s, clearOpen: false }))
        }
        onConfirmClear={() => clearProvider("anthropic")}
        onTest={() => testProvider("anthropic")}
        rotatePlaceholder="sk-ant-..."
        clearWarning="All LLM features will disable immediately. In-flight calls will not continue."
      />

      <ProviderCard
        title="Voyage AI"
        required={false}
        description="Optional — enables semantic retrieval. BM25 fallback runs without it."
        display={status?.voyage_display ?? null}
        present={status?.voyage_present ?? false}
        state={voyage}
        onOpenRotate={() =>
          setVoyage((s) => ({ ...s, rotateOpen: true, rotateResult: null }))
        }
        onCloseRotate={() => setVoyage((s) => ({ ...s, rotateOpen: false }))}
        onSubmitRotate={(key) => rotateProvider("voyage", key)}
        onOpenClear={() => setVoyage((s) => ({ ...s, clearOpen: true }))}
        onCloseClear={() => setVoyage((s) => ({ ...s, clearOpen: false }))}
        onConfirmClear={() => clearProvider("voyage")}
        onTest={() => testProvider("voyage")}
        rotatePlaceholder="pa-voyage-..."
        clearWarning="Semantic retrieval will fall back to BM25 lexical search."
      />
    </div>
  );
}

interface ProviderCardProps {
  title: string;
  required: boolean;
  description: string;
  display: string | null;
  present: boolean;
  state: ProviderState;
  rotatePlaceholder: string;
  clearWarning: string;
  onOpenRotate: () => void;
  onCloseRotate: () => void;
  onSubmitRotate: (key: string) => Promise<void>;
  onOpenClear: () => void;
  onCloseClear: () => void;
  onConfirmClear: () => void;
  onTest: () => void;
}

function ProviderCard({
  title,
  required,
  description,
  display,
  present,
  state,
  rotatePlaceholder,
  clearWarning,
  onOpenRotate,
  onCloseRotate,
  onSubmitRotate,
  onOpenClear,
  onCloseClear,
  onConfirmClear,
  onTest,
}: ProviderCardProps) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <CardTitle>{title}</CardTitle>
            {required ? (
              <Badge variant="accent">Required</Badge>
            ) : (
              <Badge variant="muted">Optional</Badge>
            )}
            {present ? (
              <Badge variant="success">Stored</Badge>
            ) : (
              <Badge variant="outline">Not set</Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
        <KeyDisplay display={display} present={present} />
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          <LoadingButton
            type="button"
            variant="default"
            size="sm"
            onClick={onOpenRotate}
            loading={state.busy}
          >
            {present ? "Rotate" : "Set key"}
          </LoadingButton>
          <LoadingButton
            type="button"
            variant="outline"
            size="sm"
            onClick={onTest}
            loading={state.busy}
            disabled={!present}
          >
            Test
          </LoadingButton>
          <LoadingButton
            type="button"
            variant="ghost"
            size="sm"
            onClick={onOpenClear}
            loading={state.busy}
            disabled={!present}
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
          >
            Clear
          </LoadingButton>
        </div>

        {state.testResult && (
          <ValidationChip result={state.testResult} />
        )}
        {state.rotateResult && state.rotateResult.state === "valid" && (
          <ValidationChip result={state.rotateResult} />
        )}
        {state.rotateResult && state.rotateResult.state === "unreachable" && (
          <ValidationChip result={state.rotateResult} />
        )}
      </CardContent>

      <Dialog open={state.rotateOpen} onOpenChange={(o) => !o && onCloseRotate()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{present ? `Rotate ${title} key` : `Set ${title} key`}</DialogTitle>
            <DialogDescription>
              The existing key, if any, is replaced and re-validated.
            </DialogDescription>
          </DialogHeader>
          <KeyInputPanel
            provider={title.toLowerCase().startsWith("voyage") ? "voyage" : "anthropic"}
            placeholder={rotatePlaceholder}
            loading={state.busy}
            submitLabel={state.busy ? "Validating…" : "Save and validate"}
            onSubmit={async (key) => {
              await onSubmitRotate(key);
            }}
          />
          {state.rotateResult && (
            <ValidationChip result={state.rotateResult} />
          )}
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onCloseRotate}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={state.clearOpen} onOpenChange={(o) => !o && onCloseClear()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Clear {title} key?</DialogTitle>
            <DialogDescription>{clearWarning}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onCloseClear}>
              Cancel
            </Button>
            <Button variant="destructive" size="sm" onClick={onConfirmClear}>
              Clear key
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function KeyDisplay({
  display,
  present,
}: {
  display: string | null;
  present: boolean;
}) {
  if (!present || !display) {
    return (
      <code className="font-mono text-sm text-muted-foreground">
        (none)
      </code>
    );
  }
  return (
    <code className="rounded-sm border border-border bg-subtle px-2 py-1 font-mono text-xs tracking-tight text-foreground">
      {display}
    </code>
  );
}

function ValidationChip({ result }: { result: ValidationResult }) {
  const base =
    "flex items-start gap-2 rounded-sm border px-3 py-2 text-xs";
  if (result.state === "valid") {
    return (
      <div
        role="status"
        className={cn(base, "border-success/40 bg-success/10 text-success")}
      >
        <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>{result.message}</span>
      </div>
    );
  }
  if (result.state === "unreachable") {
    return (
      <div
        role="alert"
        className={cn(base, "border-warning/40 bg-warning/10 text-warning")}
      >
        <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>{result.message}</span>
      </div>
    );
  }
  return (
    <div
      role="alert"
      className={cn(base, "border-destructive/40 bg-destructive/10 text-destructive")}
    >
      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>{result.message}</span>
    </div>
  );
}
