import { act, render, screen } from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";

import { TaskProgress } from "@/components/TaskProgress";

type FetchMock = Mock<(...args: unknown[]) => Promise<Response>>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeTask(overrides: Partial<Record<string, unknown>>) {
  return {
    id: "task-abc",
    kind: "book_ingestion",
    status: "running",
    progress_step: "Extracting pages",
    progress_pct: 0.25,
    corpus_id: null,
    created_at: "2026-04-20T10:00:00Z",
    started_at: "2026-04-20T10:00:01Z",
    finished_at: null,
    error: null,
    result: {},
    ...overrides,
  };
}

describe("<TaskProgress />", () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = vi.fn();
    (globalThis as { fetch: typeof fetch }).fetch =
      fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("polls the task endpoint and fires onCompleted with the result", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(
          makeTask({
            status: "running",
            progress_pct: 0.1,
            progress_step: "Queued",
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          makeTask({
            status: "running",
            progress_pct: 0.55,
            progress_step: "Generating blocks",
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          makeTask({
            status: "completed",
            progress_pct: 1,
            progress_step: "Finished",
            finished_at: "2026-04-20T10:05:00Z",
            result: {
              book_id: "book-xyz",
              page_count: 1400,
              block_count: 8200,
              input_tokens: 100000,
              output_tokens: 25000,
            },
          }),
        ),
      );

    const onCompleted = vi.fn();
    const onFailed = vi.fn();

    render(
      <TaskProgress
        taskId="task-abc"
        onCompleted={onCompleted}
        onFailed={onFailed}
        pollIntervalMs={1000}
      />,
    );

    // First poll fires immediately on mount via setTimeout(0).
    // advanceTimersByTimeAsync also flushes microtasks for resolved promises.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Second poll after pollIntervalMs.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Third poll lands on `completed`.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    expect(onCompleted).toHaveBeenCalledTimes(1);
    expect(onCompleted).toHaveBeenCalledWith(
      expect.objectContaining({
        book_id: "book-xyz",
        page_count: 1400,
      }),
    );
    expect(onFailed).not.toHaveBeenCalled();

    // Advancing further should not trigger more polls now that we're terminal.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    // The "Done" line shows up.
    expect(screen.getByText(/Done\./)).toBeInTheDocument();
  });

  it("fires onFailed with the error string when the task ends in failure", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        makeTask({
          status: "failed",
          error: "Marker crashed at page 412.",
          progress_pct: 0.42,
          progress_step: "Marker pass",
        }),
      ),
    );

    const onFailed = vi.fn();
    render(
      <TaskProgress
        taskId="task-fail"
        onFailed={onFailed}
        pollIntervalMs={500}
      />,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(onFailed).toHaveBeenCalledWith("Marker crashed at page 412.");
    expect(
      screen.getByText("Marker crashed at page 412."),
    ).toBeInTheDocument();
  });

  it("calls /api/tasks/{taskId} with the correct path", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(makeTask({ status: "completed", progress_pct: 1 })),
    );
    render(<TaskProgress taskId="task-zzz" pollIntervalMs={200} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const [url] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toBe("/api/tasks/task-zzz");
  });
});
