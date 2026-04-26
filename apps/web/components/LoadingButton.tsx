"use client";

import * as React from "react";

import { Spinner } from "@/components/Spinner";
import { Button, type ButtonProps } from "@/components/ui/button";

/**
 * Button wrapper that surfaces a stable boolean loading state. The spinner
 * sits to the left of children — the label remains visible because the user
 * needs to remember which action they triggered (spec aesthetic note: no
 * disappearing labels under "spinner replaces text" patterns).
 *
 * The disabled flag is OR'd with `loading` so callers don't have to
 * juggle both. Click handlers fire only when neither is true; we wrap
 * `onClick` rather than relying on the native `disabled` attribute alone
 * because some test runners and browser extensions can fire clicks on
 * elements with `aria-disabled` that otherwise visually appear disabled.
 */

export interface LoadingButtonProps extends ButtonProps {
  loading: boolean;
}

export const LoadingButton = React.forwardRef<
  HTMLButtonElement,
  LoadingButtonProps
>(function LoadingButton(
  { loading, disabled, children, onClick, ...rest },
  ref,
) {
  const isDisabled = disabled || loading;
  const handleClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    if (loading) {
      event.preventDefault();
      return;
    }
    onClick?.(event);
  };

  return (
    <Button
      ref={ref}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      onClick={handleClick}
      {...rest}
    >
      {loading ? (
        <Spinner size="sm" label="Loading" />
      ) : null}
      {children}
    </Button>
  );
});

export default LoadingButton;
