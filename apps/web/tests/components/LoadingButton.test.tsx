import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LoadingButton } from "@/components/LoadingButton";

describe("<LoadingButton />", () => {
  it("renders the children label even while loading", () => {
    render(<LoadingButton loading={true}>Save</LoadingButton>);
    expect(screen.getByRole("button")).toHaveTextContent("Save");
  });

  it("is disabled while loading", () => {
    render(<LoadingButton loading={true}>Save</LoadingButton>);
    const btn = screen.getByRole("button");
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("aria-busy", "true");
  });

  it("renders a spinner alongside the children when loading", () => {
    render(<LoadingButton loading={true}>Save</LoadingButton>);
    // The Spinner component sets role="status".
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("does not render a spinner when not loading", () => {
    render(<LoadingButton loading={false}>Save</LoadingButton>);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("invokes onClick when not loading", async () => {
    const onClick = vi.fn();
    render(
      <LoadingButton loading={false} onClick={onClick}>
        Save
      </LoadingButton>,
    );
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("does not invoke onClick when loading", async () => {
    const onClick = vi.fn();
    render(
      <LoadingButton loading={true} onClick={onClick}>
        Save
      </LoadingButton>,
    );
    // userEvent clicks won't fire on disabled buttons; assert no call.
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).not.toHaveBeenCalled();
  });

  it("respects an external `disabled` even when not loading", () => {
    render(
      <LoadingButton loading={false} disabled>
        Save
      </LoadingButton>,
    );
    expect(screen.getByRole("button")).toBeDisabled();
  });
});
