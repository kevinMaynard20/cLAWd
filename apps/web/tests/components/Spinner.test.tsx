import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Spinner } from "@/components/Spinner";

describe("<Spinner />", () => {
  it("renders the visually-hidden label so screen readers announce it", () => {
    render(<Spinner label="Loading data" />);
    const label = screen.getByText("Loading data");
    expect(label).toBeInTheDocument();
    expect(label).toHaveClass("sr-only");
  });

  it("renders without a label when none is provided", () => {
    render(<Spinner />);
    // role=status is announced even without a label.
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it.each([
    ["sm", "h-[14px]"],
    ["md", "h-[20px]"],
    ["lg", "h-[28px]"],
  ] as const)("applies size class for size=%s", (size, expectedClass) => {
    const { container } = render(<Spinner size={size} />);
    const ring = container.querySelector("span > span[aria-hidden='true']");
    expect(ring).not.toBeNull();
    expect(ring?.className).toContain(expectedClass);
  });

  it("uses the default 'md' size when no size is passed", () => {
    const { container } = render(<Spinner />);
    const ring = container.querySelector("span > span[aria-hidden='true']");
    expect(ring?.className).toContain("h-[20px]");
  });
});
