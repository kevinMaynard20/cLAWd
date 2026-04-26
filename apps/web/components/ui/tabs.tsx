"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Minimal controlled Tabs. We deliberately avoid adding @radix-ui/react-tabs
 * as a dependency — the package.json already lists react-dialog/-label/-slot/-tooltip
 * and we keep the scope tight. Keyboard interaction is left arrow / right arrow
 * / home / end between triggers; ARIA roles are wired so screen readers treat
 * this like a proper tablist.
 */

type TabsContextValue = {
  value: string;
  setValue: (next: string) => void;
  idPrefix: string;
};

const TabsContext = React.createContext<TabsContextValue | null>(null);

function useTabs(): TabsContextValue {
  const ctx = React.useContext(TabsContext);
  if (!ctx) throw new Error("Tabs components must be used inside <Tabs>.");
  return ctx;
}

export interface TabsProps extends React.HTMLAttributes<HTMLDivElement> {
  defaultValue: string;
  value?: string;
  onValueChange?: (next: string) => void;
}

function Tabs({
  defaultValue,
  value: controlled,
  onValueChange,
  className,
  children,
  ...props
}: TabsProps) {
  const [uncontrolled, setUncontrolled] = React.useState(defaultValue);
  const value = controlled ?? uncontrolled;
  const setValue = React.useCallback(
    (next: string) => {
      if (controlled === undefined) setUncontrolled(next);
      onValueChange?.(next);
    },
    [controlled, onValueChange],
  );
  const idPrefix = React.useId();
  const ctx = React.useMemo<TabsContextValue>(
    () => ({ value, setValue, idPrefix }),
    [value, setValue, idPrefix],
  );
  return (
    <TabsContext.Provider value={ctx}>
      <div className={cn("flex flex-col gap-4", className)} {...props}>
        {children}
      </div>
    </TabsContext.Provider>
  );
}

const TabsList = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, children, ...props }, ref) => (
  <div
    ref={ref}
    role="tablist"
    className={cn(
      "flex items-center gap-0 border-b border-border-strong",
      className,
    )}
    {...props}
  >
    {children}
  </div>
));
TabsList.displayName = "TabsList";

export interface TabsTriggerProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string;
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ className, value, onClick, onKeyDown, ...props }, ref) => {
    const { value: active, setValue, idPrefix } = useTabs();
    const isActive = active === value;
    const handleKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
      onKeyDown?.(event);
      if (event.defaultPrevented) return;
      const key = event.key;
      if (
        key !== "ArrowLeft" &&
        key !== "ArrowRight" &&
        key !== "Home" &&
        key !== "End"
      ) {
        return;
      }
      const current = event.currentTarget;
      const list = current.parentElement;
      if (!list) return;
      const triggers = Array.from(
        list.querySelectorAll<HTMLButtonElement>('[role="tab"]:not([disabled])'),
      );
      const idx = triggers.indexOf(current);
      if (idx === -1) return;
      event.preventDefault();
      let next = idx;
      if (key === "ArrowLeft") next = idx === 0 ? triggers.length - 1 : idx - 1;
      else if (key === "ArrowRight")
        next = idx === triggers.length - 1 ? 0 : idx + 1;
      else if (key === "Home") next = 0;
      else if (key === "End") next = triggers.length - 1;
      triggers[next]?.focus();
    };
    return (
      <button
        ref={ref}
        type="button"
        role="tab"
        id={`${idPrefix}-trigger-${value}`}
        aria-controls={`${idPrefix}-panel-${value}`}
        aria-selected={isActive}
        tabIndex={isActive ? 0 : -1}
        data-state={isActive ? "active" : "inactive"}
        onClick={(event) => {
          onClick?.(event);
          if (!event.defaultPrevented) setValue(value);
        }}
        onKeyDown={handleKeyDown}
        className={cn(
          "-mb-px inline-flex h-9 items-center justify-center border-b-2 border-transparent px-4 text-sm font-medium tracking-tight text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          "data-[state=active]:border-accent data-[state=active]:text-foreground",
          className,
        )}
        {...props}
      />
    );
  },
);
TabsTrigger.displayName = "TabsTrigger";

export interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string;
}

const TabsContent = React.forwardRef<HTMLDivElement, TabsContentProps>(
  ({ className, value, ...props }, ref) => {
    const { value: active, idPrefix } = useTabs();
    const isActive = active === value;
    if (!isActive) return null;
    return (
      <div
        ref={ref}
        role="tabpanel"
        id={`${idPrefix}-panel-${value}`}
        aria-labelledby={`${idPrefix}-trigger-${value}`}
        className={cn("animate-fade-in focus-visible:outline-none", className)}
        tabIndex={0}
        {...props}
      />
    );
  },
);
TabsContent.displayName = "TabsContent";

export { Tabs, TabsList, TabsTrigger, TabsContent };
