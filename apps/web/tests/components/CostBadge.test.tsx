import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";

import { CostBadge } from "@/components/CostBadge";

// Mock next/navigation — we only assert on the router mock, not on
// actual navigation. The useRouter() hook returns a stub whose `push`
// we introspect.
const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

type FetchMock = Mock<(...args: unknown[]) => Promise<Response>>;

describe("<CostBadge />", () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    pushMock.mockReset();
    fetchMock = vi.fn();
    (globalThis as { fetch: typeof fetch }).fetch =
      fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the session total from a mocked /api/costs/session", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          session_id: "abc-123",
          total_usd: "0.47",
          input_tokens: 100_000,
          output_tokens: 42_000,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    render(<CostBadge />);
    // Polling kicks off an immediate fetch; wait for the value to render.
    const dollars = await screen.findByText("$0.47");
    expect(dollars).toBeInTheDocument();
    expect(await screen.findByText("142K tokens")).toBeInTheDocument();
  });

  it("renders the $0.00 state before the first response resolves", () => {
    fetchMock.mockReturnValue(
      new Promise<Response>(() => {
        /* never resolves */
      }),
    );
    render(<CostBadge />);
    expect(screen.getByText("$0.00")).toBeInTheDocument();
    expect(screen.getByText("0 tokens")).toBeInTheDocument();
  });

  it("navigates to /settings/costs on click", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          session_id: "s",
          total_usd: "0",
          input_tokens: 0,
          output_tokens: 0,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    render(<CostBadge />);
    await userEvent.click(screen.getByRole("button"));
    expect(pushMock).toHaveBeenCalledWith("/settings/costs");
  });
});
