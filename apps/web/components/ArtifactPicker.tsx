"use client";

import * as React from "react";

import { Spinner } from "@/components/Spinner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { formatRelativeTime, formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Generic artifact picker. Used by synthesis, attack-sheet, what-if, outline,
 * and the practice wizard's "select a rubric" step.
 *
 * Modes:
 *   - `multiple={false}` (default): single-select; click a row to set the
 *     `value`, click again to clear.
 *   - `multiple={true}`: checkbox-style; controlled by `values: string[]`.
 *
 * Filters: searchable by derived title (server does substring match on the
 * `title` derived in `routes/artifacts.py:_derive_title`).
 */

export type ArtifactRow = {
  id: string;
  corpus_id: string;
  type: string;
  created_at: string;
  cost_usd: string;
  parent_artifact_id: string | null;
  title: string;
};

type CommonProps = {
  corpusId: string;
  /** ArtifactType value (e.g. "case_brief"). Omit for "any type". */
  type?: string;
  /** Optional placeholder for the title-search input. */
  searchPlaceholder?: string;
  /** Hide the search input (useful in tight modal layouts). */
  hideSearch?: boolean;
  className?: string;
  /** Called whenever the row list refreshes — useful for "no rows available" empty states upstream. */
  onLoaded?: (rows: ArtifactRow[]) => void;
  /** Optional element rendered inside the picker when no rows match. Use it
   * to point users at *where* to create the missing artifact (e.g. "open a
   * book and brief some cases first"). */
  emptyHint?: React.ReactNode;
};

type SingleProps = CommonProps & {
  multiple?: false;
  value: string | null;
  onChange: (id: string | null) => void;
};

type MultiProps = CommonProps & {
  multiple: true;
  values: string[];
  onChange: (ids: string[]) => void;
};

export function ArtifactPicker(props: SingleProps | MultiProps) {
  const [rows, setRows] = React.useState<ArtifactRow[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [q, setQ] = React.useState("");

  const { corpusId, type, hideSearch, className, onLoaded, emptyHint } = props;

  const refresh = React.useCallback(
    async (needle: string) => {
      setError(null);
      try {
        const res = await api.get<{ count: number; artifacts: ArtifactRow[] }>(
          "/artifacts",
          { corpus_id: corpusId, type: type ?? undefined, q: needle || undefined, limit: 200 },
        );
        setRows(res.artifacts);
        onLoaded?.(res.artifacts);
      } catch (err) {
        setRows([]);
        setError(
          err instanceof ApiError
            ? err.message
            : "Could not load artifacts.",
        );
      }
    },
    [corpusId, type, onLoaded],
  );

  React.useEffect(() => {
    void refresh("");
  }, [refresh]);

  const handleSearch = (next: string) => {
    setQ(next);
    void refresh(next);
  };

  const isSelected = (id: string): boolean => {
    if (props.multiple) return props.values.includes(id);
    return props.value === id;
  };

  const toggle = (id: string) => {
    if (props.multiple) {
      const next = props.values.includes(id)
        ? props.values.filter((x) => x !== id)
        : [...props.values, id];
      props.onChange(next);
    } else {
      props.onChange(props.value === id ? null : id);
    }
  };

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      {!hideSearch && (
        <Input
          value={q}
          onChange={(e) => handleSearch(e.target.value)}
          placeholder={props.searchPlaceholder ?? "Filter by title…"}
        />
      )}
      <div className="border border-border bg-card">
        {rows === null && (
          <div className="flex items-center gap-2 px-3 py-3 text-sm text-muted-foreground">
            <Spinner size="sm" />
            Loading artifacts…
          </div>
        )}
        {error && (
          <div className="px-3 py-3 text-sm text-destructive">{error}</div>
        )}
        {rows !== null && !error && rows.length === 0 && (
          <div className="px-3 py-4 text-sm text-muted-foreground">
            {emptyHint ?? (
              <>No artifacts {type ? `of type ${type}` : ""} in this corpus yet.</>
            )}
          </div>
        )}
        {rows !== null && rows.length > 0 && (
          <ul role="listbox" aria-multiselectable={props.multiple ? "true" : "false"}>
            {rows.map((r) => {
              const selected = isSelected(r.id);
              return (
                <li
                  key={r.id}
                  role="option"
                  aria-selected={selected}
                  onClick={() => toggle(r.id)}
                  className={cn(
                    "grid cursor-pointer grid-cols-[1fr_auto] items-center border-b border-border px-3 py-2 text-sm last:border-b-0 hover:bg-muted",
                    selected && "bg-accent/10",
                  )}
                >
                  <div className="min-w-0">
                    <p className="truncate font-serif text-base">{r.title}</p>
                    <p className="text-[11px] tabular-nums text-muted-foreground">
                      {r.type} · {formatRelativeTime(r.created_at)} ·{" "}
                      {formatUsd(r.cost_usd)} · <code className="font-mono">{r.id.slice(0, 8)}…</code>
                    </p>
                  </div>
                  {props.multiple ? (
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => toggle(r.id)}
                      onClick={(e) => e.stopPropagation()}
                      className="h-4 w-4"
                    />
                  ) : (
                    <span
                      className={cn(
                        "h-3 w-3 rounded-full border",
                        selected
                          ? "border-accent bg-accent"
                          : "border-border-strong bg-card",
                      )}
                      aria-hidden
                    />
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {props.multiple && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {props.values.length} selected
          </span>
          {props.values.length > 0 && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => props.onChange([])}
            >
              Clear
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
