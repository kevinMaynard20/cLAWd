import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";

// Type the mocked fetch so vi.fn() inference doesn't balk at returning a
// Response-shaped object.
type MockedFetch = ReturnType<typeof vi.fn<(...args: unknown[]) => Promise<Response>>>;

describe("api.get", () => {
  let fetchMock: MockedFetch;

  beforeEach(() => {
    fetchMock = vi.fn();
    (globalThis as { fetch: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("GETs the expected /api/* URL and returns parsed JSON", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ ok: true, value: 42 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const out = await api.get<{ ok: boolean; value: number }>(
      "/costs/session",
    );
    expect(out).toEqual({ ok: true, value: 42 });
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toBe("/api/costs/session");
    expect((init as RequestInit).method).toBe("GET");
  });

  it("encodes query params", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ events: [], count: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await api.get("/costs/events", { limit: 50, feature: "case_brief" });
    const [url] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toContain("limit=50");
    expect(String(url)).toContain("feature=case_brief");
  });

  it("raises ApiError on non-2xx", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "nope" }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(api.get("/credentials/test")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      message: "nope",
    });
  });

  it("ApiError carries the status and body", async () => {
    fetchMock.mockResolvedValue(
      new Response("plain text", { status: 500 }),
    );
    try {
      await api.get("/anything");
      throw new Error("expected throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      const ae = err as ApiError;
      expect(ae.status).toBe(500);
      expect(ae.message).toBe("plain text");
    }
  });
});
